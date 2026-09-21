"""Music Assistant adapter for the native FeiNiu client."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import aclosing
from copy import deepcopy
from dataclasses import replace
from functools import partial
from time import monotonic
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
    audio_format,
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
        self._collections: dict[str, tuple[float, dict[str, MediaItem]]] = {}
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
        self._collections.clear()
        if hasattr(self, "_client"):
            await self._client.__aexit__(None, None, None)

    @property
    def is_streaming_provider(self) -> bool:
        """Each configured account exposes its own library."""
        return False

    async def get_library_tracks(self) -> AsyncGenerator[Track]:
        """Yield every accessible track."""
        for item in (await self._collection("track", refresh=True)).values():
            yield cast("Track", deepcopy(item))

    async def get_library_albums(self) -> AsyncGenerator[Album]:
        """Yield every accessible album."""
        for item in (await self._collection("album", refresh=True)).values():
            yield cast("Album", deepcopy(item))

    async def get_library_artists(self) -> AsyncGenerator[Artist]:
        """Yield every accessible artist."""
        for item in (await self._collection("artist", refresh=True)).values():
            yield cast("Artist", deepcopy(item))

    async def get_library_playlists(self) -> AsyncGenerator[Playlist]:
        """Read the complete native playlist collection, which is not paginated."""
        for item in (await self._collection("playlist", refresh=True)).values():
            yield cast("Playlist", deepcopy(item))

    async def get_track(self, prov_track_id: str) -> Track:
        """Return listed metadata and optional lyrics for this account."""
        track = cast("Track", await self._listed("track", prov_track_id))
        detail = await self._track_detail(prov_track_id, self._cache_id)
        track.provider_mappings = {
            replace(mapping, audio_format=audio_format(detail["audioSpec"]))
            for mapping in track.provider_mappings
        }
        track.metadata.lyrics = detail["lyrics"]
        track.metadata.lrc_lyrics = detail["lrc_lyrics"]
        self._check_open()
        return track

    async def get_album(self, prov_album_id: str) -> Album:
        """Use the account-filtered album collection as the metadata source."""
        return cast("Album", await self._listed("album", prov_album_id))

    async def get_artist(self, prov_artist_id: str) -> Artist:
        """Return a listed artist or MA's local unknown-artist placeholder."""
        if prov_artist_id == UNKNOWN_ARTIST:
            return unknown_artist(self.instance_id)
        return cast("Artist", await self._listed("artist", prov_artist_id))

    async def get_playlist(self, prov_playlist_id: str) -> Playlist:
        """Return a listed, read-only playlist."""
        return cast("Playlist", await self._listed("playlist", prov_playlist_id))

    async def get_album_tracks(self, prov_album_id: str) -> list[Track]:
        """Read all pages of the album relationship."""
        await self._listed("album", prov_album_id)
        listed = await self._collection("track")
        return [
            cast("Track", deepcopy(listed[item["guid"]]))
            async for item in self._pages(
                lambda page: self._client.related("album", prov_album_id, page)
            )
            if item["guid"] in listed
        ]

    async def get_artist_albums(self, prov_artist_id: str) -> list[Album]:
        """Read all pages of the artist's album relationship."""
        if prov_artist_id == UNKNOWN_ARTIST:
            return []
        await self._listed("artist", prov_artist_id)
        listed = await self._collection("album")
        return [
            cast("Album", deepcopy(listed[item["guid"]]))
            async for item in self._pages(
                lambda page: self._client.related("artist", prov_artist_id, page, albums=True)
            )
            if item["guid"] in listed
        ]

    async def get_playlist_tracks(self, prov_playlist_id: str, page: int = 0) -> list[Track]:
        """Read a playlist page, retaining server order and duplicate track positions."""
        await self._listed("playlist", prov_playlist_id)
        listed = await self._collection("track")
        # Filtering a single page could turn an intermediate page into an empty
        # result, which MA treats as the end. Retain order and duplicate positions.
        ids = await self._playlist_ids(prov_playlist_id, self._cache_id)
        visible = [item_id for item_id in ids if item_id in listed]
        tracks = []
        for index, item_id in enumerate(visible[page * 100 : (page + 1) * 100]):
            track = cast("Track", deepcopy(listed[item_id]))
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
        item = await self._listed(kind, item_id)
        if path not in self._image_paths(item):
            raise ProviderPermissionDenied("Artwork is outside the current FeiNiu library")
        return await self._call(lambda: self._client.cover(cover))

    async def get_stream_details(self, item_id: str, media_type: MediaType) -> StreamDetails:
        """Return a server-owned stream with a stable track identity."""
        if media_type != MediaType.TRACK:
            raise UnplayableMediaError("FeiNiu supports track playback only")
        await self._listed("track", item_id)
        data = await self._call(lambda: self._client.detail("track", item_id))
        track = data.get("track")
        if not isinstance(track, dict) or track.get("guid") != item_id or track.get("isCue"):
            raise UnplayableMediaError("Missing track or unsupported CUE track")
        spec = data.get("audioSpec") or {}
        return StreamDetails(
            provider=self.instance_id,
            item_id=item_id,
            stream_type=StreamType.CUSTOM,
            audio_format=audio_format(spec),
            duration=number(track.get("duration")) // 1000 or None,
            size=number(spec.get("size")) or None,
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
        await self._listed("track", streamdetails.item_id)
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
    async def _track_detail(self, item_id: str, cache_id: str) -> dict[str, Any]:
        """Cache only playback format and optional lyrics for one provider load."""
        data = await self._call(lambda: self._client.detail("track", item_id))
        if not isinstance(data.get("track"), dict) or data["track"].get("guid") != item_id:
            raise InvalidDataError("FeiNiu returned a different or missing track")
        result: dict[str, Any] = {"audioSpec": {}, "lyrics": None, "lrc_lyrics": None}
        spec = data.get("audioSpec") or {}
        result["audioSpec"] = {
            key: spec[key]
            for key in ("format", "codec", "sampleRate", "bitDepth", "channel", "bitrate")
            if key in spec
        }
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

    async def _collection(self, kind: str, *, refresh: bool = False) -> dict[str, MediaItem]:
        self._check_open()
        async with self._collection_locks.setdefault(kind, asyncio.Lock()):
            cached = self._collections.get(kind)
            if not refresh and cached and monotonic() - cached[0] < 30:
                return cached[1]
            # An incomplete read must neither replace the collection nor fall back
            # to stale membership. A later request can retry the ordinary read.
            self._collections.pop(kind, None)
            rows = (
                await self._call(self._client.playlists)
                if kind == "playlist"
                else [row async for row in self._pages(lambda page: self._client.page(kind, page))]
            )
            parser = {
                "track": parse_track,
                "album": parse_album,
                "artist": parse_artist,
                "playlist": parse_playlist,
            }[kind]
            items = {}
            for row in rows:
                item = self._bind_images(parser(row, self.instance_id))
                if item.item_id in items:
                    raise InvalidDataError("FeiNiu returned duplicate collection identifiers")
                items[item.item_id] = item
            self._check_open()
            self._collections[kind] = (monotonic(), items)
            return items

    async def _listed(self, kind: str, item_id: str) -> MediaItem:
        items = await self._collection(kind)
        if item_id not in items:
            raise ProviderPermissionDenied("Item is outside the current FeiNiu library")
        return deepcopy(items[item_id])

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
    async def _playlist_ids(self, item_id: str, cache_id: str) -> list[str]:
        ids: list[str] = []
        total = None
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
                ids.append(row["guid"])
            if len(ids) == total:
                return ids
            if not rows or len(ids) > total:
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
                    self._collections.clear()
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
