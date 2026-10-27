"""Resolve share payloads to media items available in Music Assistant."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from music_assistant_models.enums import ExternalID, MediaType, ProviderType
from music_assistant_models.errors import InvalidDataError
from music_assistant_models.media_items import Album, Artist, ItemMapping, Track, UniqueList

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
)
from music_assistant.helpers.provider_access import visible_music_sources
from music_assistant.models.music_provider import PROVIDER_FETCH_ERRORS
from music_assistant.providers.share_provider.payload import SharePayload

if TYPE_CHECKING:
    from music_assistant_models.media_items import MediaItemType

    from music_assistant.mass import MusicAssistant

LOGGER = logging.getLogger(__name__)


class ShareLinkResolver:
    """Resolve shared media using external identifiers and Core matching."""

    def __init__(self, mass: MusicAssistant) -> None:
        """Initialize the resolver with the Music Assistant instance."""
        self.mass = mass

    async def resolve_media_item(
        self,
        payload: SharePayload,
        allow_search: bool = True,
        hydrate_track: bool = True,
        search_provider_instance_ids: set[str] | None = None,
    ) -> MediaItemType | None:
        """Resolve a shared track, album, or artist using known IDs before search."""
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
        """Build the metadata-backed Track used by share matching."""
        item = self._playlist_item_from_payload(payload)
        media_item = construct_media_item_from_playlist_item(
            item, self.mass, default_media_type=MediaType.TRACK
        )
        if not isinstance(media_item, Track):
            raise InvalidDataError("Invalid shared track payload")
        return media_item

    @staticmethod
    def _playlist_item_from_payload(
        payload: SharePayload, include_external_ids: bool = True
    ) -> PlaylistItem:
        """Convert a track share payload into the metadata form consumed by core."""
        metadata = {"media_type": MediaType.TRACK.value, "name": payload.title}
        if payload.version:
            metadata["version"] = payload.version
        if include_external_ids:
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
