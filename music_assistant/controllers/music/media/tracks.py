"""Manage MediaItems of type Track."""

from __future__ import annotations

from collections.abc import Iterable
from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Never, cast

from aiohttp import ClientError
from music_assistant_models.auth import Scope
from music_assistant_models.enums import (
    ExternalID,
    ImageType,
    MediaType,
    ProviderFeature,
    ProviderType,
)
from music_assistant_models.errors import (
    InvalidDataError,
    MediaNotFoundError,
    MusicAssistantError,
    ProviderUnavailableError,
    UnsupportedFeaturedException,
)
from music_assistant_models.helpers import create_safe_string
from music_assistant_models.media_items import (
    Album,
    Artist,
    ItemMapping,
    ItemMappingSummary,
    MediaItemImage,
    ProviderMapping,
    Track,
    TrackSummary,
    UniqueList,
)

from music_assistant.constants import (
    DB_TABLE_ALBUM_TRACKS,
    DB_TABLE_ALBUMS,
    DB_TABLE_TRACK_ARTISTS,
    DB_TABLE_TRACKS,
)
from music_assistant.controllers.music.helpers import (
    provider_mappings_for_update,
    search_name_match_clause,
)
from music_assistant.helpers.compare import (
    TrackMatchConfidence,
    compare_artists,
    compare_media_item,
    compare_track,
    compare_track_evidence,
    compare_track_title,
    loose_compare_strings,
)
from music_assistant.helpers.database import UNSET
from music_assistant.helpers.json import json_loads, serialize_to_json
from music_assistant.helpers.lyrics import extract_lrc_lyrics, normalize_lrc_lyrics
from music_assistant.models.music_provider import MusicProvider

from .base import MediaControllerBase, TrackSyncDetails

if TYPE_CHECKING:
    from collections.abc import Mapping

    from music_assistant import MusicAssistant
    from music_assistant.models.metadata_provider import MetadataProvider
    from music_assistant.models.plugin import PluginProvider


@dataclass(frozen=True, slots=True)
class TrackProviderMatch:
    """Matched provider track with its confidence and target mapping."""

    track: Track
    mapping: ProviderMapping
    confidence: TrackMatchConfidence


@dataclass(frozen=True, slots=True)
class TrackProviderMatchResult:
    """Result of resolving a track on one provider."""

    match: TrackProviderMatch | None = None
    ambiguous: bool = False


@dataclass(frozen=True, slots=True)
class TrackProviderEnrichment:
    """Track enriched with provider matches without changing the library."""

    track: Track
    matches: tuple[TrackProviderMatch, ...]
    ambiguous_providers: tuple[str, ...]
    failed_providers: tuple[str, ...]
    used_library_item: bool


