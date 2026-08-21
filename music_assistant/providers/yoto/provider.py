"""Music Assistant mapping and provider implementation."""

from __future__ import annotations

import asyncio
import secrets
from collections.abc import AsyncGenerator, Sequence
from dataclasses import dataclass
from time import monotonic
from typing import TYPE_CHECKING
from urllib.parse import quote, unquote

from aiohttp import ClientError, web
from music_assistant_models.enums import MediaType, ProviderFeature, StreamType
from music_assistant_models.errors import MediaNotFoundError, ProviderUnavailableError
from music_assistant_models.media_items import (
    Album,
    Artist,
    Audiobook,
    AudioFormat,
    BrowseFolder,
    ItemMapping,
    SearchResults,
    Track,
)
from music_assistant_models.streamdetails import MultiPartPath, StreamDetails

from music_assistant.helpers.aiohttp_client import encoded_request_url
from music_assistant.models.music_provider import MusicProvider

from .catalogue import Catalogue, CatalogueCard
from .client import YotoAdapter
from .media import (
    common_format,
    content_type,
    has_compatible_formats,
    map_album,
    map_artist,
    map_audiobook,
    map_image,
    map_track,
)
from .setup_flow import CONF_CLIENT_ID, CONF_REFRESH_TOKEN

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant

SUPPORTED_FEATURES = {
    ProviderFeature.BROWSE,
    ProviderFeature.SEARCH,
    ProviderFeature.LIBRARY_ARTISTS,
    ProviderFeature.LIBRARY_ALBUMS,
    ProviderFeature.LIBRARY_AUDIOBOOKS,
    ProviderFeature.LIBRARY_TRACKS,
}

SYNC_REFRESH_WINDOW = 30
MIN_PLAYBACK_SESSION_TTL = 15 * 60
PLAYBACK_SESSION_BUFFER = 15 * 60
MAX_PLAYBACK_SESSIONS = 64
PROXY_CHUNK_SIZE = 64 * 1024
PROXY_MAX_BYTES_PER_SECOND = 64 * 1024
PROXY_INITIAL_BURST_BYTES = PROXY_MAX_BYTES_PER_SECOND * 5
PROXY_RESPONSE_HEADERS = ("Content-Type", "Content-Length", "Content-Range", "Accept-Ranges")


@dataclass(slots=True)
class _AudiobookPlaybackSession:
    """Short-lived capability for one audiobook's ordered parts."""

    card_id: str
    part_ids: tuple[str, ...]
    expires_at: float


