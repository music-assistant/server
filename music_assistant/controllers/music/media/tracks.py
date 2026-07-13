"""Manage MediaItems of type Track."""

from __future__ import annotations

import urllib.parse
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

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
    MusicAssistantError,
    UnsupportedFeaturedException,
)
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
from music_assistant.controllers.music.helpers import search_name_match_clause
from music_assistant.helpers.compare import (
    compare_artists,
    compare_media_item,
    compare_track,
    create_safe_string,
    loose_compare_strings,
)
from music_assistant.helpers.database import UNSET
from music_assistant.helpers.json import json_loads, serialize_to_json
from music_assistant.models.music_provider import MusicProvider

from .base import MediaControllerBase, TrackSyncDetails

if TYPE_CHECKING:
    from collections.abc import Mapping

    from music_assistant import MusicAssistant
    from music_assistant.models.metadata_provider import MetadataProvider
    from music_assistant.models.plugin import PluginProvider


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

    async def library_items(
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
        """
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
        if search and " - " in search:
            # handle combined artist + title search
            artist_str, title_str = search.split(" - ", 1)
            search = None
            title_str = create_safe_string(title_str, True, True)
            artist_str = create_safe_string(artist_str, True, True)
            extra_query_parts.append(
                search_name_match_clause("tracks", title_str, "search_title", extra_query_params)
            )
            # use join with artists table to filter on artist name
            extra_join_parts.append(
                "JOIN track_artists ON track_artists.track_id = tracks.item_id "
                "JOIN artists ON artists.item_id = track_artists.artist_id "
                "AND "
                + search_name_match_clause(
                    "artists", artist_str, "search_artist", extra_query_params
                )
            )
        result = await self.get_library_items_by_query(
            favorite=favorite,
            search=search,
            genre_ids=genre,
            limit=limit,
            offset=offset,
            order_by=order_by,
            provider_filter=self._ensure_provider_filter(provider),
            extra_query_parts=extra_query_parts,
            extra_query_params=extra_query_params,
            extra_join_parts=extra_join_parts,
            played_only=played_only,
            in_library_only=True,
            summary=summary,
        )
        if search and len(result) < 25 and not offset:
            # append artist items to result
            artist_search_str = create_safe_string(search, True, True)
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
                provider_filter=self._ensure_provider_filter(provider),
                extra_query_parts=extra_query_parts,
                extra_query_params=extra_query_params,
                extra_join_parts=extra_join_parts,
                in_library_only=True,
                summary=summary,
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
            if not self.mass.music.library_supported(provider, MediaType.TRACK):
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

        Results accumulate (de-duplicated) up to ``limit`` from every provider
        offering similarity, consulted in order of each provider's ``priority``
        attribute (lower = more preferred, default 50). At equal priority the
        track's own music provider leads and the others top up any shortfall,
        so results only change when a provider explicitly claims priority.

        :param item_id: The item ID of the track.
        :param provider_instance_id_or_domain: The provider instance ID or domain.
        :param limit: Maximum number of similar tracks to return (the target the
            top-up logic tries to fill).
        :param allow_lookup: Allow matching the seed onto a music provider to pull
            more similar tracks when the priority providers come up short.
        :param preferred_provider_instances: Music-provider instances to prefer
            when topping up from the track's own providers.
        """
        ref_item = await self.get(item_id, provider_instance_id_or_domain)

        # Accumulate up to `limit` deduped tracks from all similarity sources.
        collected: list[Track] = []
        seen: set[str] = set()

        def _add(tracks: list[Track]) -> None:
            for track in tracks:
                if len(collected) >= limit:
                    return
                uri = getattr(track, "uri", None)
                if uri is not None:
                    if uri == ref_item.uri or uri in seen:
                        continue
                    seen.add(uri)
                collected.append(track)

        # The track's own provider mappings, preferred instances and higher
        # quality first.
        def sort_key(mapping: ProviderMapping) -> tuple[int, int]:
            preferred = (
                0
                if preferred_provider_instances
                and mapping.provider_instance in preferred_provider_instances
                else 1
            )
            quality = -(mapping.quality or 0)
            return (preferred, quality)

        # Gather every similarity source: the track's own music provider(s)
        # plus plugin/metadata providers implementing SIMILAR_TRACKS. Sources
        # are consulted by the provider's `priority` attribute; at equal
        # priority the track's own music provider wins, so a provider that
        # wants its results to lead (e.g. a sonic-similarity plugin) must
        # explicitly declare a lower priority.
        sources: list[
            tuple[
                tuple[int, int, int],
                MusicProvider | MetadataProvider | PluginProvider,
                ProviderMapping | None,
            ]
        ] = []
        for prov in self.mass.get_providers_supporting_feature(
            ProviderFeature.SIMILAR_TRACKS,
            priority=(ProviderType.METADATA, ProviderType.PLUGIN),
        ):
            if isinstance(prov, MusicProvider):
                continue
            cross_prov = cast("MetadataProvider | PluginProvider", prov)
            sources.append(((getattr(cross_prov, "priority", 50), 1, 0), cross_prov, None))
        for idx, prov_mapping in enumerate(sorted(ref_item.provider_mappings, key=sort_key)):
            mapped_prov = self.mass.get_provider(prov_mapping.provider_instance)
            if not isinstance(mapped_prov, MusicProvider):
                continue
            if ProviderFeature.SIMILAR_TRACKS not in mapped_prov.supported_features:
                continue
            sources.append(
                ((getattr(mapped_prov, "priority", 50), 0, idx), mapped_prov, prov_mapping)
            )

        for _, prov, src_mapping in sorted(sources, key=lambda source: source[0]):
            if len(collected) >= limit:
                break
            try:
                if src_mapping is None:
                    _add(
                        await cast("MetadataProvider | PluginProvider", prov).get_similar_tracks(
                            ref_item, limit=limit
                        )
                    )
                else:
                    _add(
                        await cast("MusicProvider", prov).get_similar_tracks(
                            prov_track_id=src_mapping.item_id, limit=limit
                        )
                    )
            except NotImplementedError:
                continue

        # Final top-up: when still short, match the seed onto a similar-capable
        # music provider and pull more.
        if allow_lookup and len(collected) < limit:
            music_prov: MusicProvider | None = next(
                (
                    prov
                    for prov in self.mass.music.providers
                    if ProviderFeature.SIMILAR_TRACKS in prov.supported_features
                ),
                None,
            )
            if music_prov is None:
                if not collected:
                    msg = "No Music Provider found that supports requesting similar tracks."
                    raise UnsupportedFeaturedException(msg)
            elif mappings := await self.match_provider(ref_item, music_prov):
                if ref_item.provider == "library":
                    # update database with new provider mappings
                    await self.add_provider_mappings(ref_item.item_id, mappings)
                ref_item.provider_mappings.update(mappings)
                _add(
                    await music_prov.get_similar_tracks(
                        prov_track_id=mappings[0].item_id, limit=limit
                    )
                )

        return collected[:limit]

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
        enc_track_id = urllib.parse.quote(item_id)
        return (
            f"{self.mass.webserver.base_url}/preview?"
            f"provider={provider_instance_id_or_domain}&item_id={enc_track_id}"
        )

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
                    matches.extend(search_result_item.provider_mappings)

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
            if not self.mass.music.library_supported(provider, MediaType.TRACK):
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

    async def _add_library_item(self, item: Track, overwrite_existing: bool = False) -> int:
        """Add a new item record to the database."""
        if not isinstance(item, Track):  # TODO: Remove this once the codebase is fully typed
            msg = "Not a valid Track object (ItemMapping can not be added to db)"  # type: ignore[unreachable]
            raise InvalidDataError(msg)
        if not item.artists:
            msg = "Track is missing artist(s)"
            raise InvalidDataError(msg)
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
        self, item_id: str | int, update: Track, overwrite: bool = False
    ) -> None:
        """Update Track record in the database, merging data."""
        db_id = int(item_id)  # ensure integer
        cur_item = await self.get_library_item(db_id)
        metadata = update.metadata if overwrite else cur_item.metadata.update(update.metadata)
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
        provider_mappings = (
            update.provider_mappings
            if overwrite
            else {*update.provider_mappings, *cur_item.provider_mappings}
        )
        await self.set_provider_mappings(db_id, provider_mappings, overwrite)
        # set track artist(s)
        artists = update.artists if overwrite else cur_item.artists + update.artists
        await self._set_track_artists(db_id, artists, overwrite=overwrite)
        # update/set track album
        if update.album:
            await self._set_track_album(
                db_id=db_id,
                album=update.album,
                disc_number=update.disc_number or cur_item.disc_number,
                track_number=update.track_number or cur_item.track_number,
                overwrite=overwrite,
            )
        self.logger.debug("updated %s in database: (id %s)", update.name, db_id)

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
        """Store Track Artists."""
        if overwrite:
            # on overwrite, clear the track_artists table first
            await self.mass.music.database.delete(
                DB_TABLE_TRACK_ARTISTS,
                {
                    "track_id": db_id,
                },
            )
        artist_mappings: UniqueList[ItemMapping] = UniqueList()
        for artist in artists:
            mapping = await self._set_track_artist(db_id, artist=artist, overwrite=overwrite)
            artist_mappings.append(mapping)

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
        # the sync loop needs to know if the track has a (valid) album link
        # to be able to backfill a missing album on existing library tracks
        extra_columns = """
            , EXISTS (
                SELECT 1 FROM album_tracks
                JOIN albums ON albums.item_id = album_tracks.album_id
                WHERE album_tracks.track_id = tracks.item_id
            ) AS has_album
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
