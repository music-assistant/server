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

from music_assistant_models.auth import Scope
from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import (
    ConfigEntryType,
    ExternalID,
    MediaType,
    PlaylistMatchPolicy,
    ProviderType,
)
from music_assistant_models.errors import (
    InvalidDataError,
)
from music_assistant_models.media_items import (
    Album,
    Artist,
    BrowseFolder,
    ItemMapping,
    Track,
    UniqueList,
)

from music_assistant.controllers.webserver.helpers.auth_middleware import get_current_user
from music_assistant.helpers.compare import (
    TrackMatchConfidence,
    compare_album,
    compare_artist,
    compare_track_evidence,
    compare_track_title,
)
from music_assistant.helpers.external_ids import (
    is_valid_barcode,
    is_valid_isrc,
    normalize_external_id,
)
from music_assistant.helpers.playlists import (
    ArtistInfo,
    PlaylistItem,
    construct_media_item_from_playlist_item,
    generate_m3u,
)
from music_assistant.helpers.provider_access import visible_music_sources
from music_assistant.models.music_provider import PROVIDER_FETCH_ERRORS
from music_assistant.models.plugin import PluginProvider
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
# Provider links are optional, so third-party outages must not hold up share generation.
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
        search_provider_instance_ids = await self._searchable_music_provider_instances()
        semaphore = asyncio.Semaphore(2)
        resolved_by_payload: dict[str, asyncio.Task[Track | None]] = {}

        async def resolve_payload(track_payload: SharePayload) -> Track | None:
            async with semaphore:
                try:
                    item = await self._resolve_media_item(
                        track_payload,
                        hydrate_track=False,
                        search_provider_instance_ids=search_provider_instance_ids,
                    )
                except PROVIDER_FETCH_ERRORS as err:
                    LOGGER.debug(
                        "Could not resolve shared preview track %s: %s",
                        track_payload.title,
                        err,
                    )
                    return None
            return item if isinstance(item, Track) else None

        async def resolve_track(index: int, stub: TrackStub) -> Track:
            track_payload = SharePayload(
                type="track",
                title=stub.title,
                artist=stub.artist,
                artists=stub.artists,
                version=stub.version,
                album=stub.album,
                isrc=stub.isrc,
                mbid=stub.mbid,
                mb_track=stub.mb_track,
                duration=stub.duration,
                provider_refs=stub.provider_refs,
            )
            encoded_track = track_payload.to_base64()
            task = resolved_by_payload.get(encoded_track)
            if task is None:
                task = asyncio.create_task(resolve_payload(track_payload))
                resolved_by_payload[encoded_track] = task
            item = await task
            if item is not None:
                return replace(item, position=index + 1)
            preview = construct_media_item_from_playlist_item(
                self._playlist_item_from_payload(track_payload), self.mass
            )
            if not isinstance(preview, Track):
                raise InvalidDataError("Invalid shared track preview")
            if track_payload.album:
                preview.album = ItemMapping(
                    item_id=track_payload.album,
                    provider=self.instance_id,
                    name=track_payload.album,
                    media_type=MediaType.ALBUM,
                )
            return replace(
                preview,
                item_id=f"share-preview://track/{encoded_track}",
                provider="builtin",
                uri=None,
                provider_mappings=set(),
                position=index + 1,
            )

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
                artists=track.artists,
                version=track.version,
                album=track.album,
                isrc=track.isrc,
                mbid=track.mbid,
                mb_track=track.mb_track,
                duration=track.duration,
                provider_refs=track.provider_refs,
            )
            playlist_items.append(self._playlist_item_from_payload(track_payload))
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
        artist_credits: list[str] | None = None
        version_str: str | None = None
        album_str: str | None = None
        isrc: str | None = None
        mbid: str | None = None
        mb_track: str | None = None
        barcode: str | None = None
        art_ref: str | None = None
        duration: int | None = None
        provider_refs: dict[str, str] | None = None
        tracks: list[TrackStub] | None = None

        if item.media_type not in SUPPORTED_SHARE_MEDIA_TYPES:
            raise InvalidDataError(f"Sharing {media_type} items is not supported")

        if item.image and item.image.remotely_accessible and is_valid_artwork_url(item.image.path):
            art_ref = compact_artwork_ref(item.image.path)

        if item.media_type == MediaType.TRACK:
            version_str = getattr(item, "version", None) or None
            if isinstance(item, Track):
                duration = item.duration
                artist_credits = [artist.name for artist in item.artists] or None
                artist_str = ", ".join(artist_credits) if artist_credits else None
            isrc = item.get_external_id(ExternalID.ISRC)
            mbid = item.get_external_id(ExternalID.MB_RECORDING)
            mb_track = item.get_external_id(ExternalID.MB_TRACK)
            if hasattr(item, "album") and item.album:
                album_str = item.album.name
            provider_refs = build_provider_refs(item)

        elif item.media_type == MediaType.ARTIST:
            mbid = item.get_external_id(ExternalID.MB_ARTIST)
            provider_refs = build_provider_refs(item)

        elif item.media_type == MediaType.ALBUM:
            version_str = getattr(item, "version", None) or None
            mbid = item.get_external_id(ExternalID.MB_ALBUM)
            barcode = item.get_external_id(ExternalID.BARCODE)
            if isinstance(item, Album):
                artist_credits = [artist.name for artist in item.artists] or None
                artist_str = ", ".join(artist_credits) if artist_credits else None
            provider_refs = build_provider_refs(item)

        elif item.media_type == MediaType.PLAYLIST:
            tracks = await self._build_playlist_tracks(item)

        return SharePayload(
            type=cast("Literal['track', 'album', 'artist', 'playlist']", media_type),
            title=item.name,
            artist=artist_str,
            artists=artist_credits,
            version=version_str,
            album=album_str,
            isrc=isrc,
            mbid=mbid,
            mb_track=mb_track,
            barcode=barcode,
            art_ref=art_ref,
            provider_refs=provider_refs or None,
            tracks=tracks,
            duration=duration,
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
                    artist=", ".join(artist.name for artist in track.artists) or None,
                    artists=[artist.name for artist in track.artists] or None,
                    version=track.version or None,
                    isrc=track.get_external_id(ExternalID.ISRC),
                    mbid=track.get_external_id(ExternalID.MB_RECORDING),
                    mb_track=track.get_external_id(ExternalID.MB_TRACK),
                    album=track.album.name if track.album else None,
                    duration=track.duration,
                    provider_refs=build_provider_refs(track),
                )
            )
        return result

    # ------------------------------------------------------------------
    # private helpers — resolution
    # ------------------------------------------------------------------

    async def _resolve_media_item(
        self,
        payload: SharePayload,
        allow_search: bool = True,
        hydrate_track: bool = True,
        search_provider_instance_ids: set[str] | None = None,
    ) -> MediaItemType | None:
        """
        Resolve a shared track, album, or artist using known IDs before search.

        :param hydrate_track: Fetch full details for track results.
        """
        media_type = MediaType(payload.type)
        item = await self._resolve_by_external_ids(payload, media_type, hydrate_track)
        if item is not None or not allow_search:
            return item
        if media_type == MediaType.TRACK:
            return await self._resolve_track_with_core(payload, search_provider_instance_ids)
        return await self._resolve_by_search(payload, media_type)

    async def _resolve_by_external_ids(
        self, payload: SharePayload, media_type: MediaType, hydrate_track: bool
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
        track_reference = (
            self._track_from_payload(payload) if media_type == MediaType.TRACK else None
        )
        album_reference = self._album_reference(payload) if media_type == MediaType.ALBUM else None
        artist_reference = (
            self._artist_reference(payload) if media_type == MediaType.ARTIST else None
        )
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
                if media_type == MediaType.ALBUM and (
                    not isinstance(item, Album)
                    or album_reference is None
                    or not self._matches_album_reference(payload, album_reference, item)
                ):
                    continue
                if media_type == MediaType.TRACK:
                    if (
                        not isinstance(item, Track)
                        or track_reference is None
                        or compare_track_evidence(track_reference, item)
                        < TrackMatchConfidence.LIKELY
                        or (
                            external_id_type == ExternalID.ISRC
                            and not self._isrc_candidate_corroborated(track_reference, item)
                        )
                    ):
                        continue
                    if not hydrate_track:
                        return item
                    try:
                        return await self.mass.music.tracks.get(
                            item.item_id, item.provider, allow_update_metadata=False
                        )
                    except PROVIDER_FETCH_ERRORS as err:
                        LOGGER.debug("Could not load details for shared track: %s", err)
                        continue
                if media_type == MediaType.ARTIST and (
                    not isinstance(item, Artist)
                    or artist_reference is None
                    or compare_artist(artist_reference, item) is not True
                ):
                    continue
                return item
        return None

    async def _resolve_by_search(
        self, payload: SharePayload, media_type: MediaType
    ) -> MediaItemType | None:
        query = f"{payload.artist} {payload.title}" if payload.artist else payload.title
        results = await self.mass.music.search(query, media_types=[media_type], limit=5)
        if media_type == MediaType.ALBUM:
            album_reference = self._album_reference(payload)
            album_matches = [
                item
                for item in results.albums or []
                if isinstance(item, Album)
                and self._matches_album_reference(payload, album_reference, item)
            ]
            return album_matches[0] if len(album_matches) == 1 else None
        artist_reference = self._artist_reference(payload)
        artist_matches = [
            item
            for item in results.artists or []
            if isinstance(item, Artist) and compare_artist(artist_reference, item) is True
        ]
        return artist_matches[0] if len(artist_matches) == 1 else None

    async def _resolve_track_with_core(
        self,
        payload: SharePayload,
        provider_instance_ids: set[str] | None = None,
    ) -> Track | None:
        """Resolve a shared track through the core provider matcher."""
        if provider_instance_ids is None:
            provider_instance_ids = await self._searchable_music_provider_instances()
        shared_track = self._track_from_payload(payload)
        enrichment = await self.mass.music.tracks.enrich_provider_mappings(
            shared_track,
            minimum_confidence=TrackMatchConfidence.LIKELY,
            provider_instance_ids=provider_instance_ids,
            search_non_streaming_without_mapping=True,
        )
        matches = [
            match
            for match in enrichment.matches
            if match.confidence >= TrackMatchConfidence.LIKELY
            and (not payload.isrc or self._isrc_candidate_corroborated(shared_track, match.track))
        ]
        if not matches:
            return None
        best_confidence = max(match.confidence for match in matches)
        best_matches = [match for match in matches if match.confidence == best_confidence]
        primary_match = min(
            best_matches,
            key=lambda match: (
                not match.mapping.available,
                -match.mapping.quality,
                match.mapping.provider_domain,
                match.mapping.provider_instance,
                match.mapping.item_id,
            ),
        )
        return primary_match.track

    @staticmethod
    def _isrc_candidate_corroborated(base_track: Track, candidate: Track) -> bool:
        """Require title and all credited artists to confirm an ISRC hit."""
        if not compare_track_title(base_track.name, candidate.name):
            return False
        unmatched_candidates = list(candidate.artists)
        if not base_track.artists or len(base_track.artists) != len(unmatched_candidates):
            return False
        for base_artist in base_track.artists:
            matching_index = next(
                (
                    index
                    for index, candidate_artist in enumerate(unmatched_candidates)
                    if compare_artist(base_artist, candidate_artist) is True
                ),
                None,
            )
            if matching_index is None:
                return False
            unmatched_candidates.pop(matching_index)
        return not unmatched_candidates

    async def _searchable_music_provider_instances(self) -> set[str]:
        """Return enabled, user-visible music providers that can search catalogs."""
        user = get_current_user()
        visible_sources = visible_music_sources(self.mass, user) if user else None
        provider_configs = await self.mass.config.get_provider_configs(
            provider_type=ProviderType.MUSIC
        )
        return {
            config.instance_id
            for config in provider_configs
            if config.enabled
            and config.domain != "builtin"
            and (visible_sources is None or config.instance_id in visible_sources)
        }

    def _track_from_payload(self, payload: SharePayload) -> Track:
        """Build the same metadata-backed Track shape used by playlist imports."""
        item = self._playlist_item_from_payload(payload)
        media_item = construct_media_item_from_playlist_item(
            item, self.mass, default_media_type=MediaType.TRACK
        )
        if not isinstance(media_item, Track):
            raise InvalidDataError("Invalid shared track payload")
        return media_item

    @staticmethod
    def _playlist_item_from_payload(payload: SharePayload) -> PlaylistItem:
        """Convert a track share payload into the metadata form consumed by core."""
        metadata = {"media_type": MediaType.TRACK.value, "name": payload.title}
        if payload.version:
            metadata["version"] = payload.version
        if payload.isrc and is_valid_isrc(payload.isrc):
            metadata["isrc"] = normalize_external_id(ExternalID.ISRC, payload.isrc)
        if payload.mbid:
            metadata["mbid"] = normalize_external_id(ExternalID.MB_RECORDING, payload.mbid)
        if payload.mb_track:
            metadata["mb_track"] = normalize_external_id(ExternalID.MB_TRACK, payload.mb_track)
        return PlaylistItem(
            path=f"share://track/{payload.to_base64()}",
            length=str(payload.duration) if payload.duration is not None else None,
            title=(f"{payload.artist} - {payload.title}" if payload.artist else payload.title),
            metadata=metadata,
            artists=[
                ArtistInfo(artist, "", "", "")
                for artist in (payload.artists or ([payload.artist] if payload.artist else []))
            ],
            album=None,
        )

    @staticmethod
    def _album_reference(payload: SharePayload) -> Album | ItemMapping:
        external_ids = set()
        artist_names = payload.artists or ([payload.artist] if payload.artist else [])
        if payload.mbid:
            external_ids.add(
                (ExternalID.MB_ALBUM, normalize_external_id(ExternalID.MB_ALBUM, payload.mbid))
            )
        if payload.barcode and is_valid_barcode(payload.barcode):
            external_ids.add(
                (ExternalID.BARCODE, normalize_external_id(ExternalID.BARCODE, payload.barcode))
            )
        if not artist_names:
            return ItemMapping(
                item_id="share-payload",
                provider="share_provider",
                name=payload.title,
                version=payload.version or "",
                media_type=MediaType.ALBUM,
                external_ids=external_ids,
            )
        return Album(
            item_id="share-payload",
            provider="share_provider",
            name=payload.title,
            version=payload.version or "",
            artists=UniqueList(
                [
                    ItemMapping(
                        item_id=artist_name,
                        provider="share_provider",
                        name=artist_name,
                        media_type=MediaType.ARTIST,
                    )
                    for artist_name in artist_names
                ]
            ),
            external_ids=external_ids,
            provider_mappings=set(),
        )

    @staticmethod
    def _matches_album_reference(
        payload: SharePayload,
        reference: Album | ItemMapping,
        candidate: Album | ItemMapping,
    ) -> bool:
        return (
            compare_album(
                reference,
                candidate,
                strict=bool(
                    (payload.artists or payload.artist)
                    and isinstance(reference, Album)
                    and isinstance(candidate, Album)
                ),
            )
            is True
        )

    @staticmethod
    def _artist_reference(payload: SharePayload) -> ItemMapping:
        external_ids = (
            {(ExternalID.MB_ARTIST, normalize_external_id(ExternalID.MB_ARTIST, payload.mbid))}
            if payload.mbid
            else set()
        )
        return ItemMapping(
            item_id="share-payload",
            provider="share_provider",
            name=payload.title,
            media_type=MediaType.ARTIST,
            external_ids=external_ids,
        )