class YotoProvider(MusicProvider):
    """Read-only Yoto card library provider."""

    reload_on_streams_network_change = True
    adapter: YotoAdapter

    def __init__(
        self, mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
    ) -> None:
        """Initialize an empty Yoto provider."""
        super().__init__(mass, manifest, config, SUPPORTED_FEATURES)
        self.catalogue = Catalogue()
        self._sync_lock = asyncio.Lock()
        self._last_sync_refresh = 0.0
        self._audiobook_sessions: dict[str, _AudiobookPlaybackSession] = {}

    async def handle_async_init(self) -> None:
        """Authenticate and load the initial family-library snapshot."""
        client_id = str(self.get_setup_value(CONF_CLIENT_ID) or "")
        refresh_token = str(self.get_setup_value(CONF_REFRESH_TOKEN) or "")
        self.adapter = YotoAdapter(
            client_id,
            refresh_token,
            session=self.mass.http_session,
            token_callback=self._persist_refresh_token,
        )
        self.catalogue = await self.adapter.refresh_catalogue()
        self._last_sync_refresh = monotonic()
        self._on_unload_callbacks = [
            self.mass.streams.register_dynamic_route(
                f"/{self.instance_id}_yoto_part", self._handle_audiobook_part_request
            )
        ]

    async def unload(self, is_removed: bool = False) -> None:
        """Unregister dynamic stream routes when the provider is unloaded."""
        self._audiobook_sessions.clear()
        for callback in getattr(self, "_on_unload_callbacks", []):
            callback()
        await super().unload(is_removed)

    @property
    def is_streaming_provider(self) -> bool:
        """Return whether this provider resolves remote streams."""
        return True

    async def sync_library(self, media_type: MediaType) -> None:
        """Refresh once for a burst of independent MA media-type syncs."""
        if not hasattr(self, "_sync_lock"):
            self._sync_lock = asyncio.Lock()
            self._last_sync_refresh = 0.0
        async with self._sync_lock:
            now = monotonic()
            if now - self._last_sync_refresh >= SYNC_REFRESH_WINDOW:
                self.catalogue = await self.adapter.refresh_catalogue()
                self._last_sync_refresh = monotonic()
            await super().sync_library(media_type)

    async def get_library_albums(self) -> AsyncGenerator[Album]:
        """Yield non-story cards as albums."""
        for card in self.catalogue.cards.values():
            if not card.is_audiobook:
                yield map_album(card, self.instance_id)

    async def get_library_artists(self) -> AsyncGenerator[Artist]:
        """Yield each distinct card author once."""
        seen: set[str] = set()
        for card in self.catalogue.cards.values():
            artist = map_artist(card.author, self.instance_id)
            if artist.item_id not in seen:
                seen.add(artist.item_id)
                yield artist

    async def get_library_audiobooks(self) -> AsyncGenerator[Audiobook]:
        """Yield story cards as resumable audiobooks."""
        for card in self.catalogue.cards.values():
            if card.is_audiobook:
                yield map_audiobook(card, self.instance_id)

    async def get_library_tracks(self) -> AsyncGenerator[Track]:
        """Yield every playable card track in source order."""
        for card in self.catalogue.cards.values():
            if card.is_audiobook:
                continue
            for track in card.tracks:
                yield map_track(card, track, self.instance_id)

    async def get_album(self, prov_album_id: str) -> Album:
        """Return one card as an album."""
        if (card := self.catalogue.cards.get(prov_album_id)) is None or card.is_audiobook:
            raise MediaNotFoundError(f"Yoto card {prov_album_id!r} is unavailable")
        return map_album(card, self.instance_id)

    async def get_artist(self, prov_artist_id: str) -> Artist:
        """Return one card author by stable provider ID."""
        for card in self.catalogue.cards.values():
            artist = map_artist(card.author, self.instance_id)
            if artist.item_id == prov_artist_id:
                return artist
        raise MediaNotFoundError(f"Yoto artist {prov_artist_id!r} is unavailable")

    async def get_audiobook(self, prov_audiobook_id: str) -> Audiobook:
        """Return one story card as an audiobook."""
        if (card := self.catalogue.cards.get(prov_audiobook_id)) is None or not card.is_audiobook:
            raise MediaNotFoundError(f"Yoto audiobook {prov_audiobook_id!r} is unavailable")
        return map_audiobook(card, self.instance_id)

    async def get_track(self, prov_track_id: str) -> Track:
        """Return one track by its stable provider ID."""
        track = self.catalogue.find_track(prov_track_id)
        if (
            track is None
            or (card := self.catalogue.cards.get(track.card_id)) is None
            or card.is_audiobook
        ):
            raise MediaNotFoundError("Yoto track is unavailable")
        return map_track(card, track, self.instance_id)

    async def get_album_tracks(self, prov_album_id: str) -> list[Track]:
        """Return the ordered tracks for one card."""
        if (card := self.catalogue.cards.get(prov_album_id)) is None or card.is_audiobook:
            raise MediaNotFoundError(f"Yoto card {prov_album_id!r} is unavailable")
        return [map_track(card, track, self.instance_id) for track in card.tracks]

    async def search(
        self, search_query: str, media_types: list[MediaType], limit: int = 5
    ) -> SearchResults:
        """Search cards, authors, series, chapters, and tracks."""
        needle = search_query.strip().casefold()
        result = SearchResults()
        if not needle or limit < 1:
            return result
        if MediaType.ALBUM in media_types:
            result.albums = [
                map_album(card, self.instance_id)
                for card in self.catalogue.cards.values()
                if not card.is_audiobook and needle in _card_search_text(card)
            ][:limit]
        if MediaType.AUDIOBOOK in media_types:
            result.audiobooks = [
                map_audiobook(card, self.instance_id)
                for card in self.catalogue.cards.values()
                if card.is_audiobook and needle in _card_search_text(card)
            ][:limit]
        if MediaType.TRACK in media_types:
            matches: list[Track] = []
            for card in self.catalogue.cards.values():
                if card.is_audiobook:
                    continue
                card_text = _card_search_text(card)
                for source in card.tracks:
                    track_text = (
                        f"{card_text} {source.chapter_title or ''} {source.title}".casefold()
                    )
                    if needle in track_text:
                        matches.append(map_track(card, source, self.instance_id))
                        if len(matches) >= limit:
                            break
                if len(matches) >= limit:
                    break
            result.tracks = matches
        return result

    async def browse(self, path: str) -> Sequence[Album | Audiobook | ItemMapping | BrowseFolder]:
        """Browse all cards and Yoto library groups."""
        root = f"{self.instance_id}://"
        if path in (self.instance_id, root):
            return [
                BrowseFolder(
                    item_id="cards",
                    provider=self.instance_id,
                    name="All Yoto cards",
                    translation_key="all_cards",
                    path=f"{root}cards",
                ),
                BrowseFolder(
                    item_id="groups",
                    provider=self.instance_id,
                    name="Yoto library groups",
                    translation_key="library_groups",
                    path=f"{root}groups",
                ),
            ]
        if path == f"{root}cards":
            return [_map_card(card, self.instance_id) for card in self.catalogue.cards.values()]
        if path == f"{root}groups":
            return [
                BrowseFolder(
                    item_id=group.item_id,
                    provider=self.instance_id,
                    name=group.name,
                    path=f"{root}group/{quote(group.item_id, safe='')}",
                    image=map_image(group.artwork, self.instance_id) if group.artwork else None,
                )
                for group in self.catalogue.groups.values()
            ]
        prefix = f"{root}group/"
        if path.startswith(prefix):
            group = self.catalogue.groups.get(unquote(path.removeprefix(prefix)))
            if group is None:
                raise MediaNotFoundError("Yoto group is unavailable")
            return [
                _map_card(card, self.instance_id)
                for card_id in group.card_ids
                if (card := self.catalogue.cards.get(card_id)) is not None
            ]
        raise MediaNotFoundError("Unknown Yoto browse path")

    async def get_stream_details(self, item_id: str, media_type: MediaType) -> StreamDetails:
        """Resolve a fresh signed stream immediately before playback."""
        if media_type is MediaType.AUDIOBOOK:
            return await self._get_audiobook_stream_details(item_id)
        if media_type is not MediaType.TRACK:
            raise MediaNotFoundError("Yoto only streams tracks and audiobooks")
        source = self.catalogue.find_track(item_id)
        if (
            source is None
            or (card := self.catalogue.cards.get(source.card_id)) is None
            or card.is_audiobook
        ):
            raise MediaNotFoundError("Yoto track is unavailable")
        resolved = await self.adapter.resolve_stream(item_id)
        return StreamDetails(
            provider=self.instance_id,
            item_id=item_id,
            audio_format=AudioFormat(content_type=content_type(resolved.format or source.format)),
            media_type=MediaType.TRACK,
            stream_type=StreamType.HTTP,
            duration=resolved.duration or source.duration,
            path=resolved.path,
            allow_seek=True,
            can_seek=True,
        )

    async def _persist_refresh_token(self, refresh_token: str) -> None:
        """Persist a single-use rotated token before further API work."""
        self._update_setup_data(CONF_REFRESH_TOKEN, refresh_token, immediate=True)

    async def _get_audiobook_stream_details(self, item_id: str) -> StreamDetails:
        """Build a multipart audiobook with short-lived capability URLs."""
        card = self.catalogue.cards.get(item_id)
        if card is None or not card.is_audiobook or not card.tracks:
            raise MediaNotFoundError("Yoto audiobook is unavailable")
        if not has_compatible_formats(card):
            raise MediaNotFoundError("Yoto audiobook has incompatible audio properties")
        if not all(source.duration > 0 for source in card.tracks):
            raise MediaNotFoundError("Yoto audiobook has invalid part durations")
        now = monotonic()
        sessions = self._get_audiobook_sessions()
        self._prune_audiobook_sessions(now)
        if len(sessions) >= MAX_PLAYBACK_SESSIONS:
            sessions.pop(next(iter(sessions)))
        session_id = secrets.token_urlsafe(32)
        duration = sum(max(source.duration, 0) for source in card.tracks)
        session_ttl = max(MIN_PLAYBACK_SESSION_TTL, duration + PLAYBACK_SESSION_BUFFER)
        sessions[session_id] = _AudiobookPlaybackSession(
            card_id=card.item_id,
            part_ids=tuple(source.item_id for source in card.tracks),
            expires_at=now + session_ttl,
        )
        parts = [
            MultiPartPath(
                path=(
                    f"{self.mass.streams.base_url}/{self.instance_id}_yoto_part"
                    f"?session_id={session_id}&part={part_index}"
                ),
                duration=source.duration,
            )
            for part_index, source in enumerate(card.tracks)
        ]
        return StreamDetails(
            provider=self.instance_id,
            item_id=item_id,
            audio_format=AudioFormat(content_type=content_type(common_format(card))),
            media_type=MediaType.AUDIOBOOK,
            stream_type=StreamType.HTTP,
            duration=duration,
            path=parts[0].path if len(parts) == 1 else parts,
            allow_seek=True,
            can_seek=True,
        )

    async def _handle_audiobook_part_request(self, request: web.Request) -> web.StreamResponse:
        """Resolve and proxy one authorized audiobook part without exposing its signed URL."""
        session_id = request.query.get("session_id")
        part_value = request.query.get("part")
        if not session_id or part_value is None:
            raise web.HTTPBadRequest(text="Missing audiobook session or part")
        try:
            part_index = int(part_value)
        except ValueError as err:
            raise web.HTTPBadRequest(text="Invalid audiobook part") from err
        sessions = self._get_audiobook_sessions()
        session = sessions.get(session_id)
        if session is None:
            raise web.HTTPNotFound(text="Yoto audiobook session is unavailable")
        if session.expires_at <= monotonic():
            sessions.pop(session_id, None)
            raise web.HTTPGone(text="Yoto audiobook session expired")
        if part_index < 0 or part_index >= len(session.part_ids):
            raise web.HTTPNotFound(text="Yoto audiobook part is unavailable")
        item_id = session.part_ids[part_index]
        source = self.catalogue.find_track(item_id)
        if (
            source is None
            or (card := self.catalogue.cards.get(source.card_id)) is None
            or not card.is_audiobook
            or card.item_id != session.card_id
        ):
            raise web.HTTPNotFound(text="Yoto audiobook part is unavailable")
        try:
            resolved = await self.adapter.resolve_stream(item_id)
        except ProviderUnavailableError as err:
            raise web.HTTPServiceUnavailable(text="Yoto audiobook part is unavailable") from err
        return await self._proxy_audiobook_part(request, resolved.path)

    async def _proxy_audiobook_part(
        self, request: web.Request, signed_url: str
    ) -> web.StreamResponse:
        """Stream an upstream Yoto media response through the local capability route."""
        request_headers: dict[str, str] = {}
        if range_header := request.headers.get("Range"):
            request_headers["Range"] = range_header
        try:
            async with self.mass.http_session.get(
                encoded_request_url(signed_url), headers=request_headers
            ) as upstream_response:
                if upstream_response.status not in (200, 206):
                    raise web.HTTPServiceUnavailable(text="Yoto audiobook stream is unavailable")
                response_headers = {
                    name: upstream_response.headers[name]
                    for name in PROXY_RESPONSE_HEADERS
                    if name in upstream_response.headers
                }
                response_headers["Cache-Control"] = "no-store"
                response = web.StreamResponse(
                    status=upstream_response.status,
                    headers=response_headers,
                )
                await response.prepare(request)
                started_at = monotonic()
                bytes_written = 0
                async for chunk in upstream_response.content.iter_chunked(PROXY_CHUNK_SIZE):
                    await response.write(chunk)
                    bytes_written += len(chunk)
                    if bytes_written > PROXY_INITIAL_BURST_BYTES:
                        target_elapsed = (
                            bytes_written - PROXY_INITIAL_BURST_BYTES
                        ) / PROXY_MAX_BYTES_PER_SECOND
                        if (delay := target_elapsed - (monotonic() - started_at)) > 0:
                            await asyncio.sleep(delay)
                await response.write_eof()
                return response
        except web.HTTPException:
            raise
        except ClientError, TimeoutError:
            raise web.HTTPServiceUnavailable(text="Yoto audiobook stream is unavailable") from None

    def _get_audiobook_sessions(self) -> dict[str, _AudiobookPlaybackSession]:
        if not hasattr(self, "_audiobook_sessions"):
            self._audiobook_sessions = {}
        return self._audiobook_sessions

    def _prune_audiobook_sessions(self, now: float) -> None:
        sessions = self._get_audiobook_sessions()
        for session_id, session in tuple(sessions.items()):
            if session.expires_at <= now:
                sessions.pop(session_id, None)


def _card_search_text(card: CatalogueCard) -> str:
    return " ".join(
        value for value in (card.title, card.author, card.series_title, card.category) if value
    ).casefold()


def _map_card(card: CatalogueCard, instance_id: str) -> Album | Audiobook:
    return map_audiobook(card, instance_id) if card.is_audiobook else map_album(card, instance_id)
