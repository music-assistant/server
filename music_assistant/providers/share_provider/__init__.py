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
from collections.abc import Callable, Sequence
from dataclasses import replace
from typing import TYPE_CHECKING, Literal, cast

from music_assistant_models.auth import Scope
from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import (
    ConfigEntryType,
    ExternalID,
    MediaType,
    PlaylistMatchPolicy,
)
from music_assistant_models.errors import (
    InvalidDataError,
)
from music_assistant_models.media_items import Album, BrowseFolder, ItemMapping, Track

from music_assistant.helpers.compare import compare_strings, compare_version
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
from music_assistant.providers.share_provider.enrichment import ShareLinkEnricher
from music_assistant.providers.share_provider.links import (
    build_provider_refs,
    compact_artwork_ref,
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
# Leave room for the API command envelope under the 64 KiB fallback DataChannel limit.
MAX_SHARE_PLAYLIST_PAYLOAD_SIZE = 48 * 1024
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
    _link_enricher: ShareLinkEnricher

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
        self._link_enricher = ShareLinkEnricher(self.mass, self.instance_id)
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
        encoded_payload = payload.to_base64()
        if payload.type == "playlist" and len(encoded_payload) > MAX_SHARE_PLAYLIST_PAYLOAD_SIZE:
            raise InvalidDataError(
                "This playlist is too large to share.",
                translation_key="share_playlist_too_large",
                translation_owner=self.translation_owner,
            )
        base_url = self.config.get_value(CONF_SHARE_BASE_URL) or DEFAULT_SHARE_BASE_URL
        return f"{base_url}/#{encoded_payload}"

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
                    type="track",
                    title=stub.title,
                    artist=stub.artist,
                    version=stub.version,
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
                        path=f"share-preview://track/{track_payload.to_base64()}",
                        length=(str(stub.duration) if stub.duration is not None else None),
                        title=stub.title,
                        metadata={
                            "media_type": "track",
                            "name": stub.title,
                            **({"version": stub.version} if stub.version else {}),
                        },
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
        for track in payload.tracks:
            track_payload = SharePayload(
                type="track",
                title=track.title,
                artist=track.artist,
                version=track.version,
                album=track.album,
                isrc=track.isrc,
                mbid=track.mbid,
                mb_track=track.mb_track,
                provider_refs=track.provider_refs,
            )
            metadata = {"media_type": MediaType.TRACK.value, "name": track.title}
            if track.version:
                metadata["version"] = track.version
            if track.isrc and is_valid_isrc(track.isrc):
                metadata["isrc"] = normalize_external_id(ExternalID.ISRC, track.isrc)
            if track.mbid:
                metadata["mbid"] = normalize_external_id(ExternalID.MB_RECORDING, track.mbid)
            if track.mb_track:
                metadata["mb_track"] = normalize_external_id(ExternalID.MB_TRACK, track.mb_track)
            playlist_items.append(
                PlaylistItem(
                    path=f"share://track/{track_payload.to_base64()}",
                    length=(str(track.duration) if track.duration is not None else None),
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
        version_str: str | None = None
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
            version_str = getattr(item, "version", None) or None
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
            await self._link_enricher.enrich(
                provider_refs,
                isrc,
                item.name,
                artist_str,
                apple_music_track_id,
                version=version_str,
            )

        elif item.media_type == MediaType.ARTIST:
            mbid = item.get_external_id(ExternalID.MB_ARTIST)
            provider_refs = build_provider_refs(item, media_type)

        elif item.media_type == MediaType.ALBUM:
            version_str = getattr(item, "version", None) or None
            mbid = item.get_external_id(ExternalID.MB_ALBUM)
            barcode = item.get_external_id(ExternalID.BARCODE)
            if hasattr(item, "artist_str"):
                artist_str = item.artist_str or None
            provider_refs = build_provider_refs(item, media_type)

        elif item.media_type == MediaType.PLAYLIST:
            tracks = await self._build_playlist_tracks(item)

        return SharePayload(
            type=cast("Literal['track', 'album', 'artist', 'playlist']", media_type),
            title=item.name,
            artist=artist_str,
            version=version_str,
            album=album_str,
            isrc=isrc,
            mbid=mbid,
            mb_track=mb_track,
            barcode=barcode,
            art_ref=art_ref,
            provider_refs=provider_refs or None,
            tracks=tracks,
        )

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
                    artist=(
                        ", ".join(artist.name for artist in track.artists)
                        if track.artists
                        else None
                    ),
                    version=track.version or None,
                    isrc=track.get_external_id(ExternalID.ISRC),
                    mbid=track.get_external_id(ExternalID.MB_RECORDING),
                    mb_track=track.get_external_id(ExternalID.MB_TRACK),
                    album=track.album.name if track.album else None,
                    duration=track.duration,
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
        item = await self._resolve_by_external_ids(payload, media_type)
        if item is not None or not allow_search:
            return item
        return await self._resolve_by_search(payload, media_type)

    async def _resolve_by_external_ids(
        self, payload: SharePayload, media_type: MediaType
    ) -> MediaItemType | None:
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
            if item is not None and not isinstance(item, ItemMapping):
                if media_type == MediaType.TRACK:
                    item_version = item.version if isinstance(item.version, str) else ""
                    if external_id_type != ExternalID.MB_TRACK and not compare_version(
                        payload.version or "", item_version
                    ):
                        continue
                    try:
                        return await self.mass.music.tracks.get(
                            item.item_id, item.provider, allow_update_metadata=False
                        )
                    except PROVIDER_FETCH_ERRORS as err:
                        LOGGER.debug("Could not load details for shared track: %s", err)
                        continue
                return item
        return None

    async def _resolve_by_search(
        self, payload: SharePayload, media_type: MediaType
    ) -> MediaItemType | None:
        query = f"{payload.artist} {payload.title}" if payload.artist else payload.title
        results = await self.mass.music.search(query, media_types=[media_type], limit=5)
        if media_type == MediaType.TRACK:
            return self._best_search_match(payload, results.tracks or [])
        if media_type == MediaType.ALBUM:
            return self._best_search_match(payload, results.albums or [])
        return next(
            (
                item
                for item in results.artists or []
                if not isinstance(item, ItemMapping)
                and compare_strings(payload.title, item.name, strict=False)
            ),
            None,
        )

    @staticmethod
    def _best_search_match(
        payload: SharePayload,
        candidates: Sequence[Track | Album | ItemMapping],
    ) -> Track | Album | None:
        best_score = (-1, -1, -1)
        best_item: Track | Album | None = None
        for item in candidates:
            if isinstance(item, ItemMapping):
                continue
            if not compare_strings(payload.title, item.name, strict=False):
                continue
            if payload.artist and not item.artist_str:
                continue
            if payload.artist and not (
                compare_strings(payload.artist, item.artist_str, strict=True)
                or compare_strings(payload.artist, item.artist_str, strict=False)
            ):
                continue
            is_track = payload.type == MediaType.TRACK.value
            item_version = item.version if isinstance(item.version, str) else ""
            if is_track:
                if not compare_version(payload.version or "", item_version):
                    continue
            elif (
                payload.version
                and item.version
                and not compare_version(payload.version, item.version)
            ):
                continue
            version_matches = (
                compare_version(payload.version or "", item_version)
                if is_track
                else not payload.version
                or bool(item.version and compare_version(payload.version or "", item_version))
            )
            score = (
                int(compare_strings(payload.title, item.name, strict=True)),
                int(
                    not payload.artist
                    or compare_strings(payload.artist, item.artist_str, strict=True)
                ),
                int(version_matches),
            )
            if score > best_score:
                best_score = score
                best_item = item
        return best_item
