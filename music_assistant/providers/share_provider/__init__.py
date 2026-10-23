"""
Share Links plugin for Music Assistant.

Generates and resolves provider-agnostic share links for tracks, artists and
playlists. Links encode item metadata as base64-encoded JSON in the URL
fragment — no server-side storage required.

Link format:
    https://<share_base_url>/#<base64url-encoded-json>
"""

from __future__ import annotations

import asyncio
import binascii
import logging
from collections.abc import Callable
from dataclasses import replace
from typing import TYPE_CHECKING, Literal, cast
from urllib.parse import quote

from aiohttp import ClientError, ClientTimeout
from music_assistant_models.auth import Scope
from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import (
    ConfigEntryType,
    ExternalID,
    MediaType,
    PlaylistMatchPolicy,
)
from music_assistant_models.errors import InvalidDataError
from music_assistant_models.media_items import BrowseFolder, ItemMapping, Track

from music_assistant.controllers.cache import use_cache
from music_assistant.helpers.compare import compare_strings
from music_assistant.helpers.external_ids import (
    is_valid_barcode,
    is_valid_isrc,
    normalize_external_id,
)
from music_assistant.helpers.playlists import (
    AlbumInfo,
    ArtistInfo,
    PlaylistItem,
    construct_media_item_from_playlist_item,
    generate_m3u,
)
from music_assistant.models.music_provider import PROVIDER_FETCH_ERRORS
from music_assistant.models.plugin import PluginProvider
from music_assistant.providers.share_provider.links import (
    build_provider_refs,
    compact_artwork_ref,
    compact_provider_ref,
    is_valid_artwork_url,
)
from music_assistant.providers.share_provider.payload import (
    SUPPORTED_SHARE_MEDIA_TYPES,
    SharePayload,
    TrackStub,
)

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.media_items import MediaItemType, Playlist
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType

CONF_SHARE_BASE_URL = "share_base_url"
DEFAULT_SHARE_BASE_URL = "https://share.music-assistant.io"
LOGGER = logging.getLogger(__name__)
__all__ = ["SharePayload", "ShareProvider", "TrackStub", "setup"]


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider instance with given configuration."""
    return ShareProvider(mass, manifest, config)


class ShareProvider(PluginProvider):
    """Share Links plugin provider."""

    _unregister_api_commands: list[Callable[[], None]]

    async def get_config_entries(self) -> tuple[ConfigEntry, ...]:
        """Return the configurable share-link base URL."""
        return (
            ConfigEntry(
                key=CONF_SHARE_BASE_URL,
                type=ConfigEntryType.STRING,
                default_value=DEFAULT_SHARE_BASE_URL,
            ),
        )

    async def handle_async_init(self) -> None:
        """Register authenticated share commands on startup."""
        self._unregister_api_commands = []
        try:
            self._unregister_api_commands.append(
                self.mass.register_api_command(
                    "share/generate",
                    self.generate_share_link,
                    required_scope=Scope.LIBRARY_READ,
                )
            )
            self._unregister_api_commands.append(
                self.mass.register_api_command(
                    "share/resolve",
                    self.resolve_share_link,
                    required_scope=Scope.LIBRARY_READ,
                )
            )
            self._unregister_api_commands.append(
                self.mass.register_api_command(
                    "share/import_playlist",
                    self.import_shared_playlist,
                    required_scope=Scope.LIBRARY_WRITE,
                )
            )
            self._unregister_api_commands.append(
                self.mass.register_api_command(
                    "share/preview_playlist",
                    self.preview_shared_playlist,
                    required_scope=Scope.LIBRARY_READ,
                )
            )
        except Exception:
            await self.unload()
            raise

    async def unload(self, is_removed: bool = False) -> None:
        """Unregister API commands on unload."""
        for unregister in getattr(self, "_unregister_api_commands", []):
            unregister()
        self._unregister_api_commands = []

    # ------------------------------------------------------------------
    # WebSocket API commands
    # ------------------------------------------------------------------

    async def generate_share_link(self, uri: str) -> str:
        """
        Generate a shareable link for a given MA item URI.

        :param uri: The MA item URI.
        :return: A fully-qualified share URL with the payload in the fragment.
        """
        item = await self.mass.music.get_item_by_uri(uri)
        if isinstance(item, BrowseFolder):
            raise InvalidDataError("Sharing browse folders is not supported")
        payload = await self._build_payload(item)
        base_url = self.config.get_value(CONF_SHARE_BASE_URL) or DEFAULT_SHARE_BASE_URL
        return f"{base_url}/#{payload.to_base64()}"

    async def resolve_share_link(
        self, encoded: str, allow_search: bool = True
    ) -> MediaItemType | None:
        """
        Resolve a share payload to a local MA item.

        :param encoded: Base64-encoded share payload.
        :param allow_search: Allow text search when no external ID matches.
        :return: The resolved MA item, or None if nothing matched.
        """
        try:
            payload = SharePayload.from_base64(encoded)
        except (binascii.Error, TypeError, ValueError) as err:
            LOGGER.debug("Could not decode share payload: %s", err)
            return None

        if payload.type == "playlist":
            return None
        return await self._resolve_media_item(payload, allow_search=allow_search)

    async def preview_shared_playlist(
        self, encoded: str, offset: int = 0, limit: int = 50
    ) -> list[Track]:
        """Return a read-only block of shared tracks, keeping unresolved entries."""
        if offset < 0 or limit < 1:
            raise InvalidDataError("Invalid share preview range")
        try:
            payload = SharePayload.from_base64(encoded)
        except (binascii.Error, TypeError, ValueError) as err:
            raise InvalidDataError("Invalid share payload") from err
        if payload.type != "playlist":
            raise InvalidDataError("Share payload is not a playlist")
        semaphore = asyncio.Semaphore(2)

        async def resolve_track(index: int, stub: TrackStub) -> Track:
            async with semaphore:
                track_payload = SharePayload(
                    v=2,
                    type="track",
                    title=stub.title,
                    artist=stub.artist,
                    album=stub.album,
                    isrc=stub.isrc,
                    mbid=stub.mbid,
                    mb_track=stub.mb_track,
                )
                item = await self._resolve_media_item(track_payload, allow_search=False)
                if isinstance(item, Track):
                    return replace(item, position=index + 1)
                preview = construct_media_item_from_playlist_item(
                    PlaylistItem(
                        path=f"share://track/{track_payload.to_base64()}",
                        title=stub.title,
                        metadata={"media_type": "track", "name": stub.title},
                        artists=[ArtistInfo(stub.artist, "", "", "")] if stub.artist else [],
                        album=AlbumInfo(stub.album, "", "", "") if stub.album else None,
                    ),
                    self.mass,
                )
                if not isinstance(preview, Track):
                    raise InvalidDataError("Invalid shared track preview")
                return replace(preview, position=index + 1)

        tracks = payload.tracks or []
        return list(
            await asyncio.gather(
                *(
                    resolve_track(offset + index, stub)
                    for index, stub in enumerate(tracks[offset : offset + limit])
                )
            )
        )

    async def import_shared_playlist(self, encoded: str) -> Playlist:
        """Import a playlist after the caller has confirmed the operation."""
        try:
            payload = SharePayload.from_base64(encoded)
        except (binascii.Error, TypeError, ValueError) as err:
            raise InvalidDataError(
                "Invalid share payload",
                translation_key="invalid_share_payload",
                translation_owner=self.translation_owner,
            ) from err
        if payload.type != "playlist":
            raise InvalidDataError(
                "Share payload is not a playlist",
                translation_key="share_payload_not_playlist",
                translation_owner=self.translation_owner,
            )
        if not payload.tracks:
            raise InvalidDataError(
                "Shared playlist contains no tracks",
                translation_key="share_playlist_empty",
                translation_owner=self.translation_owner,
            )

        playlist_items: list[PlaylistItem] = []
        for index, track in enumerate(payload.tracks):
            metadata = {"media_type": MediaType.TRACK.value, "name": track.title}
            if track.isrc and is_valid_isrc(track.isrc):
                metadata["isrc"] = normalize_external_id(ExternalID.ISRC, track.isrc)
            if track.mbid:
                metadata["mbid"] = normalize_external_id(ExternalID.MB_RECORDING, track.mbid)
            if track.mb_track:
                metadata["mb_track"] = normalize_external_id(ExternalID.MB_TRACK, track.mb_track)
            playlist_items.append(
                PlaylistItem(
                    path=f"share://track/{index}",
                    title=f"{track.artist} - {track.title}" if track.artist else track.title,
                    metadata=metadata,
                    artists=[ArtistInfo(track.artist, "", "", "")] if track.artist else [],
                    album=AlbumInfo(track.album, "", "", "") if track.album else None,
                )
            )
        return await self.mass.music.playlists.import_playlist(
            generate_m3u(payload.title, playlist_items),
            library_matching=True,
            match_policy=PlaylistMatchPolicy.SAME_RECORDING,
        )

    # ------------------------------------------------------------------
    # private helpers — payload building
    # ------------------------------------------------------------------

    async def _build_payload(self, item: MediaItemType) -> SharePayload:
        """Build a SharePayload from a MediaItem."""
        media_type = item.media_type.value
        artist_str: str | None = None
        album_str: str | None = None
        isrc: str | None = None
        mbid: str | None = None
        mb_track: str | None = None
        barcode: str | None = None
        art_ref: str | None = None
        provider_refs: dict[str, str] | None = None
        tracks: list[TrackStub] | None = None

        if item.media_type not in SUPPORTED_SHARE_MEDIA_TYPES:
            raise InvalidDataError(f"Sharing {media_type} items is not supported")

        if item.image and item.image.remotely_accessible and is_valid_artwork_url(item.image.path):
            art_ref = compact_artwork_ref(item.image.path)

        if item.media_type == MediaType.TRACK:
            isrc = item.get_external_id(ExternalID.ISRC)
            mbid = item.get_external_id(ExternalID.MB_RECORDING)
            mb_track = item.get_external_id(ExternalID.MB_TRACK)
            apple_music_track_id = next(
                (
                    mapping.item_id
                    for mapping in item.provider_mappings
                    if mapping.provider_domain == "apple_music" and mapping.item_id.isdecimal()
                ),
                None,
            )
            if hasattr(item, "artist_str"):
                artist_str = item.artist_str or None
            if hasattr(item, "album") and item.album:
                album_str = item.album.name
            provider_refs = build_provider_refs(item, media_type)
            await self._enrich_links_via_isrc(
                provider_refs, isrc, item.name, artist_str, apple_music_track_id
            )

        elif item.media_type == MediaType.ARTIST:
            mbid = item.get_external_id(ExternalID.MB_ARTIST)
            provider_refs = build_provider_refs(item, media_type)

        elif item.media_type == MediaType.ALBUM:
            mbid = item.get_external_id(ExternalID.MB_ALBUM)
            barcode = item.get_external_id(ExternalID.BARCODE)
            if hasattr(item, "artist_str"):
                artist_str = item.artist_str or None
            provider_refs = build_provider_refs(item, media_type)

        elif item.media_type == MediaType.PLAYLIST:
            tracks = await self._build_playlist_tracks(item)

        return SharePayload(
            v=2,
            type=cast("Literal['track', 'album', 'artist', 'playlist']", media_type),
            title=item.name,
            artist=artist_str,
            album=album_str,
            isrc=isrc,
            mbid=mbid,
            mb_track=mb_track,
            barcode=barcode,
            art_ref=art_ref,
            provider_refs=provider_refs or None,
            tracks=tracks,
        )

    async def _enrich_links_via_isrc(
        self,
        provider_refs: dict[str, str],
        isrc: str | None,
        title: str,
        artist: str | None,
        apple_music_track_id: str | None = None,
    ) -> None:
        """
        Add missing provider links using keyless public APIs and active MA providers.

        - Deezer: keyless ISRC lookup via public API.
        - Apple Music: keyless iTunes Search API (title + artist).
        - YouTube Music: via the active ytmusic MA provider if configured.

        Modifies ``provider_refs`` in place — only adds missing providers.
        """
        if not isrc and not title:
            return

        q = f"{artist} {title}" if artist else title

        if "deezer" not in provider_refs:
            deezer_link: str | None = None
            if isrc and is_valid_isrc(isrc):
                try:
                    deezer_link = await self._get_deezer_track_url(
                        normalize_external_id(ExternalID.ISRC, isrc)
                    )
                except (ClientError, TimeoutError, ValueError) as err:
                    LOGGER.debug("Could not look up Deezer track by ISRC: %s", err)
            if not deezer_link:
                search_query = f'track:"{title.replace(chr(34), "")}"'
                if artist:
                    safe_artist = artist.replace(chr(34), "")
                    search_query = f'artist:"{safe_artist}" {search_query}'
                try:
                    deezer_link = await self._search_deezer_track_url(search_query, title, artist)
                except (ClientError, TimeoutError, ValueError) as err:
                    LOGGER.debug("Could not enrich share link with Deezer search: %s", err)
            if deezer_link:
                provider_refs["deezer"] = compact_provider_ref("deezer", deezer_link)

        # Apple Music — exact catalog lookup first, then keyless text search
        if "apple_music" not in provider_refs:
            try:
                apple_link = None
                if apple_music_track_id:
                    apple_link = await self._get_apple_music_track_url_by_id(apple_music_track_id)
                if not apple_link:
                    apple_link = await self._get_apple_music_track_url(q, title, artist)
                if apple_link:
                    provider_refs["apple_music"] = compact_provider_ref("apple_music", apple_link)
            except (ClientError, TimeoutError, ValueError) as err:
                LOGGER.debug("Could not enrich share link with Apple Music: %s", err)

        # YouTube Music — via active ytmusic MA provider
        if "ytmusic" not in provider_refs:
            try:
                results = await self.mass.music.search(
                    q, [MediaType.TRACK], limit=3, providers=["ytmusic"]
                )
                for track in results.tracks or []:
                    if isinstance(track, ItemMapping):
                        continue
                    if not compare_strings(title, track.name, strict=False) or (
                        artist and not compare_strings(artist, track.artist_str, strict=False)
                    ):
                        continue
                    for mapping in track.provider_mappings:
                        if mapping.provider_domain == "ytmusic":
                            ytmusic_link = (
                                f"https://music.youtube.com/watch?v="
                                f"{quote(mapping.item_id, safe='')}"
                            )
                            provider_refs["ytmusic"] = compact_provider_ref("ytmusic", ytmusic_link)
                            break
                    if "ytmusic" in provider_refs:
                        break
            except PROVIDER_FETCH_ERRORS as err:
                LOGGER.debug("Could not enrich share link with YouTube Music: %s", err)

    async def _build_playlist_tracks(self, playlist: MediaItemType) -> list[TrackStub]:
        """Fetch and serialize playlist tracks as minimal stubs (no provider links)."""
        result: list[TrackStub] = []
        for track in await self.mass.music.playlists.tracks(
            playlist.item_id, playlist.provider, limit=0
        ):
            if not isinstance(track, Track):
                continue
            result.append(
                TrackStub(
                    title=track.name,
                    artist=getattr(track, "artist_str", None) or None,
                    isrc=track.get_external_id(ExternalID.ISRC),
                    mbid=track.get_external_id(ExternalID.MB_RECORDING),
                    mb_track=track.get_external_id(ExternalID.MB_TRACK),
                    album=track.album.name if track.album else None,
                    provider_refs=build_provider_refs(track, MediaType.TRACK.value),
                )
            )
        return result

    # ------------------------------------------------------------------
    # private helpers — resolution
    # ------------------------------------------------------------------

    async def _resolve_media_item(
        self, payload: SharePayload, allow_search: bool = True
    ) -> MediaItemType | None:
        """Resolve a shared track, album, or artist using known IDs before search."""
        media_type = MediaType(payload.type)
        external_ids: tuple[tuple[ExternalID, str | None], ...]
        if media_type == MediaType.TRACK:
            external_ids = (
                (ExternalID.ISRC, payload.isrc),
                (ExternalID.MB_RECORDING, payload.mbid),
                (ExternalID.MB_TRACK, payload.mb_track),
            )
        elif media_type == MediaType.ALBUM:
            external_ids = (
                (ExternalID.BARCODE, payload.barcode),
                (ExternalID.MB_ALBUM, payload.mbid),
            )
        else:
            external_ids = ((ExternalID.MB_ARTIST, payload.mbid),)

        controller = self.mass.music.get_controller(media_type)
        for external_id_type, external_id in external_ids:
            if not external_id:
                continue
            if external_id_type == ExternalID.ISRC and not is_valid_isrc(external_id):
                continue
            if external_id_type == ExternalID.BARCODE and not is_valid_barcode(external_id):
                continue
            try:
                try:
                    item = await controller.get_item_by_external_id(
                        normalize_external_id(external_id_type, external_id), external_id_type
                    )
                except PROVIDER_FETCH_ERRORS as err:
                    LOGGER.debug(
                        "External ID lookup failed for %s (%s): %s",
                        external_id_type,
                        external_id,
                        err,
                    )
                    continue
            except PROVIDER_FETCH_ERRORS as err:
                LOGGER.debug(
                    "External ID lookup failed for %s (%s): %s",
                    external_id_type,
                    external_id,
                    err,
                )
                continue
            if item is not None and not isinstance(item, ItemMapping):
                if media_type == MediaType.TRACK:
                    return await self.mass.music.tracks.get(
                        item.item_id, item.provider, allow_update_metadata=False
                    )
                return item

        if not allow_search:
            return None

        query = f"{payload.artist} {payload.title}" if payload.artist else payload.title
        results = await self.mass.music.search(query, media_types=[media_type], limit=5)
        if media_type == MediaType.TRACK:
            return next(
                (item for item in results.tracks or [] if not isinstance(item, ItemMapping)), None
            )
        if media_type == MediaType.ALBUM:
            return next(
                (item for item in results.albums or [] if not isinstance(item, ItemMapping)), None
            )
        return next(
            (item for item in results.artists or [] if not isinstance(item, ItemMapping)), None
        )

    @use_cache(3600 * 24 * 30)
    async def _get_deezer_track_url(self, isrc: str) -> str | None:
        """Return the public Deezer track URL for an ISRC, if available."""
        async with self.mass.http_session.get(
            f"https://api.deezer.com/track/isrc:{isrc}", timeout=ClientTimeout(total=4)
        ) as response:
            if response.status != 200:
                return None
            link = (await response.json()).get("link")
            return cast("str | None", link) if isinstance(link, str) else None

    @use_cache(3600 * 24 * 30)
    async def _search_deezer_track_url(
        self, query: str, title: str, artist: str | None
    ) -> str | None:
        """Find a Deezer track by title and artist when ISRC lookup misses."""
        async with self.mass.http_session.get(
            "https://api.deezer.com/search",
            params={"q": query, "limit": 5},
            timeout=ClientTimeout(total=4),
        ) as response:
            if response.status != 200:
                return None
            data = (await response.json()).get("data", [])
            return self._find_matching_track_url(data, title, artist)

    @use_cache(3600 * 24 * 30)
    async def _get_apple_music_track_url(
        self, query: str, title: str, artist: str | None
    ) -> str | None:
        """Return the public Apple Music track URL for a text query, if available."""
        async with self.mass.http_session.get(
            "https://itunes.apple.com/search",
            params={"term": query, "entity": "song", "limit": 5},
            timeout=ClientTimeout(total=4),
        ) as response:
            if response.status != 200:
                return None
            results = (await response.json()).get("results", [])
            return self._find_matching_track_url(results, title, artist)

    @use_cache(3600 * 24 * 30)
    async def _get_apple_music_track_url_by_id(self, track_id: str) -> str | None:
        """Return the canonical Apple Music URL for a catalog track ID."""
        async with self.mass.http_session.get(
            "https://itunes.apple.com/lookup",
            params={"id": track_id, "entity": "song", "limit": 1},
            timeout=ClientTimeout(total=4),
        ) as response:
            if response.status != 200:
                return None
            results = (await response.json()).get("results", [])
            for result in results:
                if (
                    isinstance(result, dict)
                    and result.get("kind") == "song"
                    and isinstance(result.get("trackViewUrl"), str)
                ):
                    return cast("str", result["trackViewUrl"])
        return None

    @staticmethod
    def _find_matching_track_url(results: object, title: str, artist: str | None) -> str | None:
        if not isinstance(results, list):
            return None
        best_score = (-1, -1)
        best_url: str | None = None
        for result in results:
            if not isinstance(result, dict):
                continue
            if result.get("kind") not in (None, "song"):
                continue
            result_title = result.get("trackName") or result.get("title")
            result_artist = result.get("artistName")
            if result_artist is None and isinstance(result.get("artist"), dict):
                result_artist = result["artist"].get("name")
            result_url = result.get("trackViewUrl") or result.get("link")
            if (
                not isinstance(result_title, str)
                or not isinstance(result_url, str)
                or not compare_strings(title, result_title, strict=False)
                or (artist and not isinstance(result_artist, str))
                or (
                    artist
                    and isinstance(result_artist, str)
                    and not compare_strings(artist, result_artist, strict=False)
                )
            ):
                continue
            score = (
                int(compare_strings(title, result_title, strict=True)),
                int(
                    not artist
                    or (
                        isinstance(result_artist, str)
                        and compare_strings(artist, result_artist, strict=True)
                    )
                ),
            )
            if score > best_score:
                best_score = score
                best_url = result_url
        return best_url