class TracksController(MediaControllerBase[Track]):
    """Controller managing MediaItems of type Track."""

    db_table = DB_TABLE_TRACKS
    media_type = MediaType.TRACK
    item_cls = Track
    summary_item_cls = TrackSummary

    def __init__(self, mass: MusicAssistant) -> None:
        """Initialize class."""
        super().__init__(mass)
        # register (extra) api handlers
        api_base = self.api_base
        self.mass.register_api_command(
            f"music/{api_base}/track_versions", self.versions, required_scope=Scope.LIBRARY_READ
        )
        self.mass.register_api_command(
            f"music/{api_base}/track_albums", self.albums, required_scope=Scope.LIBRARY_READ
        )
        self.mass.register_api_command(
            f"music/{api_base}/preview", self.get_preview_url, required_scope=Scope.LIBRARY_READ
        )
        self.mass.register_api_command(
            f"music/{api_base}/similar_tracks",
            self.similar_tracks,
            required_scope=Scope.LIBRARY_READ,
        )

    @property
    def base_query(self) -> tuple[str, dict[str, Any]]:
        """Return the base SELECT query for tracks and its bound query params."""
        # NOTE: the track_album subquery is fully self-contained (correlated) so the
        # outer query needs no join with album_tracks (which would fan out rows for
        # tracks that appear on multiple albums and force a GROUP BY). For tracks on
        # multiple albums it prefers :preferred_album_id (used for album track
        # listings) and otherwise deterministically picks the lowest album id.
        query = f"""
        SELECT
            tracks.*,
            {self._external_ids_query()} AS external_ids,
            {self._provider_mappings_query()} AS provider_mappings,

            (SELECT JSON_GROUP_ARRAY(
                json_object(
                'item_id', artists.item_id,
                'provider', 'library',
                    'name', artists.name,
                    'sort_name', artists.sort_name,
                    'media_type', 'artist',
                    'external_ids', json({self._external_ids_query(MediaType.ARTIST, "artists")})
                )) FROM artists JOIN track_artists on track_artists.track_id = tracks.item_id  WHERE artists.item_id = track_artists.artist_id) AS artists,
            (SELECT
                json_object(
                'item_id', albums.item_id,
                'provider', 'library',
                    'name', albums.name,
                    'sort_name', albums.sort_name,
                    'media_type', 'album',
                    'year', albums.year,
                    'disc_number', album_tracks.disc_number,
                    'track_number', album_tracks.track_number,
                    'images', json_extract(albums.metadata, '$.images')
                ) FROM album_tracks
                JOIN albums ON albums.item_id = album_tracks.album_id
                WHERE album_tracks.track_id = tracks.item_id
                ORDER BY (album_tracks.album_id IS :preferred_album_id) DESC, album_tracks.album_id
                LIMIT 1) AS track_album
            FROM tracks
            """
        return query, {"preferred_album_id": None}

    @property
    def summary_query(self) -> tuple[str, dict[str, Any]]:
        """Return the slim SELECT query used for track summary listings."""
        # the track_album subquery follows the same correlated pattern as in base_query
        # (see the NOTE there), just with the few fields a list row needs
        query = f"""
        SELECT
            {self._summary_base_columns()},
            tracks.version,
            tracks.duration,
            json_extract(tracks.metadata, '$.explicit') AS explicit,
            json_extract(tracks.metadata, '$.release_date') AS release_date,
            {self._provider_mappings_query()} AS provider_mappings,
            {self._artist_mappings_summary_query(DB_TABLE_TRACK_ARTISTS, "track_id")} AS artists,
            (SELECT
                json_object(
                'item_id', albums.item_id,
                    'name', albums.name,
                    'sort_name', albums.sort_name,
                    'year', albums.year,
                    'disc_number', album_tracks.disc_number,
                    'track_number', album_tracks.track_number,
                    'images', json_extract(albums.metadata, '$.images')
                ) FROM album_tracks
                JOIN albums ON albums.item_id = album_tracks.album_id
                WHERE album_tracks.track_id = tracks.item_id
                ORDER BY (album_tracks.album_id IS :preferred_album_id) DESC, album_tracks.album_id
                LIMIT 1) AS track_album
            FROM tracks
            """
        return query, {"preferred_album_id": None}

    async def get(
        self,
        item_id: str,
        provider_instance_id_or_domain: str,
        allow_update_metadata: bool = True,
        recursive: bool = True,
        album_uri: str | None = None,
    ) -> Track:
        """Return (full) details for a single media item."""
        track = await super().get(
            item_id,
            provider_instance_id_or_domain,
            allow_update_metadata=allow_update_metadata,
        )
        track.audio_metadata = await self.mass.streams.audio_analysis.get_track_audio_metadata(
            track
        )
        if not recursive and album_uri is None:
            # return early if we do not want recursive full details and no album uri is provided
            return track

        # append full album details to full track item (resolve ItemMappings)
        try:
            if album_uri:
                item = await self.mass.music.get_item_by_uri(album_uri, allow_update_metadata=False)
                if isinstance(item, Album):
                    track.album = item
            elif provider_instance_id_or_domain == "library":
                # grab the first album this track is attached to
                for album_track_row in await self.mass.music.database.get_rows(
                    DB_TABLE_ALBUM_TRACKS, {"track_id": int(item_id)}, limit=1
                ):
                    track.album = await self.mass.music.albums.get_library_item(
                        album_track_row["album_id"]
                    )
            elif isinstance(track.album, ItemMapping) or (track.album and not track.album.image):
                track.album = await self.mass.music.albums.get(
                    track.album.item_id,
                    track.album.provider,
                    allow_update_metadata=False,
                    recursive=False,
                )
        except MusicAssistantError as err:
            # edge case where playlist track has invalid albumdetails
            self.logger.warning("Unable to fetch album details for %s - %s", track.uri, str(err))

        if not recursive:
            return track

        # append artist details to full track item (resolve ItemMappings)
        track_artists = []
        for artist in track.artists:
            if not isinstance(artist, ItemMapping):
                track_artists.append(artist)
                continue
            try:
                track_artists.append(
                    await self.mass.music.artists.get(
                        artist.item_id,
                        artist.provider,
                        allow_update_metadata=False,
                    )
                )
            except MusicAssistantError as err:
                # edge case where playlist track has invalid artistdetails
                self.logger.warning("Unable to fetch artist details %s - %s", artist.uri, str(err))
        track.artists = UniqueList(track_artists)
        return track

    async def library_items(  # noqa: PLR0913
        self,
        favorite: bool | None = None,
        search: str | None = None,
        limit: int = 500,
        offset: int = 0,
        order_by: str = "sort_name",
        provider: str | list[str] | None = None,
        genre: int | list[int] | None = None,
        played_only: bool = False,
        explicit: bool | None = None,
        *,
        summary: bool = True,
        reachable_via: list[str] | None = None,
        **kwargs: Any,
    ) -> list[Track]:
        """
        Get in-database tracks.

        :param favorite: Filter by favorite status.
        :param search: Filter by search query.
        :param limit: Maximum number of items to return.
        :param offset: Number of items to skip.
        :param order_by: Order by field (e.g. 'sort_name', 'timestamp_added').
        :param provider: Filter by provider instance ID (single string or list).
        :param genre: Filter by genre id(s).
        :param played_only: Filter to only played tracks.
        :param explicit: Filter by explicit content (True=only explicit, False=no explicit, None=all).
        :param summary: When True (default), return slim summary items containing only the
            fields needed for a list view. Set to False to get fully hydrated items.
        :param reachable_via: Restrict results to items with a provider mapping reachable
            through one of these provider instance ids (OR semantics). See
            `MediaControllerBase.library_items` for the full semantics.
        """
        reachable_via = self._resolve_reachable_via(reachable_via)
        if reachable_via is not None and not reachable_via:
            return []
        extra_query_params: dict[str, Any] = {}
        extra_query_parts: list[str] = []
        extra_join_parts: list[str] = []

        # Apply explicit content filter
        if explicit is not None:
            if explicit:
                # Only explicit tracks
                extra_query_parts.append("json_extract(tracks.metadata, '$.explicit') = 1")
            else:
                # No explicit tracks (null or false)
                extra_query_parts.append(
                    "(json_extract(tracks.metadata, '$.explicit') IS NULL "
                    "OR json_extract(tracks.metadata, '$.explicit') = 0)"
                )

        if (order_by and "track_artist_name" in order_by) or (search and " - " in search):
            extra_join_parts.append(
                "JOIN track_artists ON track_artists.track_id = tracks.item_id "
                "JOIN artists ON artists.item_id = track_artists.artist_id "
            )

        if search and " - " in search:
            # handle combined artist + title search
            artist_str, title_str = search.split(" - ", 1)
            search = None
            title_str = create_safe_string(title_str, True, True)
            artist_str = create_safe_string(artist_str, True, True)
            extra_query_parts.append(
                search_name_match_clause("tracks", title_str, "search_title", extra_query_params)
            )
            extra_query_parts.append(
                search_name_match_clause("artists", artist_str, "search_artist", extra_query_params)
            )
        result = await self.get_library_items_by_query(
            favorite=favorite,
            search=search,
            genre_ids=genre,
            limit=limit,
            offset=offset,
            order_by=order_by,
            provider_filter=self._provider_filter_considering_reachability(provider, reachable_via),
            extra_query_parts=extra_query_parts,
            extra_query_params=extra_query_params,
            extra_join_parts=extra_join_parts,
            played_only=played_only,
            in_library_only=True,
            summary=summary,
            reachable_via=reachable_via,
        )
        if search and len(result) < 25 and not offset:
            # append artist items to result
            artist_search_str = create_safe_string(search, True, True)
            if order_by and "track_artist_name" in order_by:
                # JOIN already exists for sorting, only add WHERE clause
                extra_query_parts.append(
                    search_name_match_clause(
                        "artists", artist_search_str, "search_artist", extra_query_params
                    )
                )
            else:
                # JOIN not yet added, add it with the search condition
                extra_join_parts.append(
                    "JOIN track_artists ON track_artists.track_id = tracks.item_id "
                    "JOIN artists ON artists.item_id = track_artists.artist_id "
                    "AND "
                    + search_name_match_clause(
                        "artists", artist_search_str, "search_artist", extra_query_params
                    )
                )
            existing_uris = {item.uri for item in result}
            for _track in await self.get_library_items_by_query(
                favorite=favorite,
                search=None,
                genre_ids=genre,
                limit=limit,
                order_by=order_by,
                provider_filter=self._provider_filter_considering_reachability(
                    provider, reachable_via
                ),
                extra_query_parts=extra_query_parts,
                extra_query_params=extra_query_params,
                extra_join_parts=extra_join_parts,
                in_library_only=True,
                summary=summary,
                reachable_via=reachable_via,
            ):
                # prevent duplicates (when artist is also in the title)
                if _track.uri not in existing_uris:
                    result.append(_track)
        return result

    async def versions(
        self,
        item_id: str,
        provider_instance_id_or_domain: str,
    ) -> UniqueList[Track]:
        """Return all versions of a track we can find on all providers."""
        track = await self.get(item_id, provider_instance_id_or_domain)
        search_query = f"{track.artist_str} - {track.name}"
        result: UniqueList[Track] = UniqueList()
        for provider_id in self.mass.music.get_unique_providers():
            provider = self.mass.get_provider(provider_id)
            if not isinstance(provider, MusicProvider):
                continue
            if MediaType.TRACK not in provider.supported_media_types:
                continue
            result.extend(
                prov_item
                for prov_item in await self.search(search_query, provider_id)
                if loose_compare_strings(track.name, prov_item.name)
                and compare_artists(prov_item.artists, track.artists, any_match=True)
                # make sure that the 'base' version is NOT included
                and not track.provider_mappings.intersection(prov_item.provider_mappings)
            )
        return result

    async def albums(
        self,
        item_id: str,
        provider_instance_id_or_domain: str,
        in_library_only: bool = False,
    ) -> UniqueList[Album]:
        """Return all albums the track appears on."""
        full_track = await self.get(item_id, provider_instance_id_or_domain)
        db_items = (
            await self.get_library_track_albums(full_track.item_id)
            if full_track.provider == "library"
            else []
        )
        # return all (unique) items from all providers
        result: UniqueList[Album] = UniqueList(db_items)
        # use search to get all items on the provider
        search_query = f"{full_track.artist_str} - {full_track.name}"
        # TODO: we could use musicbrainz info here to get a list of all releases known
        unique_ids: set[str] = set()
        # explicitly search all providers as we want all album versions
        # of this track, including those already mapped in the library
        search_providers = ["library", *self.mass.music.get_unique_providers()]
        search_results = await self.mass.music.search(
            search_query, [MediaType.TRACK], providers=search_providers
        )
        for prov_item in search_results.tracks:
            if not isinstance(prov_item, Track):  # for type checking
                continue
            if not loose_compare_strings(full_track.name, prov_item.name):
                continue
            if not prov_item.album:
                continue
            if not compare_artists(full_track.artists, prov_item.artists, any_match=True):
                continue
            unique_id = f"{prov_item.album.name}.{prov_item.album.version}"
            if unique_id in unique_ids:
                continue
            unique_ids.add(unique_id)
            # prefer db item
            if db_item := await self.mass.music.albums.get_library_item_by_prov_id(
                prov_item.album.item_id, prov_item.album.provider
            ):
                result.append(db_item)
            elif not in_library_only and isinstance(prov_item.album, Album):
                result.append(prov_item.album)
        return result

    async def similar_tracks(
        self,
        item_id: str,
        provider_instance_id_or_domain: str,
        limit: int = 25,
        allow_lookup: bool = False,
        preferred_provider_instances: list[str] | None = None,
    ) -> list[Track]:
        """
        Get a list of similar tracks for the given track.

        :param item_id: The item ID of the track.
        :param provider_instance_id_or_domain: The provider instance ID or domain.
        :param limit: Maximum number of similar tracks to return.
        :param allow_lookup: Allow lookup on other providers if not found.
        :param preferred_provider_instances: List of preferred provider instance IDs to use.
            When provided, these providers will be tried first before falling back to others.
        :raises MusicAssistantError: When no provider can complete the request.
        """
        ref_item = await self.get(item_id, provider_instance_id_or_domain)

        # Sort provider mappings to prefer user's provider instances
        def sort_key(mapping: ProviderMapping) -> tuple[int, int]:
            # Primary sort: preferred providers first (0), then others (1)
            preferred = (
                0
                if preferred_provider_instances
                and mapping.provider_instance in preferred_provider_instances
                else 1
            )
            # Secondary sort: by quality (higher is better, so negate)
            quality = -(mapping.quality or 0)
            return (preferred, quality)

        sorted_mappings = sorted(ref_item.provider_mappings, key=sort_key)
        last_provider_error: MusicAssistantError | ClientError | OSError | TimeoutError | None = (
            None
        )
        provider_responded = False

        # Try preferred providers first, then fall back to others
        for prov_mapping in sorted_mappings:
            prov = self.mass.get_provider(prov_mapping.provider_instance)
            if (
                not isinstance(prov, MusicProvider)
                or ProviderFeature.SIMILAR_TRACKS not in prov.supported_features
            ):
                continue
            result, error = await self._get_similar_tracks_from_provider(
                prov, ref_item, limit, provider_track_id=prov_mapping.item_id
            )
            if error is not None:
                last_provider_error = error
                continue
            if result is None:
                continue
            provider_responded = True
            if result:
                return result

        # Fallback: consult metadata/plugin providers that claim SIMILAR_TRACKS
        for prov in self.mass.get_providers_supporting_feature(
            ProviderFeature.SIMILAR_TRACKS,
            priority=(ProviderType.METADATA, ProviderType.PLUGIN),
        ):
            cross_prov = cast("MetadataProvider | PluginProvider", prov)
            result, error = await self._get_similar_tracks_from_provider(
                cross_prov, ref_item, limit
            )
            if error is not None:
                last_provider_error = error
                continue
            if result is None:
                continue
            provider_responded = True
            if result:
                return result

        if not allow_lookup:
            if not provider_responded and last_provider_error is not None:
                self._raise_similar_tracks_provider_error(ref_item, last_provider_error)
            return []

        try:
            result, error = await self._lookup_similar_tracks_provider(ref_item, limit)
        except UnsupportedFeaturedException:
            if provider_responded:
                return []
            if last_provider_error is not None:
                self._raise_similar_tracks_provider_error(ref_item, last_provider_error)
            raise
        if error is not None:
            last_provider_error = error
        if result is not None:
            provider_responded = True
            if result:
                return result

        if not provider_responded and last_provider_error is not None:
            self._raise_similar_tracks_provider_error(ref_item, last_provider_error)
        return []

    async def remove_item_from_library(self, item_id: str | int, recursive: bool = True) -> None:
        """Delete record from the database."""
        db_id = int(item_id)  # ensure integer
        # delete entry(s) from albumtracks table
        await self.mass.music.database.delete(DB_TABLE_ALBUM_TRACKS, {"track_id": db_id})
        # delete entry(s) from trackartists table
        await self.mass.music.database.delete(DB_TABLE_TRACK_ARTISTS, {"track_id": db_id})
        # delete the track itself from db
        await super().remove_item_from_library(db_id)

    async def set_identifiers(
        self,
        item_id: str,
        provider_instance_id_or_domain: str,
        mbid: str | None = None,
        acoustid: str | None = None,
        isrcs: list[str] | None = None,
    ) -> None:
        """
        Persist MBID / AcoustID / ISRCs onto the library track row.

        :param item_id: Provider-native track ID.
        :param provider_instance_id_or_domain: Music provider instance ID or domain.
        :param mbid: MusicBrainz recording ID.
        :param acoustid: AcoustID UUID.
        :param isrcs: ISRC codes.
        """
        # MBID is filled only when empty; AcoustID/ISRCs are appended via
        # external_ids without clobbering tag-sourced values.
        if not mbid and not acoustid and not isrcs:
            return
        try:
            track = await self.get_library_item_by_prov_id(item_id, provider_instance_id_or_domain)
        except MusicAssistantError as err:
            self.logger.debug(
                "set_identifiers: failed to load library track %s/%s: %s",
                provider_instance_id_or_domain,
                item_id,
                err,
            )
            return
        if track is None:
            return

        changed = False
        if mbid and not track.mbid:
            track.mbid = mbid
            changed = True
        if acoustid and not any(
            ext_id[0] == ExternalID.ACOUSTID and ext_id[1] == acoustid
            for ext_id in track.external_ids
        ):
            track.add_external_id(ExternalID.ACOUSTID, acoustid)
            changed = True
        for isrc in isrcs or ():
            if isrc:
                track.add_external_id(ExternalID.ISRC, isrc)
                changed = True
        if not changed:
            return

        await self.update_item_in_library(int(track.item_id), track)

    async def get_preview_url(self, provider_instance_id_or_domain: str, item_id: str) -> str:
        """Return url to short preview sample."""
        track = await self.get_provider_item(item_id, provider_instance_id_or_domain)
        # prefer provider-provided preview
        if preview := track.metadata.preview:
            return preview
        # fallback to a preview/sample hosted by our own webserver
        return self.mass.webserver.create_preview_url(provider_instance_id_or_domain, item_id)

    async def get_library_track_albums(
        self,
        item_id: str | int,
    ) -> list[Album]:
        """Return all in-library albums for a track."""
        db_id = int(item_id)  # ensure integer
        subquery = (
            f"SELECT album_id FROM {DB_TABLE_ALBUM_TRACKS} "
            f"WHERE {DB_TABLE_ALBUM_TRACKS}.track_id = :track_id"
        )
        query = f"{DB_TABLE_ALBUMS}.item_id in ({subquery})"
        return await self.mass.music.albums.get_library_items_by_query(
            extra_query_parts=[query],
            extra_query_params={"track_id": db_id},
            in_library_only=True,
        )

    async def get_library_match(self, item: Track) -> Track | None:
        """
        Return an existing library track matching the provider track.

        :param item: Provider track to resolve.
        """
        if library_item_id := await self._get_library_item_by_match(item):
            return await self.get_library_item(library_item_id)
        return None

    async def find_provider_match(
        self,
        base_track: Track,
        provider: MusicProvider,
        minimum_confidence: TrackMatchConfidence = TrackMatchConfidence.LIKELY,
        base_album: Album | ItemMapping | None = None,
        mapping_source: Track | None = None,
        allowed_provider_instances: set[str] | None = None,
    ) -> TrackProviderMatchResult:
        """
        Find the best track match on a music provider.

        :param base_track: Reference track to match.
        :param provider: Target provider.
        :param minimum_confidence: Lowest confidence that may be returned.
        :param base_album: Optional full reference album for release evidence.
        :param mapping_source: Optional library track whose mappings may be reused as candidates.
        :param allowed_provider_instances: Provider instances available to the initiating user.
        """
        resolved_base_album = base_album
        mapped_match: TrackProviderMatch | None = None
        mapping_source = mapping_source or base_track
        if mapping := self._get_provider_mapping(mapping_source, provider):
            if mapping_source is base_track:
                return TrackProviderMatchResult(
                    match=TrackProviderMatch(
                        track=base_track,
                        mapping=mapping,
                        confidence=TrackMatchConfidence.EXACT,
                    )
                )
            try:
                mapped_candidate = await self.get_provider_item(
                    mapping.item_id,
                    provider.instance_id,
                )
            except MediaNotFoundError:
                mapped_candidate = None
            if mapped_candidate:
                confidence, resolved_base_album = await self._get_match_confidence(
                    base_track,
                    mapped_candidate,
                    resolved_base_album,
                )
                if confidence >= minimum_confidence and (
                    candidate_mapping := self._get_provider_mapping(
                        mapped_candidate,
                        provider,
                    )
                ):
                    mapped_match = TrackProviderMatch(
                        track=mapped_candidate,
                        mapping=candidate_mapping,
                        confidence=confidence,
                    )
                    if confidence == TrackMatchConfidence.EXACT:
                        return TrackProviderMatchResult(match=mapped_match)
        if ProviderFeature.SEARCH not in provider.supported_features:
            return TrackProviderMatchResult(match=mapped_match)
        if MediaType.TRACK not in provider.supported_media_types:
            return TrackProviderMatchResult(match=mapped_match)
        if not base_track.artists:
            return TrackProviderMatchResult(match=mapped_match)

        search_queries = list(
            dict.fromkeys(f"{artist.name} - {base_track.name}" for artist in base_track.artists)
        )
        candidates: list[tuple[int, TrackProviderMatch]] = (
            [(0, mapped_match)] if mapped_match else []
        )
        seen_candidates: set[tuple[str, str]] = set()
        search_rank = len(candidates)
        for search_query in search_queries:
            search_results = await self.mass.music.search_provider(
                search_query,
                provider.instance_id,
                [MediaType.TRACK],
                limit=5,
                allowed_provider_instances=allowed_provider_instances,
            )
            for search_result in search_results.tracks:
                if not isinstance(search_result, Track):
                    continue
                candidate_key = (search_result.provider, search_result.item_id)
                if candidate_key in seen_candidates or not search_result.available:
                    continue
                seen_candidates.add(candidate_key)
                if not compare_track_title(base_track.name, search_result.name):
                    continue
                if not compare_artists(base_track.artists, search_result.artists, any_match=True):
                    continue
                try:
                    candidate = await self.get_provider_item(
                        search_result.item_id,
                        search_result.provider,
                    )
                except MediaNotFoundError:
                    continue
                confidence, resolved_base_album = await self._get_match_confidence(
                    base_track,
                    candidate,
                    resolved_base_album,
                )
                if confidence < minimum_confidence:
                    continue
                if not (mapping := self._get_provider_mapping(candidate, provider)):
                    continue
                candidates.append(
                    (
                        search_rank,
                        TrackProviderMatch(
                            track=candidate,
                            mapping=mapping,
                            confidence=confidence,
                        ),
                    )
                )
                search_rank += 1

        if not candidates:
            return TrackProviderMatchResult()
        best_confidence = max(match.confidence for _, match in candidates)
        best_matches = [
            (rank, match) for rank, match in candidates if match.confidence == best_confidence
        ]
        if best_confidence == TrackMatchConfidence.LOOSE and not self._matches_are_compatible(
            [match for _, match in best_matches]
        ):
            return TrackProviderMatchResult(ambiguous=True)
        _, best_match = max(
            best_matches,
            key=lambda ranked_match: (
                ranked_match[1].mapping.quality,
                -ranked_match[0],
            ),
        )
        return TrackProviderMatchResult(match=best_match)

    async def enrich_provider_mappings(
        self,
        track: Track,
        minimum_confidence: TrackMatchConfidence = TrackMatchConfidence.LIKELY,
        provider_instance_ids: set[str] | None = None,
    ) -> TrackProviderEnrichment:
        """
        Resolve missing streaming-provider mappings without updating the library.

        :param track: Provider track to enrich.
        :param minimum_confidence: Lowest confidence that may be accepted.
        :param provider_instance_ids: Provider instances available to the initiating user.
        """
        library_track = await self.get_library_match(track)
        enriched_track = deepcopy(track)
        base_album = await self._get_full_track_album(track)
        existing_domains = {
            mapping.provider_domain
            for mapping in enriched_track.provider_mappings
            if mapping.available
        }
        matches: list[TrackProviderMatch] = []
        ambiguous_providers: list[str] = []
        failed_providers: list[str] = []
        providers = (
            [
                provider
                for provider_instance_id in sorted(provider_instance_ids)
                if isinstance(
                    provider := self.mass.get_provider(provider_instance_id),
                    MusicProvider,
                )
            ]
            if provider_instance_ids is not None
            else self.mass.music.providers
        )
        for provider in providers:
            if provider.domain in existing_domains:
                continue
            if not provider.is_streaming_provider and not (
                library_track and self._get_provider_mapping(library_track, provider)
            ):
                continue
            try:
                result = await self.find_provider_match(
                    track,
                    provider,
                    minimum_confidence=minimum_confidence,
                    base_album=base_album,
                    mapping_source=library_track,
                    allowed_provider_instances=provider_instance_ids,
                )
            except (MusicAssistantError, ClientError, OSError, TimeoutError) as err:
                self.logger.warning(
                    "Failed to match %s on provider %s: %s",
                    track.name,
                    provider.name,
                    err,
                )
                failed_providers.append(provider.name)
                continue
            if result.match:
                enriched_track.provider_mappings = {
                    mapping
                    for mapping in enriched_track.provider_mappings
                    if mapping.provider_domain != provider.domain or mapping.available
                }
                enriched_track.provider_mappings.add(result.match.mapping)
                matches.append(result.match)
                existing_domains.add(provider.domain)
            elif result.ambiguous:
                ambiguous_providers.append(provider.name)
        return TrackProviderEnrichment(
            track=enriched_track,
            matches=tuple(matches),
            ambiguous_providers=tuple(ambiguous_providers),
            failed_providers=tuple(failed_providers),
            used_library_item=library_track is not None,
        )

    async def match_provider(
        self,
        base_track: Track,
        provider: MusicProvider,
        strict: bool = True,
        ref_albums: list[Album] | None = None,
    ) -> list[ProviderMapping]:
        """
        Try to find match on (streaming) provider for the provided track.

        This is used to link objects of different providers/qualities together.
        """
        if ref_albums is None:
            ref_albums = await self.albums(base_track.item_id, base_track.provider)
        self.logger.debug("Trying to match track %s on provider %s", base_track.name, provider.name)
        matches: list[ProviderMapping] = []
        for artist in base_track.artists:
            if matches:
                break
            search_str = f"{artist.name} - {base_track.name}"
            search_result = await self.search(search_str, provider.domain)
            for search_result_item in search_result:
                if not search_result_item.available:
                    continue
                # do a basic compare first
                if not compare_media_item(base_track, search_result_item, strict=False):
                    continue
                # we must fetch the full version, search results can be simplified objects
                prov_track = await self.get_provider_item(
                    search_result_item.item_id,
                    search_result_item.provider,
                    fallback=search_result_item,
                )
                if compare_track(base_track, prov_track, strict=strict, track_albums=ref_albums):
                    matches.extend(prov_track.provider_mappings)

        if not matches:
            self.logger.debug(
                "Could not find match for Track %s on provider %s",
                base_track.name,
                provider.name,
            )
        return matches

    async def match_providers(self, db_track: Track) -> None:
        """
        Try to find matching track on all providers for the provided (database) track_id.

        This is used to link objects of different providers/qualities together.
        """
        if db_track.provider != "library":
            return  # Matching only supported for database items

        track_albums = await self.albums(db_track.item_id, db_track.provider)
        # try to find match on all providers
        processed_domains = set()
        for provider in self.mass.music.providers:
            if provider.domain in processed_domains:
                continue
            if ProviderFeature.SEARCH not in provider.supported_features:
                continue
            if MediaType.TRACK not in provider.supported_media_types:
                continue
            if not provider.is_streaming_provider:
                # matching on unique providers is pointless as they push (all) their content to MA
                continue
            if match := await self.match_provider(
                db_track, provider, strict=True, ref_albums=track_albums
            ):
                # 100% match, we update the db with the additional provider mapping(s)
                await self.add_provider_mappings(db_track.item_id, match)
                processed_domains.add(provider.domain)

    async def _get_match_confidence(
        self,
        base_track: Track,
        candidate: Track,
        base_album: Album | ItemMapping | None,
    ) -> tuple[TrackMatchConfidence, Album | ItemMapping | None]:
        """Return candidate confidence with full album evidence when needed."""
        confidence = compare_track_evidence(
            base_track,
            candidate,
            base_album=base_album,
        )
        if confidence not in (
            TrackMatchConfidence.LOOSE,
            TrackMatchConfidence.LIKELY,
        ):
            return confidence, base_album
        if base_album is None:
            base_album = await self._get_full_track_album(base_track)
        candidate_album = await self._get_full_track_album(candidate)
        return (
            compare_track_evidence(
                base_track,
                candidate,
                base_album=base_album,
                compare_album_item=candidate_album,
            ),
            base_album,
        )

    @staticmethod
    def _get_provider_mapping(track: Track, provider: MusicProvider) -> ProviderMapping | None:
        """Return an available mapping suitable for the provider instance."""
        domain_mapping: ProviderMapping | None = None
        for mapping in sorted(track.provider_mappings, key=lambda item: item.quality, reverse=True):
            if not mapping.available:
                continue
            if mapping.provider_instance == provider.instance_id:
                return mapping
            if (
                mapping.provider_domain == provider.domain
                and not mapping.is_unique
                and domain_mapping is None
            ):
                domain_mapping = mapping
        if domain_mapping is None:
            return None
        return replace(domain_mapping, provider_instance=provider.instance_id)

    async def _get_full_track_album(self, track: Track) -> Album | ItemMapping | None:
        """Return full album details when they are available."""
        if not track.album or isinstance(track.album, Album):
            return track.album
        try:
            return await self.mass.music.albums.get(
                track.album.item_id,
                track.album.provider,
                allow_update_metadata=False,
            )
        except (InvalidDataError, MediaNotFoundError, ProviderUnavailableError) as err:
            self.logger.debug(
                "Could not load album details for track %s: %s",
                track.name,
                err,
            )
            return track.album

    @staticmethod
    def _matches_are_compatible(matches: list[TrackProviderMatch]) -> bool:
        """Return whether tied loose matches identify the same recording."""
        first_match = matches[0]
        return all(
            compare_track_evidence(first_match.track, match.track) >= TrackMatchConfidence.LIKELY
            for match in matches[1:]
        )

    async def _add_library_item(self, item: Track, overwrite_existing: bool = False) -> int:
        """Add a new item record to the database."""
        if not isinstance(item, Track):  # TODO: Remove this once the codebase is fully typed
            msg = "Not a valid Track object (ItemMapping can not be added to db)"  # type: ignore[unreachable]
            raise InvalidDataError(msg)
        if not item.artists:
            msg = "Track is missing artist(s)"
            raise InvalidDataError(msg)
        # normalize synced lyrics so clients only need a minimal single-timestamp LRC parser
        # promoting LRC formatted text stored in the plain lyrics tag
        item.metadata.lrc_lyrics = normalize_lrc_lyrics(
            item.metadata.lrc_lyrics or extract_lrc_lyrics(item.metadata.lyrics)
        )
        db_id = await self.mass.music.database.insert(
            self.db_table,
            {
                "name": item.name,
                "sort_name": item.sort_name,
                "version": item.version,
                "duration": item.duration,
                "favorite": item.favorite,
                "metadata": serialize_to_json(item.metadata),
                "search_name": create_safe_string(item.name, True, True),
                "search_sort_name": create_safe_string(item.sort_name or "", True, True),
                "timestamp_added": int(item.date_added.timestamp()) if item.date_added else UNSET,
            },
        )
        # update/set external id lookup table
        await self.set_external_ids(db_id, item.external_ids)
        # update/set provider_mappings table
        await self.set_provider_mappings(db_id, item.provider_mappings)
        # set track artist(s)
        await self._set_track_artists(db_id, item.artists)
        # handle track album
        if item.album:
            await self._set_track_album(
                db_id=db_id,
                album=item.album,
                disc_number=getattr(item, "disc_number", 0),
                track_number=getattr(item, "track_number", 0),
            )
        self.logger.debug("added %s to database (id: %s)", item.name, db_id)
        return db_id

    async def _update_library_item(
        self,
        item_id: str | int,
        update: Track,
        overwrite: bool = False,
        *,
        set_album: bool = True,
    ) -> None:
        """Update Track record in the database, merging data."""
        db_id = int(item_id)  # ensure integer
        cur_item = await self.get_library_item(db_id)
        metadata = update.metadata if overwrite else cur_item.metadata.update(update.metadata)
        metadata.lrc_lyrics = normalize_lrc_lyrics(
            metadata.lrc_lyrics or extract_lrc_lyrics(metadata.lyrics)
        )
        cur_item.external_ids.update(update.external_ids)
        name = update.name if overwrite else cur_item.name
        sort_name = update.sort_name if overwrite else cur_item.sort_name or update.sort_name
        await self.mass.music.database.update(
            self.db_table,
            {"item_id": db_id},
            {
                "name": name,
                "sort_name": sort_name,
                "version": update.version if overwrite else cur_item.version or update.version,
                "duration": update.duration if overwrite else cur_item.duration or update.duration,
                "metadata": serialize_to_json(metadata),
                "search_name": create_safe_string(name, True, True),
                "search_sort_name": create_safe_string(sort_name or "", True, True),
                "timestamp_added": int(update.date_added.timestamp())
                if update.date_added
                else UNSET,
            },
        )
        # update/set external id lookup table
        await self.set_external_ids(
            db_id, update.external_ids if overwrite else cur_item.external_ids
        )
        # update/set provider_mappings table
        provider_mappings = provider_mappings_for_update(
            cur_item.provider_mappings, update.provider_mappings, overwrite
        )
        await self.set_provider_mappings(db_id, provider_mappings, overwrite)
        # set track artist(s)
        artists = update.artists if overwrite else cur_item.artists + update.artists
        await self._set_track_artists(db_id, artists, overwrite=overwrite)
        # update/set track album
        if update.album and set_album:
            await self._set_track_album(
                db_id=db_id,
                album=update.album,
                disc_number=update.disc_number or cur_item.disc_number,
                track_number=update.track_number or cur_item.track_number,
                overwrite=overwrite,
            )
        self.logger.debug("updated %s in database: (id %s)", update.name, db_id)

    async def _update_library_item_for_merge(self, item_id: int, update: Track) -> None:
        """Merge track model state without replacing existing album relations."""
        await self._update_library_item(item_id, update, set_album=False)

    async def _set_track_album(
        self,
        db_id: int,
        album: Album | ItemMapping,
        disc_number: int,
        track_number: int,
        overwrite: bool = False,
    ) -> None:
        """
        Store Track Album info.

        A track can exist on multiple albums so we have a mapping table between
        albums and tracks which stores the relation between the two and it also
        stores the track and disc number of the track within an album.
        For digital releases, the discnumber will be just 0 or 1.
        Track number should start counting at 1.
        """
        db_album: Album | ItemMapping | None = None
        if album.provider == "library":
            db_album = album
        elif existing := await self.mass.music.albums.get_library_item_by_prov_id(
            album.item_id, album.provider
        ):
            db_album = existing

        if not db_album or overwrite:
            # ensure we have an actual album object
            if isinstance(album, ItemMapping):
                db_album = await self.mass.music.albums.add_item_mapping_as_album_to_library(album)
            else:
                db_album = await self.mass.music.albums.add_item_to_library(
                    album,
                    overwrite_existing=overwrite,
                )
        # write (or update) record in album_tracks table
        await self.mass.music.database.insert_or_replace(
            DB_TABLE_ALBUM_TRACKS,
            {
                "track_id": db_id,
                "album_id": int(db_album.item_id),
                "disc_number": disc_number,
                "track_number": track_number,
            },
        )

    async def _set_track_artists(
        self,
        db_id: int,
        artists: Iterable[Artist | ItemMapping],
        overwrite: bool = False,
    ) -> None:
        """
        Store Track Artists.

        An empty set of artists never clears the stored rows: a track without any
        artist can not be played or resolved.
        """
        all_artists = list(artists)
        if not all_artists:
            if overwrite:
                # a caller asking to replace all artists with none is a bug,
                # so keep the stored rows and make the attempt visible
                self.logger.warning("Ignoring request to clear all artists of track id %s", db_id)
            return
        if overwrite:
            # on overwrite, clear the track_artists table first
            await self.mass.music.database.delete(
                DB_TABLE_TRACK_ARTISTS,
                {
                    "track_id": db_id,
                },
            )
        for artist in all_artists:
            await self._set_track_artist(db_id, artist=artist, overwrite=overwrite)

    async def _set_track_artist(
        self, db_id: int, artist: Artist | ItemMapping, overwrite: bool = False
    ) -> ItemMapping:
        """Store Track Artist info."""
        db_artist: Artist | ItemMapping | None = None
        if artist.provider == "library":
            db_artist = artist
        elif existing := await self.mass.music.artists.get_library_item_by_prov_id(
            artist.item_id, artist.provider
        ):
            db_artist = existing

        if not db_artist or overwrite:
            # Convert ItemMapping to Artist if needed
            artist_to_add = (
                self.mass.music.artists.artist_from_item_mapping(artist)
                if isinstance(artist, ItemMapping)
                else artist
            )
            db_artist = await self.mass.music.artists.add_item_to_library(
                artist_to_add, overwrite_existing=overwrite
            )
        # write (or update) record in track_artists table
        await self.mass.music.database.insert_or_replace(
            DB_TABLE_TRACK_ARTISTS,
            {
                "track_id": db_id,
                "artist_id": int(db_artist.item_id),
            },
        )
        return ItemMapping.from_item(db_artist)

    def _sync_details_query_parts(self) -> tuple[str, str, dict[str, Any]]:
        """Return extra (columns, joins, params) for the tracks sync-details query."""
        # the sync loop needs to know if the track has (valid) album and artist links
        # to be able to backfill missing ones on existing library tracks
        extra_columns = """
            , EXISTS (
                SELECT 1 FROM album_tracks
                JOIN albums ON albums.item_id = album_tracks.album_id
                WHERE album_tracks.track_id = tracks.item_id
            ) AS has_album
            , EXISTS (
                SELECT 1 FROM track_artists
                JOIN artists ON artists.item_id = track_artists.artist_id
                WHERE track_artists.track_id = tracks.item_id
            ) AS has_artists
        """
        return extra_columns, "", {}

    def _parse_sync_details_row(self, db_row: Mapping[str, Any]) -> TrackSyncDetails:
        """Parse a raw sync-details db row into a TrackSyncDetails object."""
        return TrackSyncDetails(
            item_id=db_row["item_id"],
            favorite=bool(db_row["favorite"]),
            date_added=datetime.fromtimestamp(db_row["timestamp_added"], tz=UTC),
            provider_mappings=self._parse_sync_details_mappings(db_row),
            has_album=bool(db_row["has_album"]),
            has_artists=bool(db_row["has_artists"]),
        )

    def _parse_summary_row(self, db_row: Mapping[str, Any]) -> TrackSummary:
        """Parse a raw summary db row into a TrackSummary object."""
        item = cast("TrackSummary", super()._parse_summary_row(db_row))
        item.version = db_row["version"] or ""
        item.duration = db_row["duration"] or 0
        item.metadata.explicit = None if db_row["explicit"] is None else bool(db_row["explicit"])
        if raw_release_date := db_row["release_date"]:
            item.metadata.release_date = datetime.fromisoformat(raw_release_date)
        item.artists = self._parse_summary_artist_mappings(db_row)
        if raw_album := db_row["track_album"]:
            album: dict[str, Any] = json_loads(raw_album)
            album_thumb: MediaItemImage | None = None
            if album_images := album.get("images"):
                for image in album_images:
                    if image["type"] != ImageType.THUMB.value:
                        continue
                    album_thumb = MediaItemImage(
                        type=ImageType.THUMB,
                        path=image["path"],
                        provider=image["provider"],
                        remotely_accessible=image.get("remotely_accessible", False),
                    )
                    break
            item.album = ItemMappingSummary(
                media_type=MediaType.ALBUM,
                item_id=str(album["item_id"]),
                provider="library",
                name=album["name"],
                sort_name=album["sort_name"],
                year=album["year"],
                image=album_thumb,
            )
            item.disc_number = album["disc_number"] or 0
            item.track_number = album["track_number"] or 0
            if album_thumb:
                # always prefer album image over track image
                item.metadata.images = UniqueList([album_thumb])
        return item

    async def _get_similar_tracks_from_provider(
        self,
        provider: MusicProvider | MetadataProvider | PluginProvider,
        ref_item: Track,
        limit: int,
        provider_track_id: str | None = None,
    ) -> tuple[
        list[Track] | None,
        MusicAssistantError | ClientError | OSError | TimeoutError | None,
    ]:
        """
        Request similar tracks from a provider.

        :param provider: Provider to request similar tracks from.
        :param ref_item: Full track supplied to metadata and plugin providers.
        :param limit: Maximum number of tracks to return.
        :param provider_track_id: Provider track ID supplied to music providers.
        """
        if isinstance(provider, MusicProvider):
            if provider_track_id is None:
                raise InvalidDataError("Music provider track ID is required")
            request = provider.get_similar_tracks(provider_track_id, limit=limit)
        else:
            request = provider.get_similar_tracks(ref_item, limit=limit)
        try:
            result = await request
        except NotImplementedError:
            return None, None
        except (MusicAssistantError, ClientError, OSError, TimeoutError) as err:
            self.logger.warning(
                "Failed to fetch similar tracks for %s from provider %s: %s",
                ref_item.name,
                provider.name,
                err,
            )
            return None, err
        return result, None

    async def _match_similar_tracks_provider(
        self, ref_item: Track, provider: MusicProvider
    ) -> tuple[
        list[ProviderMapping] | None,
        MusicAssistantError | ClientError | OSError | TimeoutError | None,
    ]:
        """
        Find a matching track on a provider for a similar-tracks lookup.

        :param ref_item: Track to match.
        :param provider: Provider to search for a matching track.
        """
        try:
            return await self.match_provider(ref_item, provider), None
        except (MusicAssistantError, ClientError, OSError, TimeoutError) as err:
            self.logger.warning(
                "Failed to match %s on provider %s for similar tracks: %s",
                ref_item.name,
                provider.name,
                err,
            )
            return None, err

    async def _lookup_similar_tracks_provider(
        self, ref_item: Track, limit: int
    ) -> tuple[
        list[Track] | None,
        MusicAssistantError | ClientError | OSError | TimeoutError | None,
    ]:
        """
        Find a provider match and request similar tracks from it.

        :param ref_item: Track to match.
        :param limit: Maximum number of tracks to return.
        :raises UnsupportedFeaturedException: When no music provider supports similar tracks.
        """
        supported_providers = [
            prov
            for prov in self.mass.music.providers
            if ProviderFeature.SIMILAR_TRACKS in prov.supported_features
        ]
        if not supported_providers:
            msg = "No Music Provider found that supports requesting similar tracks."
            raise UnsupportedFeaturedException(msg)
        mapped_instances = {mapping.provider_instance for mapping in ref_item.provider_mappings}
        providers = [
            prov for prov in supported_providers if prov.instance_id not in mapped_instances
        ]

        last_error: MusicAssistantError | ClientError | OSError | TimeoutError | None = None
        provider_responded = False
        for provider in providers:
            mappings, error = await self._match_similar_tracks_provider(ref_item, provider)
            if error is not None:
                last_error = error
                continue
            if not mappings:
                continue
            if ref_item.provider == "library":
                await self.add_provider_mappings(ref_item.item_id, mappings)
            ref_item.provider_mappings.update(mappings)
            result, error = await self._get_similar_tracks_from_provider(
                provider, ref_item, limit, provider_track_id=mappings[0].item_id
            )
            if error is not None:
                last_error = error
                continue
            if result is None:
                continue
            provider_responded = True
            if result:
                return result, None
        return ([] if provider_responded else None), last_error

    @staticmethod
    def _raise_similar_tracks_provider_error(
        ref_item: Track,
        err: MusicAssistantError | ClientError | OSError | TimeoutError,
    ) -> Never:
        """
        Raise a provider error from a similar-tracks lookup using MA's typed error hierarchy.

        :param ref_item: The track whose similar tracks were requested.
        :param err: The provider error to raise or normalize.
        """
        if isinstance(err, MusicAssistantError):
            raise err
        raise ProviderUnavailableError(
            f"Failed to fetch similar tracks for {ref_item.name}"
        ) from err
