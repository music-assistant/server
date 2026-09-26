"""Music Assistant adapter for the native FeiNiu client."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import aclosing
from copy import deepcopy
from dataclasses import replace
from functools import partial
from typing import Any, cast
from urllib.parse import quote, unquote
from uuid import uuid4

import aiohttp
from music_assistant_models.enums import MediaType, StreamType
from music_assistant_models.errors import (
    InvalidDataError,
    LoginFailed,
    MediaNotFoundError,
    ProviderPermissionDenied,
    RateLimited,
    ResourceTemporarilyUnavailable,
    UnplayableMediaError,
)
from music_assistant_models.media_items import (
    Album,
    Artist,
    MediaItem,
    Playlist,
    SearchResults,
    Track,
    UniqueList,
)
from music_assistant_models.streamdetails import StreamDetails

from music_assistant.constants import UNKNOWN_ARTIST
from music_assistant.controllers.cache import use_cache
from music_assistant.helpers.aiohttp_client import create_clientsession
from music_assistant.helpers.throttle_retry import Throttler
from music_assistant.models.music_provider import MusicProvider

from .client import AuthenticationError, FeiNiuClient
from .lyrics import parse_lyrics
from .parsers import (
    number,
    parse_album,
    parse_artist,
    parse_playlist,
    parse_track,
    unknown_artist,
)
from .protocol import PROFILE


class FeiNiuProvider(MusicProvider):
    """A personal library with authentication and caches isolated per instance."""

    async def handle_async_init(self) -> None:
        """Open an isolated session and authenticate with the configured music account."""
        self._login_lock = asyncio.Lock()
        self._collection_locks: dict[str, asyncio.Lock] = {}
        self._cache_id = uuid4().hex
        self._account_id: str | None = None
        self._image_scope = ""
        self._closed = False
        self._generation = 0
        self._failed_login_generation: int | None = None
        self._throttler = Throttler(rate_limit=1, period=0.25)
        self._client = FeiNiuClient(
            str(self.get_setup_value("url")),
            PROFILE,
            session_factory=lambda: create_clientsession(
                self.mass,
                cookie_jar=aiohttp.DummyCookieJar(),
                trust_env=False,
            ),
            acquire=self._throttler.acquire,
        )
        await self._client.__aenter__()
        try:
            await self._login()
        except BaseException:
            await self._client.__aexit__(None, None, None)
            raise

    async def unload(self, is_removed: bool = False) -> None:
        """Close only this instance's HTTP session."""
        self._closed = True
        if hasattr(self, "_client"):
            await self._client.__aexit__(None, None, None)

    @property
    def is_streaming_provider(self) -> bool:
        """Each configured account exposes its own library."""
        return False

    async def get_library_tracks(self) -> AsyncGenerator[Track]:
        """Yield every accessible track."""
        for item in (await self._collection("track")).values():
            yield cast("Track", deepcopy(item))

    async def get_library_albums(self) -> AsyncGenerator[Album]:
        """Yield every accessible album."""
        for item in (await self._collection("album")).values():
            yield cast("Album", deepcopy(item))

    async def get_library_artists(self) -> AsyncGenerator[Artist]:
        """Yield every accessible artist."""
        for item in (await self._collection("artist")).values():
            yield cast("Artist", deepcopy(item))

    async def get_library_playlists(self) -> AsyncGenerator[Playlist]:
        """Read the complete native playlist collection, which is not paginated."""
        for item in (await self._collection("playlist")).values():
            yield cast("Playlist", deepcopy(item))

    async def get_track(self, prov_track_id: str) -> Track:
        """Return accessible metadata and optional lyrics for this account."""
        track = cast("Track", await self._get_item("track", prov_track_id))
        detail = await self._track_lyrics(prov_track_id, self._cache_id)
        track.metadata.lyrics = detail["lyrics"]
        track.metadata.lrc_lyrics = detail["lrc_lyrics"]
        self._check_open()
        return track

    async def get_album(self, prov_album_id: str) -> Album:
        """Return album details filtered by the native service."""
        return cast("Album", await self._get_item("album", prov_album_id))

    async def get_artist(self, prov_artist_id: str) -> Artist:
        """Return native artist details or MA's local unknown-artist placeholder."""
        if prov_artist_id == UNKNOWN_ARTIST:
            return unknown_artist(self.instance_id)
        return cast("Artist", await self._get_item("artist", prov_artist_id))

    async def get_playlist(self, prov_playlist_id: str) -> Playlist:
        """Return an owned, read-only playlist from the native service."""
        return cast("Playlist", await self._get_item("playlist", prov_playlist_id))

    async def get_album_tracks(self, prov_album_id: str) -> list[Track]:
        """Read all pages of the album relationship."""
        # Music 1.0.1 (0.8.41) album track rows omit accessStatus, unlike playlist rows.
        # Inaccessible tracks are filtered from the album relationship server-side.
        return [
            cast("Track", self._bind_images(parse_track(item, self.instance_id)))
            async for item in self._pages(
                lambda page: self._client.related("album", prov_album_id, page)
            )
        ]

    async def get_artist_albums(self, prov_artist_id: str) -> list[Album]:
        """Read all pages of the artist's album relationship."""
        if prov_artist_id == UNKNOWN_ARTIST:
            return []
        # Music 1.0.1 (0.8.41) artist relations filter inaccessible albums server-side
        # and omit accessStatus, unlike playlist track rows.
        return [
            cast("Album", self._bind_images(parse_album(item, self.instance_id)))
            async for item in self._pages(
                lambda page: self._client.related("artist", prov_artist_id, page, albums=True)
            )
        ]

    async def get_playlist_tracks(self, prov_playlist_id: str, page: int = 0) -> list[Track]:
        """Read a playlist page, retaining server order and duplicate track positions."""
        self._check_open()
        # Filtering a single page could turn an intermediate page into an empty
        # result, which MA treats as the end. Retain order and duplicate positions.
        items = await self._playlist_items(prov_playlist_id, self._cache_id)
        self._check_open()
        tracks = []
        for index, item in enumerate(items[page * 100 : (page + 1) * 100]):
            track = Track.from_dict(item)
            track.position = page * 100 + index + 1
            tracks.append(track)
        return tracks

    async def search(
        self, search_query: str, media_types: list[MediaType], limit: int = 5
    ) -> SearchResults:
        """Search the verified native media types, including read-only playlists."""
        results = SearchResults()
        if limit <= 0:
            return results
        for kind, attribute, parser in (
            (MediaType.TRACK, "tracks", parse_track),
            (MediaType.ALBUM, "albums", parse_album),
            (MediaType.ARTIST, "artists", parse_artist),
            (MediaType.PLAYLIST, "playlists", parse_playlist),
        ):
            if kind not in media_types:
                continue
            found = []
            async with aclosing(
                self._pages(
                    partial(self._client.search, kind.value, search_query, size=min(limit, 100))
                )
            ) as pages:
                async for item in pages:
                    found.append(self._bind_images(parser(item, self.instance_id)))
                    if len(found) >= limit:
                        break
            setattr(results, attribute, found)
        return results

    async def resolve_image(self, path: str) -> bytes:
        """Proxy authenticated artwork as bytes, never as a credential-bearing URL."""
        parts = path.split("/")
        if len(parts) != 5 or parts[:2] != ["scoped", self._image_scope]:
            raise ProviderPermissionDenied("Unknown FeiNiu artwork owner")
        kind, item_id, cover = (unquote(value) for value in parts[2:])
        if kind not in {"track", "album", "artist", "playlist"}:
            raise ProviderPermissionDenied("Unknown FeiNiu artwork owner")
        owner = await self._get_item(kind, item_id)
        if path not in self._image_paths(owner):
            raise ProviderPermissionDenied("Artwork is outside the current FeiNiu library")
        return await self._call(lambda: self._client.cover(cover))

    async def get_stream_details(self, item_id: str, media_type: MediaType) -> StreamDetails:
        """Return a server-owned stream with a stable track identity."""
        if media_type != MediaType.TRACK:
            raise UnplayableMediaError("FeiNiu supports track playback only")
        self._check_open()
        data = await self._detail("track", item_id, self._cache_id)
        self._check_open()
        track = Track.from_dict(data["item"])
        mapping = next(iter(track.provider_mappings))
        if not mapping.available:
            raise UnplayableMediaError("Unsupported CUE track")
        return StreamDetails(
            provider=self.instance_id,
            item_id=item_id,
            stream_type=StreamType.CUSTOM,
            audio_format=mapping.audio_format,
            duration=track.duration or None,
            size=data["size"],
            can_seek=False,
            allow_seek=True,
            expiration=0,
            data=self._cache_id,
        )

    async def get_audio_stream(
        self, streamdetails: StreamDetails, seek_position: int = 0
    ) -> AsyncGenerator[bytes]:
        """Stream audio in memory; MA performs seek decoding when can_seek is false."""
        if streamdetails.provider != self.instance_id or seek_position:
            raise InvalidDataError("Stream belongs to another instance or requests native seek")
        if streamdetails.data != self._cache_id:
            raise UnplayableMediaError("FeiNiu account configuration changed")
        self._check_open()
        await self._detail("track", streamdetails.item_id, self._cache_id)
        self._check_open()
        generation = self._generation
        emitted = False
        for attempt in range(2):
            try:
                async with aclosing(self._client.audio_stream(streamdetails.item_id)) as stream:
                    async for chunk in stream:
                        self._check_open()
                        emitted = True
                        yield chunk
                return
            except AuthenticationError:
                if emitted or attempt:
                    raise
                await self._reauthenticate(generation)

    @use_cache(expiration=30)
    async def _detail(self, kind: str, item_id: str, cache_id: str) -> dict[str, Any]:
        """Cache sanitized per-item metadata independently of library membership."""
        data = await self._call(lambda: self._client.detail(kind, item_id))
        row = data.get("track") if kind == "track" else data
        if not isinstance(row, dict) or row.get("guid") != item_id:
            raise InvalidDataError("FeiNiu returned a different or missing item")
        if kind == "track":
            status = row.get("accessStatus")
            if type(status) is not int or status not in {0, 2, 3}:
                raise InvalidDataError("Missing or unknown FeiNiu track access status")
            if status == 2:
                raise ProviderPermissionDenied("FeiNiu track is outside this account's scope")
            if status == 3:
                raise MediaNotFoundError("FeiNiu track file is unavailable")
            row = {**row, "audioSpec": data.get("audioSpec") or row.get("audioSpec") or {}}
        item = self._parse_item(kind, row)
        return {
            "item": item.to_dict(),
            "size": number((row.get("audioSpec") or {}).get("size")) or None,
        }

    @use_cache(expiration=30)
    async def _track_lyrics(self, item_id: str, cache_id: str) -> dict[str, Any]:
        """Read optional lyrics without retaining private native metadata."""
        result: dict[str, Any] = {"lyrics": None, "lrc_lyrics": None}
        try:
            lyrics = await self._call(lambda: self._client.lyrics(item_id))
            result["lyrics"], result["lrc_lyrics"] = parse_lyrics(lyrics)
        except (
            LoginFailed,
            MediaNotFoundError,
            ProviderPermissionDenied,
            ResourceTemporarilyUnavailable,
            RateLimited,
            InvalidDataError,
        ) as err:
            self.logger.warning("Optional FeiNiu lyrics unavailable (%s)", type(err).__name__)
        return result

    def _check_open(self) -> None:
        if self._closed:
            raise ResourceTemporarilyUnavailable("FeiNiu account configuration changed")

    async def _collection(self, kind: str) -> dict[str, MediaItem]:
        async with self._collection_locks.setdefault(kind, asyncio.Lock()):
            return await self._read_collection(kind)

    async def _read_collection(self, kind: str) -> dict[str, MediaItem]:
        self._check_open()
        rows = (
            await self._call(self._client.playlists)
            if kind == "playlist"
            else [row async for row in self._pages(lambda page: self._client.page(kind, page))]
        )
        items = {}
        for row in rows:
            item = self._parse_item(kind, row)
            if item.item_id in items:
                raise InvalidDataError("FeiNiu returned duplicate collection identifiers")
            items[item.item_id] = item
        self._check_open()
        return items

    async def _get_item(self, kind: str, item_id: str) -> MediaItem:
        self._check_open()
        detail = await self._detail(kind, item_id, self._cache_id)
        self._check_open()
        models: dict[str, type[Track | Album | Artist | Playlist]] = {
            "track": Track,
            "album": Album,
            "artist": Artist,
            "playlist": Playlist,
        }
        return models[kind].from_dict(detail["item"])

    def _parse_item(self, kind: str, row: dict[str, Any]) -> MediaItem:
        parser = {
            "track": parse_track,
            "album": parse_album,
            "artist": parse_artist,
            "playlist": parse_playlist,
        }[kind]
        return self._bind_images(parser(row, self.instance_id))

    def _bind_images(self, item: MediaItem) -> MediaItem:
        prefix = (
            f"scoped/{self._image_scope}/{item.media_type.value}/{quote(item.item_id, safe='')}/"
        )
        for media in self._image_items(item):
            media.metadata.images = UniqueList(
                replace(image, path=prefix + quote(image.path, safe=""))
                for image in media.metadata.images or []
            )
        return item

    @staticmethod
    def _image_items(item: MediaItem) -> list[MediaItem]:
        items = [item]
        if isinstance(item, Track) and isinstance(item.album, Album):
            items.append(item.album)
        for media in tuple(items):
            if isinstance(media, Track | Album):
                items.extend(artist for artist in media.artists if isinstance(artist, Artist))
        return items

    def _image_paths(self, item: MediaItem) -> set[str]:
        return {
            image.path for media in self._image_items(item) for image in media.metadata.images or []
        }

    @use_cache(expiration=30)
    async def _playlist_items(self, item_id: str, cache_id: str) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        total = None
        received = 0
        for page in range(1, 10001):
            rows, count = self._page_data(
                await self._call(partial(self._client.related, "playlist", item_id, page))
            )
            if total is not None and count != total:
                raise InvalidDataError("FeiNiu playlist changed during pagination")
            total = count
            for row in rows:
                if not isinstance(row.get("guid"), str) or not row["guid"]:
                    raise InvalidDataError("FeiNiu playlist has a missing track ID")
                status = row.get("accessStatus")
                if type(status) is int and status == 0:
                    items.append(self._parse_item("track", row).to_dict())
            received += len(rows)
            if received == total:
                return items
            if not rows or received > total:
                raise InvalidDataError("FeiNiu playlist pagination is incomplete")
        raise InvalidDataError("FeiNiu playlist exceeded its safety limit")

    async def _login(self) -> None:
        user = await self._client.login(
            str(self.get_setup_value("username")),
            str(self.get_setup_value("password")),
            str(self.get_setup_value("device_id")),
        )
        account_id = user.get("guid")
        if not isinstance(account_id, str) or not account_id:
            raise LoginFailed("FeiNiu login returned no account identity")
        if self._account_id is not None and self._account_id != account_id:
            self._closed = True
            raise LoginFailed("FeiNiu account identity changed; reconfigure the provider")
        self._account_id = account_id
        identity = [self.instance_id, self.get_setup_value("url"), account_id]
        self._image_scope = hashlib.sha256(repr(identity).encode()).hexdigest()[:24]
        self._generation += 1

    async def _reauthenticate(self, generation: int) -> None:
        async with self._login_lock:
            if generation == self._generation:
                if self._failed_login_generation == generation:
                    raise LoginFailed(
                        "FeiNiu music re-login already failed; reconfigure the provider"
                    )
                try:
                    await self._login()
                except LoginFailed:
                    self._failed_login_generation = generation
                    raise

    async def _call[T](self, action: Callable[[], Awaitable[T]]) -> T:
        self._check_open()
        generation = self._generation
        for attempt in range(2):
            try:
                result = await action()
                self._check_open()
                return result
            except AuthenticationError:
                if attempt:
                    raise
                await self._reauthenticate(generation)
        raise LoginFailed("FeiNiu music authentication failed")

    async def _pages(
        self, fetch: Callable[[int], Awaitable[dict[str, Any]]]
    ) -> AsyncGenerator[dict[str, Any]]:
        seen = set()
        total = None
        for page in range(1, 10001):
            items, count = self._page_data(await self._call(partial(fetch, page)))
            if total is not None and total != count:
                raise InvalidDataError("FeiNiu library changed during pagination; retry sync")
            total = count
            for item in items:
                guid = item.get("guid")
                if not isinstance(guid, str) or not guid or guid in seen:
                    raise InvalidDataError("FeiNiu returned missing or duplicate identifiers")
                seen.add(guid)
                yield item
            if len(seen) == total:
                return
            if not items or len(seen) > total:
                raise InvalidDataError("FeiNiu pagination is incomplete")
        raise InvalidDataError("FeiNiu pagination exceeded its safety limit")

    @staticmethod
    def _page_data(result: Any) -> tuple[list[dict[str, Any]], int]:
        if (
            not isinstance(result, dict)
            or not isinstance(result.get("list"), list)
            or type(result.get("total")) is not int
            or result["total"] < 0
            or not all(isinstance(item, dict) for item in result["list"])
        ):
            raise InvalidDataError("FeiNiu returned an invalid page")
        return result["list"], result["total"]
