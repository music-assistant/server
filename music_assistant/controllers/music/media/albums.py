"""Manage MediaItems of type Album."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Iterable
from dataclasses import dataclass, replace
from time import time
from typing import TYPE_CHECKING, Any, Final, cast

import aiohttp
from music_assistant_models.auth import Scope
from music_assistant_models.enums import (
    AlbumType,
    ExternalID,
    ListingType,
    MediaType,
    ProviderFeature,
    SortDirection,
    SortField,
)
from music_assistant_models.errors import (
    InvalidDataError,
    MediaNotFoundError,
    MusicAssistantError,
    ProviderUnavailableError,
)
from music_assistant_models.helpers import create_safe_string, create_uri
from music_assistant_models.media_items import (
    Album,
    AlbumSummary,
    Artist,
    ItemMapping,
    ProviderMapping,
    Track,
    UniqueList,
)

from music_assistant.constants import DB_TABLE_ALBUM_ARTISTS, DB_TABLE_ALBUM_TRACKS, DB_TABLE_ALBUMS
from music_assistant.controllers.music.helpers import (
    fill_track_from_recording,
    metadata_for_update,
    provider_mappings_for_update,
    provider_mappings_from_urls,
    search_name_match_clause,
)
from music_assistant.controllers.music.listing import apply_listing, resolve_listing_sort
from music_assistant.controllers.music.listing_cache import Listing, cached_listing
from music_assistant.helpers.compare import (
    ALBUM_RETAIL_SUFFIX_KEYS,
    AlbumMatchEvidence,
    album_tracks_have_positions,
    compare_album_evidence,
    compare_artists,
    compare_strings,
    loose_compare_strings,
    strip_album_retail_suffix,
)
from music_assistant.helpers.database import UNSET
from music_assistant.helpers.external_ids import (
    barcode_to_upc,
    is_valid_barcode,
    is_valid_isrc,
    normalize_external_id,
)
from music_assistant.helpers.json import serialize_to_json
from music_assistant.helpers.uri import share_url_provider
from music_assistant.models.music_provider import (
    PROVIDER_FETCH_ERRORS,
    MusicProvider,
    provider_fetch_log_level,
)
from music_assistant.providers.musicbrainz.provider import (
    is_digital_release,
    relation_urls,
    release_matches_album,
)

from .album_tracks import album_track_backfills, select_album_tracks
from .base import EXTERNAL_ID_LOOKUP_ERRORS, MAX_EXTERNAL_ID_MATCH_LOOKUPS, MediaControllerBase

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from music_assistant import MusicAssistant
    from music_assistant.providers.musicbrainz import MusicbrainzProvider
    from music_assistant.providers.musicbrainz.models import (
        MusicBrainzBarcodeRelease,
        MusicBrainzRecording,
        MusicBrainzRelease,
    )


# on top of the fetch failures, a MusicBrainz lookup tolerates an HTTP status error: unlike
# a music provider, MusicBrainz has no account whose failure must surface, and the evidence
# it supplies is optional
_MUSICBRAINZ_LOOKUP_ERRORS: Final[tuple[type[Exception], ...]] = (
    *PROVIDER_FETCH_ERRORS,
    aiohttp.ClientResponseError,
)

# how many seconds the duration of one and the same track may differ between sources
_TRACK_DURATION_TOLERANCE = 8

# how many of a release group's official editions are looked up on the music providers,
# likeliest first: each one costs a MusicBrainz release lookup and a barcode fan-out
_MAX_EDITION_LOOKUPS = 3


@dataclass
class _BaseTracksMemo:
    """Single-slot memo holding the tracklist of one base album, resolved on first use."""

    resolved: bool = False
    tracks: list[Track] | None = None


class AlbumsController(MediaControllerBase[Album]):
    """Controller managing MediaItems of type Album."""

    db_table = DB_TABLE_ALBUMS
    media_type = MediaType.ALBUM
    item_cls = Album
    summary_item_cls = AlbumSummary

    def __init__(self, mass: MusicAssistant) -> None:
        """Initialize class."""
        super().__init__(mass)
        # register (extra) api handlers
        api_base = self.api_base
        self.mass.register_api_command(
            f"music/{api_base}/album_tracks", self.tracks, required_scope=Scope.LIBRARY_READ
        )
        self.mass.register_api_command(
            f"music/{api_base}/album_versions", self.versions, required_scope=Scope.LIBRARY_READ
        )

    @property
    def base_query(self) -> tuple[str, dict[str, Any]]:
        """Return the base SELECT query for albums and its bound query params."""
        query = f"""
        SELECT
            albums.*,
            {self._external_ids_query()} AS external_ids,
            {self._favorite_query()} AS favorite,
            {self._provider_mappings_query()} AS provider_mappings,
            (SELECT JSON_GROUP_ARRAY(
                json_object(
                'item_id', artists.item_id,
                'provider', 'library',
                    'name', artists.name,
                    'sort_name', artists.sort_name,
                    'media_type', 'artist'
                )) FROM artists JOIN album_artists on album_artists.album_id = albums.item_id  WHERE artists.item_id = album_artists.artist_id) AS artists
            FROM albums"""
        return query, {}

    @property
    def summary_query(self) -> tuple[str, dict[str, Any]]:
        """Return the slim SELECT query used for album summary listings."""
        artists_query = self._artist_mappings_summary_query(DB_TABLE_ALBUM_ARTISTS, "album_id")
        query = f"""
        SELECT
            {self._summary_base_columns()},
            albums.version,
            albums.year,
            albums.album_type,
            {self._provider_mappings_query()} AS provider_mappings,
            {artists_query} AS artists
            FROM albums"""
        return query, {}

    async def get(
        self,
        item_id: str,
        provider_instance_id_or_domain: str,
        allow_update_metadata: bool = True,
        recursive: bool = True,
    ) -> Album:
        """Return (full) details for a single media item."""
        album = await super().get(
            item_id,
            provider_instance_id_or_domain,
            allow_update_metadata=allow_update_metadata,
        )
        if not recursive:
            return album

        # append artist details to full album item (resolve ItemMappings)
        album_artists: UniqueList[Artist | ItemMapping] = UniqueList()
        for artist in album.artists:
            if not isinstance(artist, ItemMapping):
                album_artists.append(artist)
                continue
            with contextlib.suppress(MediaNotFoundError):
                album_artists.append(
                    await self.mass.music.artists.get(
                        artist.item_id, artist.provider, allow_update_metadata=False
                    )
                )
        album.artists = album_artists
        return album

    async def library_items(  # noqa: PLR0913
        self,
        favorite: bool | None = None,
        search: str | None = None,
        limit: int = 500,
        offset: int = 0,
        order_by: str | None = None,
        provider: str | list[str] | None = None,
        genre: int | list[int] | None = None,
        played_only: bool = False,
        album_types: list[AlbumType] | None = None,
        *,
        sort_field: SortField | None = None,
        sort_direction: SortDirection | None = None,
        summary: bool = True,
        reachable_via: list[str] | None = None,
        **kwargs: Any,
    ) -> list[Album]:
        """
        Get in-database albums.

        :param favorite: Only include the current user's likes (True) or dislikes (False).
        :param search: Filter by search query.
        :param limit: Maximum number of items to return.
        :param offset: Number of items to skip.
        :param order_by: DEPRECATED - use sort_field and sort_direction instead.
        :param provider: Filter by provider instance ID (single string or list).
        :param genre: Filter by genre id(s).
        :param played_only: Filter to only played albums.
        :param album_types: Filter by album types.
        :param sort_field: Sort field to use.
        :param sort_direction: Sort direction, the field's default when omitted.
        :param summary: When True (default), return slim summary items containing only the
            fields needed for a list view. Set to False to get fully hydrated items.
        :param reachable_via: Restrict results to items with a provider mapping reachable
            through one of these provider instance ids (OR semantics). See
            `MediaControllerBase.library_items` for the full semantics.
        """
        field, direction = self.resolve_sort(sort_field, sort_direction, order_by)
        reachable_via = self._resolve_reachable_via(reachable_via)
        if reachable_via is not None and not reachable_via:
            return []
        extra_query_params: dict[str, Any] = {}
        extra_query_parts: list[str] = []
        extra_join_parts: list[str] = []
        artist_table_joined = False
        if album_types:
            extra_query_parts.append("albums.album_type IN :album_types")
            extra_query_params["album_types"] = [x.value for x in album_types]
        if field == SortField.ARTIST_NAME:
            extra_join_parts.append(
                "JOIN album_artists ON album_artists.album_id = albums.item_id "
                "JOIN artists ON artists.item_id = album_artists.artist_id"
            )
            artist_table_joined = True
        if search and " - " in search:
            # handle combined artist + title search
            artist_str, title_str = search.split(" - ", 1)
            search = None
            title_str = create_safe_string(title_str, True, True)
            artist_str = create_safe_string(artist_str, True, True)
            extra_query_parts.append(
                search_name_match_clause("albums", title_str, "search_title", extra_query_params)
            )
            if not artist_table_joined:
                extra_join_parts.append(
                    "JOIN album_artists ON album_artists.album_id = albums.item_id "
                    "JOIN artists ON artists.item_id = album_artists.artist_id "
                    "AND "
                    + search_name_match_clause(
                        "artists", artist_str, "search_artist", extra_query_params
                    )
                )
                artist_table_joined = True
            else:
                extra_query_parts.append(
                    search_name_match_clause(
                        "artists", artist_str, "search_artist", extra_query_params
                    )
                )
        result = await self.get_library_items_by_query(
            favorite=favorite,
            search=search,
            genre_ids=genre,
            limit=limit,
            offset=offset,
            sort_field=field,
            sort_direction=direction,
            provider_filter=self._provider_filter_considering_reachability(provider, reachable_via),
            extra_query_parts=extra_query_parts,
            extra_query_params=extra_query_params,
            extra_join_parts=extra_join_parts,
            played_only=played_only,
            in_library_only=True,
            summary=summary,
            reachable_via=reachable_via,
        )

        # Calculate how many more items we need to reach the original limit
        remaining_limit = limit - len(result)

        if search and len(result) < 25 and not offset and remaining_limit > 0:
            # append artist items to result
            search = create_safe_string(search, True, True)
            if not artist_table_joined:
                extra_join_parts.append(
                    "JOIN album_artists ON album_artists.album_id = albums.item_id "
                    "JOIN artists ON artists.item_id = album_artists.artist_id "
                    "AND "
                    + search_name_match_clause(
                        "artists", search, "search_artist", extra_query_params
                    )
                )
            else:
                extra_query_parts.append(
                    search_name_match_clause("artists", search, "search_artist", extra_query_params)
                )
            existing_uris = {item.uri for item in result}

            for album in await self.get_library_items_by_query(
                favorite=favorite,
                search=None,
                limit=remaining_limit,
                sort_field=field,
                sort_direction=direction,
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
                if album.uri not in existing_uris:
                    result.append(album)
                    # Stop if we've reached the original limit
                    if len(result) >= limit:
                        break
        return result

    async def library_count(
        self, favorite_only: bool = False, album_types: list[AlbumType] | None = None
    ) -> int:
        """
        Return the number of albums in the library.

        Restricted to the providers the current user is allowed to see when that user
        has a provider filter set.

        :param favorite_only: Only count the albums the current user likes.
        :param album_types: Only count albums of these types.
        """
        sql_query = f"SELECT item_id FROM {self.db_table}"
        query_parts: list[str] = []
        query_params: dict[str, Any] = {}
        if favorite_only:
            query_parts.append(self._favorite_filter_clause(query_params, True))
        if album_types:
            query_parts.append("albums.album_type IN :album_types")
            query_params["album_types"] = [x.value for x in album_types]
        if provider_filter := self._ensure_provider_filter(None):
            query_parts.append(
                self._provider_filter_clause(query_params, provider_filter, in_library_only=True)
            )
        if query_parts:
            sql_query += f" WHERE {' AND '.join(query_parts)}"
        return await self.mass.music.database.get_count_from_query(sql_query, query_params)

    async def remove_item_from_library(self, item_id: str | int, recursive: bool = True) -> None:
        """Delete item from the library(database)."""
        db_id = int(item_id)  # ensure integer
        # recursively also remove album tracks
        for db_track in await self.get_library_album_tracks(db_id):
            if not recursive:
                raise MusicAssistantError("Album still has tracks linked")
            with contextlib.suppress(MediaNotFoundError):
                await self.mass.music.tracks.remove_item_from_library(db_track.item_id)
        # remove the item before its relations so failed analysis cleanup leaves it intact
        await super().remove_item_from_library(item_id)
        # delete entry(s) from albumtracks table
        await self.mass.music.database.delete(DB_TABLE_ALBUM_TRACKS, {"album_id": db_id})
        # delete entry(s) from album artists table
        await self.mass.music.database.delete(DB_TABLE_ALBUM_ARTISTS, {"album_id": db_id})

    async def set_release_group(
        self,
        album_item_id: int,
        release_group_mbid: str,
    ) -> None:
        """
        Persist a MusicBrainz release-group ID on a library album, idempotently.

        :param album_item_id: Library album item_id (database id).
        :param release_group_mbid: MusicBrainz release-group UUID to set.
        """
        if not release_group_mbid:
            return
        try:
            album = await self.get_library_item(album_item_id)
        except MusicAssistantError as err:
            self.logger.debug("set_release_group: cannot load album %s: %s", album_item_id, err)
            return
        # Refuse to overwrite — keeps tag-sourced or already-enriched IDs authoritative.
        if album.get_external_id(ExternalID.MB_RELEASEGROUP):
            self.logger.debug(
                "set_release_group: album %s already has MB_RELEASEGROUP — keeping",
                album_item_id,
            )
            return
        album.add_external_id(ExternalID.MB_RELEASEGROUP, release_group_mbid)
        await self.update_item_in_library(album_item_id, album)
        self.logger.debug(
            "set_release_group: wrote %s onto album %s", release_group_mbid, album_item_id
        )

    async def tracks(
        self,
        item_id: str,
        provider_instance_id_or_domain: str,
        in_library_only: bool = False,
        search: str | None = None,
        sort_field: SortField | None = None,
        sort_direction: SortDirection | None = None,
        limit: int | None = 500,
        offset: int = 0,
    ) -> list[Track]:
        """
        Return the tracks of an album, searched, sorted and paged.

        The whole album is assembled from the library and the providers once and served
        from the cache for a while; a refresh of the album assembles it anew.

        :param item_id: The album id on the provider, or its library id.
        :param provider_instance_id_or_domain: The provider the id belongs to, or "library".
        :param in_library_only: Only list the tracks that are in the library.
        :param search: Only list the tracks whose name, album name or artist name contains
            this text.
        :param sort_field: Sort field, the disc and track order (TRACK_NUMBER) when omitted.
        :param sort_direction: Sort direction, the field's default when omitted.
        :param limit: Maximum number of tracks to return; 0 returns them all.
        :param offset: Number of tracks to skip.
        :raises InvalidDataError: When the sort field is not offered for album tracks.
        """
        # an unsupported sort is rejected before the album is assembled
        sort_field, sort_direction = resolve_listing_sort(
            ListingType.ALBUM_TRACKS, sort_field, sort_direction
        )
        # always check if we have a library item for this album
        library_album = await self.get_library_item_by_prov_id(
            item_id, provider_instance_id_or_domain
        )
        if not library_album:
            # keyed and fetched by the one instance the selector resolves to
            provider = self.mass.get_provider(provider_instance_id_or_domain)
            if provider is None:
                return []
            tracks = await cached_listing(
                self.mass,
                ListingType.ALBUM_TRACKS,
                create_uri(MediaType.ALBUM, provider.instance_id, item_id),
                lambda: self._list_provider_album(item_id, provider.instance_id),
                item_type=Track,
            )
        else:
            # respect the current user's provider filter (if any) for both the
            # in-library tracks and the live provider fetches
            allowed_providers = self._ensure_provider_filter(None)
            if in_library_only:
                tracks = await self.get_library_album_tracks(
                    library_album.item_id, provider_filter=allowed_providers
                )
            else:
                tracks = await cached_listing(
                    self.mass,
                    ListingType.ALBUM_TRACKS,
                    cast("str", library_album.uri),
                    lambda: self._assemble_album_tracks(library_album, allowed_providers),
                    item_type=Track,
                    narrowed_to=allowed_providers,
                )
        return apply_listing(
            tracks,
            ListingType.ALBUM_TRACKS,
            search=search,
            sort_field=sort_field,
            sort_direction=sort_direction,
            limit=limit,
            offset=offset,
        )

    async def versions(
        self,
        item_id: str,
        provider_instance_id_or_domain: str,
    ) -> UniqueList[Album]:
        """Return all versions of an album we can find on all providers."""
        album = await self.get_provider_item(item_id, provider_instance_id_or_domain)
        streaming_search_query = (
            f"{album.artists[0].name} - {album.name}" if album.artists else album.name
        )
        result: UniqueList[Album] = UniqueList()
        for provider_id in self.mass.music.get_unique_providers():
            provider = self.mass.get_provider(provider_id)
            if not provider or not isinstance(provider, MusicProvider):
                continue
            if MediaType.ALBUM not in provider.supported_media_types:
                continue
            # TODO: filter by artists in db for non-streaming providers
            search_query = streaming_search_query if provider.is_streaming_provider else album.name
            result.extend(
                prov_item
                for prov_item in await self.search(search_query, provider_id)
                if loose_compare_strings(album.name, prov_item.name)
                and compare_artists(prov_item.artists, album.artists, any_match=True)
                # make sure that the 'base' version is NOT included
                and not album.provider_mappings.intersection(prov_item.provider_mappings)
            )
            if ProviderFeature.ALBUM_VERSIONS in provider.supported_features:
                # Call the specialized function in addition to searching to
                # handle cases where the provider hasn't merged the album
                # variants
                if mapped_id := next(
                    (
                        p.item_id
                        for p in album.provider_mappings
                        if p.provider_instance == provider.instance_id
                    ),
                    None,
                ):
                    result.extend(await provider.get_album_versions(mapped_id))
        return result

    async def resolve_musicbrainz_release_group(
        self, release_group_id: str, allow_update_metadata: bool = True
    ) -> Album:
        """
        Return the album a MusicBrainz release group is, on one of the user's music providers.

        The group's likeliest official editions are tried in turn, and the album is the first
        one a music provider has, found through the links MusicBrainz keeps or, failing
        those, by the edition's barcode. An album already in the library is returned as the
        library album.

        :param release_group_id: MusicBrainz release group id.
        :param allow_update_metadata: Whether the album's metadata may be refreshed on the way.
        :raises ProviderUnavailableError: The MusicBrainz provider is not loaded.
        :raises MediaNotFoundError: None of the user's music providers has the album.
        """
        musicbrainz = cast("MusicbrainzProvider | None", self.mass.get_provider("musicbrainz"))
        if musicbrainz is None:
            raise ProviderUnavailableError("MusicBrainz is not available")
        # a heavily reissued album has more editions than one page holds, of which the
        # likeliest few are wanted rather than the whole set
        editions = [
            release
            for release in await musicbrainz.browse_releases_by_release_group(
                release_group_id, complete=False
            )
            if release.status == "Official"
        ]
        # the digital editions of one group carry different ids on the providers, so each
        # edition's own links and barcode are tried, never those of the whole group; the
        # editions linked to the user's own services go first, as only those resolve by link
        services = {source.domain for source in self.mass.music.providers if source.available}
        ranked = sorted(editions, key=lambda edition: _streaming_edition_rank(edition, services))
        for edition in ranked[:_MAX_EDITION_LOOKUPS]:
            try:
                release = await musicbrainz.get_release_details(edition.id)
            except InvalidDataError as err:
                # a stale edition costs nothing but its turn
                self.logger.debug("Release %s could not be looked up: %s", edition.id, err)
                continue
            # a link names one loaded instance of a service, while the user may only be
            # handed an album from a music source it may see
            linked = _within_sources(
                await provider_mappings_from_urls(
                    self.mass, relation_urls(release.relations), MediaType.ALBUM, set()
                ),
                self.mass.music.providers,
            )
            if album := await self._first_available_album(
                linked, release_group_id, allow_update_metadata
            ):
                return album
            # a barcode lookup fans out over every provider, so it is spent only when none
            # of the links resolves; a linked provider is asked too, as its link may be stale
            by_barcode = await self._album_candidates_by_barcode(release)
            if album := await self._first_available_album(
                by_barcode, release_group_id, allow_update_metadata
            ):
                return album
        msg = f"Release group {release_group_id} is not available on any music provider"
        raise MediaNotFoundError(
            msg,
            translation_key="album_not_available_on_music_services",
            translation_owner=self.translation_owner,
        )

    async def get_library_album_tracks(
        self,
        item_id: str | int,
        provider_filter: list[str] | None = None,
    ) -> list[Track]:
        """
        Return in-database album tracks for the given database album.

        :param item_id: The library item ID of the album.
        :param provider_filter: Optional provider instance ID(s) to limit the result to.
        """
        db_id = int(item_id)  # ensure integer
        # pass the album id as preferred album so the track_album subquery in the
        # base query returns this album's disc/track numbers for tracks that
        # appear on multiple albums
        return await self.mass.music.tracks.get_library_items_by_query(
            provider_filter=provider_filter,
            extra_query_parts=[
                f"tracks.item_id IN (SELECT track_id FROM {DB_TABLE_ALBUM_TRACKS} "
                "WHERE album_id = :album_id)"
            ],
            extra_query_params={"album_id": db_id, "preferred_album_id": db_id},
        )

    async def link_album_tracks(
        self,
        album: Album,
        db_tracks: Sequence[Track],
        release: MusicBrainzRelease | None,
        *,
        link_providers: bool = True,
    ) -> None:
        """
        Carry an album's MusicBrainz identity and provider links over to its library tracks.

        The library tracks at a matching position on the release get its recording id and
        ISRCs. For each provider the album is mapped to but one or more of its library
        tracks are not, the provider's tracklist is matched to the library tracks by ISRC
        or position and the matching provider tracks are linked.

        :param album: The library album.
        :param db_tracks: The album's library tracks.
        :param release: The album's MusicBrainz release, if it was identified.
        :param link_providers: Whether to link the tracks to the album's providers too.
        """
        if not db_tracks:
            return
        # one tracklist per streaming service, its first mapping standing for it; a local
        # server pushes its own track mappings when it syncs, so its tracklist is left alone
        album_mappings: dict[str, ProviderMapping] = {}
        for mapping in sorted(
            album.provider_mappings,
            key=lambda x: (x.provider_domain, x.provider_instance, x.item_id),
        ):
            provider = self.mass.get_provider(
                mapping.provider_instance, provider_type=MusicProvider
            )
            if provider is None or not provider.is_streaming_provider:
                continue
            album_mappings.setdefault(mapping.provider_domain, mapping)
        track_domains = [
            {x.provider_domain for x in track.provider_mappings} for track in db_tracks
        ]
        async with self.mass.music.database.deferred_commit():
            if release is not None:
                await self._link_tracks_to_release(db_tracks, release)
            if not link_providers:
                return
            for domain, mapping in album_mappings.items():
                if all(domain in domains for domains in track_domains):
                    continue
                await self._link_tracks_to_provider_album(db_tracks, mapping)

    async def add_item_mapping_as_album_to_library(self, item: ItemMapping) -> Album:
        """
        Add an ItemMapping as an Album to the library.

        This is only used in special occasions as is basically adds an album
        to the db without a lot of mandatory data, such as artists.
        """
        album = self.album_from_item_mapping(item)
        return await self.add_item_to_library(album)

    async def match_provider(
        self, db_album: Album, provider: MusicProvider, strict: bool = True
    ) -> list[ProviderMapping]:
        """
        Try to find a match on the given (streaming) provider for a (database) album.

        Links albums of different providers/qualities together. A provider that supports
        barcode lookups is asked for the album by barcode before it is searched. Sparse
        provider search results only rule out a confident non-match; a candidate that
        still looks ambiguous is confirmed against the full provider album, its tracklist
        and, as a last resort, MusicBrainz before its provider mapping is accepted.
        """
        return await self._match_provider(db_album, provider, strict, _BaseTracksMemo())

    async def match_providers(self, db_album: Album) -> None:
        """
        Try to find match on all (streaming) providers for the provided (database) album.

        This is used to link objects of different providers/qualities together.
        """
        if db_album.provider != "library":
            return  # Matching only supported for database items
        if not db_album.artists:
            return  # guard

        # resolve the base tracklist at most once for the whole match operation
        base_tracks_memo = _BaseTracksMemo()
        # try to find match on all providers
        cur_provider_domains = {
            x.provider_domain for x in db_album.provider_mappings if x.available
        }
        # the links MusicBrainz keeps name the album on the other providers outright, so
        # those providers are linked first and only the remaining ones are searched
        if musicbrainz := self._musicbrainz_link_provider():
            cur_provider_domains |= await self._link_musicbrainz_entity(
                db_album, lambda: musicbrainz.resolve_release(db_album)
            )
        for provider in self.mass.music.providers:
            if provider.domain in cur_provider_domains:
                continue
            if ProviderFeature.SEARCH not in provider.supported_features:
                continue
            if MediaType.ALBUM not in provider.supported_media_types:
                continue
            if not provider.is_streaming_provider:
                # matching on unique providers is pointless as they push (all) their content to MA
                continue
            if match := await self._match_provider(db_album, provider, True, base_tracks_memo):
                # 100% match, we update the db with the additional provider mapping(s)
                await self.add_provider_mappings(db_album.item_id, match)
                cur_provider_domains.add(provider.domain)

    def album_from_item_mapping(self, item: ItemMapping) -> Album:
        """Create an Album object from an ItemMapping object."""
        # a library item mapping references an item already in the library, which has no
        # mapping to itself, so only a resolvable provider yields a real provider mapping
        provider_mappings: list[dict[str, Any]] = []
        if prov := self.mass.get_provider(item.provider):
            provider_mappings.append(
                {
                    "item_id": item.item_id,
                    "provider_domain": prov.domain,
                    "provider_instance": prov.instance_id,
                    "available": item.available,
                }
            )
        return Album.from_dict({**item.to_dict(), "provider_mappings": provider_mappings})

    async def _add_library_item(self, item: Album, overwrite_existing: bool = False) -> int:
        """Add a new record to the database."""
        if not isinstance(item, Album):  # TODO: Remove this once the codebase is fully typed
            msg = "Not a valid Album object (ItemMapping can not be added to db)"  # type: ignore[unreachable]
            raise InvalidDataError(msg)
        db_id = await self.mass.music.database.insert(
            self.db_table,
            {
                "name": item.name,
                "sort_name": item.sort_name,
                "version": item.version,
                "album_type": item.album_type,
                "year": item.year,
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
        await self._set_album_artists(db_id, item.artists)
        self.logger.debug("added %s to database (id: %s)", item.name, db_id)
        return db_id

    async def _update_library_item(
        self, item_id: str | int, update: Album, overwrite: bool = False
    ) -> None:
        """Update existing record in the database."""
        db_id = int(item_id)  # ensure integer
        cur_item = await self.get_library_item(db_id)
        metadata = metadata_for_update(cur_item.metadata, update.metadata, overwrite)
        if getattr(update, "album_type", AlbumType.UNKNOWN) != AlbumType.UNKNOWN:
            album_type = update.album_type
        else:
            album_type = cur_item.album_type
        cur_item.external_ids.update(update.external_ids)
        name = update.name if overwrite else cur_item.name
        sort_name = update.sort_name if overwrite else cur_item.sort_name or update.sort_name
        await self.mass.music.database.update(
            self.db_table,
            {"item_id": db_id},
            {
                "name": name,
                "sort_name": sort_name,
                "version": (update.version or cur_item.version)
                if overwrite
                else (cur_item.version or update.version),
                "year": (update.year or cur_item.year)
                if overwrite
                else (cur_item.year or update.year),
                "album_type": album_type.value,
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
        # set album artist(s)
        artists = update.artists if overwrite else cur_item.artists + update.artists
        await self._set_album_artists(db_id, artists, overwrite=overwrite)
        self.logger.debug("updated %s in database: (id %s)", update.name, db_id)

    async def _get_provider_album_tracks(
        self, item_id: str, provider_instance_id_or_domain: str
    ) -> list[Track]:
        """Return album tracks for the given provider album id."""
        if prov := self.mass.get_provider(provider_instance_id_or_domain):
            prov = cast("MusicProvider", prov)
            return await prov.get_album_tracks(item_id)
        return []

    async def _list_provider_album(self, item_id: str, provider_instance_id: str) -> Listing[Track]:
        """Return the tracks of an album that is not in the library, as the provider lists them."""
        album_tracks = await self._get_provider_album_tracks(item_id, provider_instance_id)
        await self._backfill_album_on_tracks(album_tracks, item_id, provider_instance_id)
        return Listing(album_tracks)

    async def _assemble_album_tracks(
        self, library_album: Album, allowed_providers: list[str] | None
    ) -> Listing[Track]:
        """
        Return the tracks of a library album: its library tracks plus what its providers add.

        A failing provider lookup leaves the listing incomplete, and is only raised when it
        leaves nothing to play.

        :param library_album: The library album.
        :param allowed_providers: The provider instances the listing is limited to, if any.
        """
        db_items = await self.get_library_album_tracks(
            library_album.item_id, provider_filter=allowed_providers
        )
        # return all (unique) items from all providers
        # because we are returning the items from all providers combined,
        # we need to make sure that we don't return duplicates
        listings: list[list[Track]] = []
        lookup_error: Exception | None = None
        fetched: set[tuple[str, str]] = set()
        for provider_mapping in library_album.provider_mappings:
            if not provider_mapping.available or (
                allowed_providers is not None
                and provider_mapping.provider_instance not in allowed_providers
            ):
                continue
            # an unavailable mapped instance hands the lookup to another account of the
            # service, which would list the album a second time over
            own_instance = self.mass.get_provider(provider_mapping.provider_instance)
            own_lookup = (
                own_instance is not None
                and own_instance.instance_id == provider_mapping.provider_instance
            )
            listing = (
                own_instance.instance_id if own_instance else provider_mapping.provider_instance,
                provider_mapping.item_id,
            )
            if listing in fetched:
                continue
            fetched.add(listing)
            try:
                provider_tracks = await self._get_provider_album_tracks(
                    provider_mapping.item_id, provider_mapping.provider_instance
                )
            except PROVIDER_FETCH_ERRORS as err:
                # one failing provider must not take the whole album down: the tracks
                # from the library and the other providers are still playable
                lookup_error = err
                if own_lookup and isinstance(err, MediaNotFoundError):
                    await self.mass.music.mark_provider_mapping_unavailable(
                        library_album, provider_mapping
                    )
                self.logger.log(
                    provider_fetch_log_level(err),
                    "Unable to fetch tracks for album %s from provider %s: %s",
                    library_album.name,
                    provider_mapping.provider_instance,
                    err,
                )
                continue
            listings.append(provider_tracks)
        for db_track, source in album_track_backfills(db_items, listings):
            await self._set_album_track(
                db_id=int(library_album.item_id),
                db_track_id=int(db_track.item_id),
                track=source,
            )
            db_track.disc_number = source.disc_number
            db_track.track_number = source.track_number
        result: list[Track] = list(db_items)
        for provider_track in select_album_tracks(db_items, listings):
            provider_track.album = library_album
            result.append(provider_track)
        if lookup_error is not None and not any(track.available for track in result):
            # nothing could be played at all, so surface the reason instead of an empty list
            raise lookup_error
        return Listing(result, complete=lookup_error is None)

    async def _backfill_album_on_tracks(
        self, album_tracks: list[Track], item_id: str, provider_instance_id_or_domain: str
    ) -> None:
        """
        Fill in the parent album and its image on provider album tracks that omit them.

        :param album_tracks: The album tracks as listed by the provider.
        :param item_id: The provider album id.
        :param provider_instance_id_or_domain: The provider the album tracks come from.
        """
        # some album-track listings omit the parent album and its image; backfill both
        # from the provider album so the queue shows the album name and artwork.
        if not album_tracks or (album_tracks[0].album and album_tracks[0].image):
            return
        prov_album = await self.get_provider_item(item_id, provider_instance_id_or_domain)
        album_mapping = ItemMapping.from_item(prov_album)
        for track in album_tracks:
            if prov_album.image and not track.image:
                track.metadata.add_image(prov_album.image)
            if track.album is None:
                track.album = album_mapping

    async def _verify_musicbrainz_mapping(self, mapping: ProviderMapping) -> bool:
        """Return True if a linked album exists on the provider, checked for Apple Music only."""
        # MusicBrainz links Apple Music albums per storefront, so a linked album may not
        # exist in the user's storefront; the other providers' catalogs are worldwide
        if mapping.provider_domain != "apple_music":
            return True
        try:
            await self.get_provider_item(
                mapping.item_id, mapping.provider_instance, allow_fallback=False
            )
        except MusicAssistantError, aiohttp.ClientError, TimeoutError:
            return False
        return True

    async def _link_tracks_to_release(
        self, db_tracks: Sequence[Track], release: MusicBrainzRelease
    ) -> None:
        """Fill the recording ids and ISRCs of a release in on the library tracks at its positions."""
        recordings = {
            (medium.position, track.position): track.recording
            for medium in release.media
            for track in medium.tracks
            if track.position and track.recording
        }
        for db_track in db_tracks:
            # a digital release stores its single disc as disc 0 or 1
            recording = recordings.get((db_track.disc_number or 1, db_track.track_number))
            if recording is None or not _recording_matches_track(recording, db_track):
                continue
            changed = fill_track_from_recording(db_track, recording)
            if not changed and db_track.metadata.last_musicbrainz_lookup is not None:
                continue
            db_track.metadata.last_musicbrainz_lookup = int(time())
            await self.mass.music.tracks.update_item_in_library(db_track.item_id, db_track)

    async def _link_tracks_to_provider_album(
        self, db_tracks: Sequence[Track], mapping: ProviderMapping
    ) -> None:
        """Link the tracks of a provider album to the library tracks they are."""
        try:
            provider_tracks = await self._get_provider_album_tracks(
                mapping.item_id, mapping.provider_instance
            )
        except PROVIDER_FETCH_ERRORS as err:
            self.logger.debug(
                "Album tracks unavailable for %s on %s: %s",
                mapping.item_id,
                mapping.provider_instance,
                err,
            )
            return
        for db_track in db_tracks:
            if any(
                x.provider_domain == mapping.provider_domain for x in db_track.provider_mappings
            ):
                continue
            if provider_track := _matching_provider_track(db_track, provider_tracks):
                await self.mass.music.tracks.add_unclaimed_provider_mappings(
                    db_track.item_id, provider_track.provider_mappings
                )

    def _library_match_names(self, item: Album | ItemMapping) -> list[str]:
        """Return the normalized album names, with and without a spelled-out retail suffix."""
        base_name = create_safe_string(strip_album_retail_suffix(item.name), True, True)
        return [base_name, *(f"{base_name}{suffix}" for suffix in ALBUM_RETAIL_SUFFIX_KEYS)]

    async def _confirm_library_candidate(self, db_item: Album, item: Album | ItemMapping) -> bool:
        """
        Return True if a library album is the same album as the one being added.

        An edition that cannot be decided on the albums' own metadata is escalated to
        tracklists and MusicBrainz, so an ambiguous album is linked to the album it
        belongs to instead of becoming a second library entry.
        """
        if not isinstance(item, Album):
            return await super()._confirm_library_candidate(db_item, item)
        evidence = compare_album_evidence(db_item, item, strict=True)
        if evidence != AlbumMatchEvidence.INSUFFICIENT:
            return evidence == AlbumMatchEvidence.MATCH
        provider = self.mass.get_provider(item.provider, provider_type=MusicProvider)
        if provider is None or provider.instance_id != item.provider:
            # only the exact provider instance the album came from may be fingerprinted,
            # never a same-domain fallback pointing at a different account/server
            return False
        evidence = await self._resolve_album_evidence(
            db_item, item, provider, True, _BaseTracksMemo()
        )
        return evidence == AlbumMatchEvidence.MATCH

    async def _match_provider(
        self,
        db_album: Album,
        provider: MusicProvider,
        strict: bool,
        base_tracks_memo: _BaseTracksMemo,
    ) -> list[ProviderMapping]:
        """Match one provider by barcode, then by search, and return the confirmed mappings."""
        self.logger.debug("Trying to match album %s on provider %s", db_album.name, provider.name)
        matches: list[ProviderMapping] = []
        if ProviderFeature.ALBUM_BY_EXTERNAL_ID in provider.supported_features:
            matches = await self._match_provider_by_barcode(
                db_album, provider, strict, base_tracks_memo
            )
        # a barcode hit makes the search unnecessary
        search_results: list[Album] = []
        if not matches:
            search_str = (
                f"{db_album.artists[0].name} - {db_album.name}"
                if db_album.artists
                else db_album.name
            )
            search_results = await self.search(search_str, provider.instance_id)
        for search_result_item in search_results:
            if not search_result_item.available:
                continue
            # a sparse search result only rules out a confident non-match; a MATCH or an
            # ambiguous (INSUFFICIENT) candidate is confirmed against the full album below
            if (
                compare_album_evidence(db_album, search_result_item, strict=strict)
                == AlbumMatchEvidence.NO_MATCH
            ):
                continue
            # search results can be simplified objects, so fetch the full provider album
            prov_album = await self.get_provider_item(
                search_result_item.item_id,
                search_result_item.provider,
                fallback=search_result_item,
            )
            evidence = await self._resolve_album_evidence(
                db_album, prov_album, provider, strict, base_tracks_memo
            )
            if evidence == AlbumMatchEvidence.MATCH:
                matches.extend(prov_album.provider_mappings)
        if not matches:
            self.logger.debug(
                "Could not find match for Album %s on provider %s",
                db_album.name,
                provider.name,
            )
        return matches

    async def _match_provider_by_barcode(
        self,
        db_album: Album,
        provider: MusicProvider,
        strict: bool,
        base_tracks_memo: _BaseTracksMemo,
    ) -> list[ProviderMapping]:
        """Return the mappings of the provider album one of the base album's barcodes resolves to."""
        # the order only makes the choice of looked-up barcodes deterministic
        for barcode in sorted(_canonical_album_barcodes(db_album))[:MAX_EXTERNAL_ID_MATCH_LOOKUPS]:
            try:
                prov_album = await provider.get_album_by_external_id(barcode, ExternalID.BARCODE)
                if prov_album is None:
                    continue
                # a lookup result can be a simplified object, so fetch the full provider album
                prov_album = await self.get_provider_item(
                    prov_album.item_id, prov_album.provider, fallback=prov_album
                )
            except EXTERNAL_ID_LOOKUP_ERRORS as err:
                self.logger.debug(
                    "Barcode %s lookup on provider %s failed: %s", barcode, provider.name, err
                )
                continue
            if not prov_album.available:
                continue
            # the queried barcode is the query, not evidence: it is left out of the scored
            # copy so name, artist, year and the tracklist decide, while a second,
            # independently agreeing barcode still counts
            candidate = replace(
                prov_album,
                external_ids={
                    (kind, value)
                    for kind, value in prov_album.external_ids
                    if not (kind == ExternalID.BARCODE and barcode_to_upc(value) == barcode)
                },
            )
            evidence = await self._resolve_album_evidence(
                db_album, candidate, provider, strict, base_tracks_memo
            )
            if evidence == AlbumMatchEvidence.MATCH:
                return list(prov_album.provider_mappings)
        return []

    async def _album_candidates_by_barcode(
        self, release: MusicBrainzRelease
    ) -> list[ProviderMapping]:
        """
        Return the mappings of the albums the music providers find by a release's barcode.

        Only an album that is the release, by title and primary artist, counts.

        :param release: The MusicBrainz release, with its barcode and artist credits.
        """
        if not release.barcode or not is_valid_barcode(release.barcode):
            return []
        upc = barcode_to_upc(release.barcode)
        providers = [
            provider
            for provider in self.mass.music.providers
            if provider.supports_feature(ProviderFeature.ALBUM_BY_EXTERNAL_ID)
        ]
        hits = await asyncio.gather(
            *(self._album_by_barcode(provider, upc) for provider in providers)
        )
        candidates: list[ProviderMapping] = []
        for provider, hit in zip(providers, hits, strict=True):
            if hit is None:
                continue
            # a barcode gets reused, and a provider answers with the first album carrying it
            if not release_matches_album(release, hit):
                self.logger.debug(
                    "Barcode %s on provider %s is album %s, not %s",
                    upc,
                    provider.name,
                    hit.name,
                    release.title,
                )
                continue
            candidates.extend(hit.provider_mappings)
        return candidates

    async def _album_by_barcode(self, provider: MusicProvider, upc: str) -> Album | None:
        """Return the album a provider finds by a barcode, if it has one and answers."""
        try:
            return await provider.get_album_by_external_id(upc, ExternalID.BARCODE)
        except EXTERNAL_ID_LOOKUP_ERRORS as err:
            self.logger.debug(
                "Barcode %s lookup on provider %s failed: %s", upc, provider.name, err
            )
            return None

    async def _first_available_album(
        self,
        candidates: Sequence[ProviderMapping],
        release_group_id: str,
        allow_update_metadata: bool,
    ) -> Album | None:
        """Return the first candidate its provider still serves as an album, if any."""
        for candidate in candidates:
            try:
                if library_album := await self.get_library_item_by_prov_id(
                    candidate.item_id, candidate.provider_instance
                ):
                    return await self.get(
                        library_album.item_id,
                        "library",
                        allow_update_metadata=allow_update_metadata,
                    )
                # the candidate names one of the user's own sources, so an unavailable one
                # must not fall back to another account of the same service
                return await self.get_provider_item(
                    candidate.item_id,
                    candidate.provider_instance,
                    allow_fallback=False,
                    strict_provider_instance=True,
                )
            except EXTERNAL_ID_LOOKUP_ERRORS as err:
                self.logger.debug(
                    "Release group %s is not available as album %s on %s: %s",
                    release_group_id,
                    candidate.item_id,
                    candidate.provider_instance,
                    err,
                )
        return None

    async def _resolve_album_evidence(
        self,
        db_album: Album,
        prov_album: Album,
        provider: MusicProvider,
        strict: bool,
        base_tracks_memo: _BaseTracksMemo,
    ) -> AlbumMatchEvidence:
        """
        Return the match evidence for a fully-fetched provider album.

        An ambiguous album is escalated to ordered track fingerprints and, only if those
        stay inconclusive, to MusicBrainz; a mapping is accepted only on a MATCH.

        :param provider: The exact provider instance the candidate album was matched on;
            its tracklist is fetched directly so a same-domain fallback can never
            fingerprint the candidate against a different account/server.
        """
        evidence = compare_album_evidence(db_album, prov_album, strict=strict)
        if evidence != AlbumMatchEvidence.INSUFFICIENT:
            return evidence
        # ambiguous metadata: resolve conservatively with ordered track fingerprints
        base_tracks = await self._resolve_base_album_tracks(db_album, base_tracks_memo)
        try:
            compare_tracks = await provider.get_album_tracks(prov_album.item_id)
        except PROVIDER_FETCH_ERRORS as err:
            # the candidate tracklist is unavailable: treat it as absent and let MusicBrainz decide
            self.logger.debug(
                "Album tracks unavailable for %s on %s: %s",
                prov_album.item_id,
                provider.instance_id,
                err,
            )
            compare_tracks = []
        evidence = compare_album_evidence(
            db_album,
            prov_album,
            strict=strict,
            base_tracks=base_tracks,
            compare_tracks=compare_tracks,
        )
        if evidence != AlbumMatchEvidence.INSUFFICIENT:
            return evidence
        # tracklists could not resolve it either: consult MusicBrainz as a last resort
        return await self._musicbrainz_album_evidence(db_album, prov_album)

    async def _resolve_base_album_tracks(
        self, db_album: Album, base_tracks_memo: _BaseTracksMemo
    ) -> list[Track] | None:
        """Return the memoized base tracklist, resolving it once on first use."""
        if not base_tracks_memo.resolved:
            base_tracks_memo.tracks = await self._load_base_album_tracks(db_album)
            base_tracks_memo.resolved = True
        return base_tracks_memo.tracks

    async def _load_base_album_tracks(self, db_album: Album) -> list[Track] | None:
        """
        Return a complete, ordered base tracklist to fingerprint against.

        Iterates the album's existing provider mappings in a deterministic order and
        returns the first loaded provider's full tracklist whose disc/track positions can
        be trusted. A provider-sourced tracklist is used rather than the stored library
        tracks because those can be an incomplete subset (individually added tracks), and
        an incomplete base would make a track-count difference look like a real conflict.
        """
        for mapping in sorted(
            db_album.provider_mappings,
            key=lambda mapping: (
                mapping.provider_domain,
                mapping.provider_instance,
                mapping.item_id,
            ),
        ):
            if not mapping.available:
                continue
            provider = self.mass.get_provider(mapping.provider_instance, return_unavailable=True)
            if (
                provider is None
                or provider.instance_id != mapping.provider_instance
                or not provider.available
            ):
                # only trust the exact, currently-available provider instance and never a
                # same-domain fallback pointing at a different account/server
                continue
            try:
                provider_tracks = await self._get_provider_album_tracks(
                    mapping.item_id, mapping.provider_instance
                )
            except PROVIDER_FETCH_ERRORS as err:
                # this mapping's tracklist is unavailable: try the next existing mapping
                self.logger.debug(
                    "Base album tracks unavailable for %s on %s: %s",
                    mapping.item_id,
                    mapping.provider_instance,
                    err,
                )
                continue
            if album_tracks_have_positions(provider_tracks):
                return provider_tracks
        return None

    async def _musicbrainz_album_evidence(
        self, base_album: Album, compare_album: Album
    ) -> AlbumMatchEvidence:
        """
        Return album match evidence from MusicBrainz release identity, or abstain.

        A barcode that resolves unambiguously to a single specific MusicBrainz release on
        both albums is strong positive evidence; barcodes belonging to entirely different
        release groups are negative. A barcode resolving to several releases, a shared
        release group alone, an unresolved barcode or a lookup failure abstains
        (INSUFFICIENT) rather than guessing.
        """
        base_barcodes = _canonical_album_barcodes(base_album)
        compare_barcodes = _canonical_album_barcodes(compare_album)
        if not base_barcodes or not compare_barcodes:
            return AlbumMatchEvidence.INSUFFICIENT
        musicbrainz = self.mass.get_provider("musicbrainz")
        if musicbrainz is None:
            return AlbumMatchEvidence.INSUFFICIENT
        musicbrainz = cast("MusicbrainzProvider", musicbrainz)
        releases_by_barcode: dict[str, list[MusicBrainzBarcodeRelease]] = {}
        try:
            for barcode in sorted(base_barcodes | compare_barcodes):
                releases_by_barcode[barcode] = await musicbrainz.get_releases_by_barcode(barcode)
        except _MUSICBRAINZ_LOOKUP_ERRORS as err:
            self.logger.debug(
                "MusicBrainz barcode lookup failed while matching album %s: %s",
                base_album.name,
                err,
            )
            return AlbumMatchEvidence.INSUFFICIENT
        base_release_ids = _unambiguous_release_ids(base_barcodes, releases_by_barcode)
        compare_release_ids = _unambiguous_release_ids(compare_barcodes, releases_by_barcode)
        if base_release_ids & compare_release_ids:
            # both albums carry a barcode that names the same single specific release
            return AlbumMatchEvidence.MATCH
        if not all(releases_by_barcode[barcode] for barcode in base_barcodes | compare_barcodes):
            # an unresolved barcode leaves the release-group sets incomplete, so a disjoint
            # comparison could wrongly reject regional equivalents: abstain instead
            return AlbumMatchEvidence.INSUFFICIENT
        base_group_ids = _release_group_ids(base_barcodes, releases_by_barcode)
        compare_group_ids = _release_group_ids(compare_barcodes, releases_by_barcode)
        if base_group_ids.isdisjoint(compare_group_ids):
            # the barcodes belong to entirely different release groups: different albums
            return AlbumMatchEvidence.NO_MATCH
        # a shared release group alone (or an ambiguous barcode) never identifies an edition
        return AlbumMatchEvidence.INSUFFICIENT

    async def _set_album_artists(
        self,
        db_id: int,
        artists: Iterable[Artist | ItemMapping],
        overwrite: bool = False,
    ) -> None:
        """
        Store Album Artists.

        An empty set of artists never clears the stored rows: an album that lost its
        artists disappears from their discography and is skipped by provider matching.
        """
        all_artists = list(artists)
        if not all_artists:
            if overwrite:
                # a caller asking to replace all artists with none is a bug,
                # so keep the stored rows and make the attempt visible
                self.logger.warning("Ignoring request to clear all artists of album id %s", db_id)
            return
        if overwrite:
            # on overwrite, clear the album_artists table first
            await self.mass.music.database.delete(
                DB_TABLE_ALBUM_ARTISTS,
                {
                    "album_id": db_id,
                },
            )
        for artist in all_artists:
            await self._set_album_artist(db_id, artist=artist, overwrite=overwrite)

    async def _set_album_artist(
        self, db_id: int, artist: Artist | ItemMapping, overwrite: bool = False
    ) -> ItemMapping:
        """Store Album Artist info."""
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
        # write (or update) record in album_artists table
        await self.mass.music.database.insert_or_replace(
            DB_TABLE_ALBUM_ARTISTS,
            {
                "album_id": db_id,
                "artist_id": int(db_artist.item_id),
            },
        )
        return ItemMapping.from_item(db_artist)

    async def _set_album_track(self, db_id: int, db_track_id: int, track: Track) -> None:
        """Store Album Track info."""
        # write (or update) record in album_tracks table
        await self.mass.music.database.insert_or_replace(
            DB_TABLE_ALBUM_TRACKS,
            {
                "album_id": db_id,
                "track_id": db_track_id,
                "track_number": track.track_number,
                "disc_number": track.disc_number,
            },
        )

    def _get_sort_sql(self, field: SortField, direction: SortDirection | None) -> str:
        """Return the ORDER BY clause for a sort field, ARTIST_NAME through the artists join."""
        if field == SortField.ARTIST_NAME:
            if direction == SortDirection.DESC:
                return "artists.search_name DESC, year DESC"
            return "artists.search_name ASC, year DESC"
        return super()._get_sort_sql(field, direction)

    def _parse_summary_row(
        self, db_row: Mapping[str, Any], hidden_sources: set[str]
    ) -> AlbumSummary:
        """Parse a raw summary db row into an AlbumSummary object."""
        item = cast("AlbumSummary", super()._parse_summary_row(db_row, hidden_sources))
        item.version = db_row["version"] or ""
        item.year = db_row["year"]
        item.album_type = AlbumType(db_row["album_type"])
        item.artists = self._parse_summary_artist_mappings(db_row)
        return item


def _canonical_album_barcodes(album: Album) -> set[str]:
    """Return an album's valid barcodes in canonical UPC form."""
    return {
        barcode_to_upc(value)
        for external_id_type, value in album.external_ids
        if external_id_type == ExternalID.BARCODE and is_valid_barcode(value)
    }


def _streaming_edition_rank(
    release: MusicBrainzBarcodeRelease, services: set[str]
) -> tuple[bool, bool, bool, bool, str]:
    """
    Return the sort key ranking a group's official editions, the one the user's services carry first.

    :param release: The edition as the release group browse lists it.
    :param services: The domains of the music services the user may see.
    """
    linked = {
        service
        for url in relation_urls(release.relations)
        if (service := share_url_provider(url, MediaType.ALBUM))
    }
    return (
        not linked & services,
        not is_digital_release(release),
        not linked,
        release.country not in ("XW", "XE"),
        release.date or "9999",
    )


def _within_sources(
    candidates: Iterable[ProviderMapping], sources: Sequence[MusicProvider]
) -> list[ProviderMapping]:
    """
    Return the candidates that name one of the given music sources.

    A candidate stays on its own instance when that is an available source; otherwise it
    moves to the first available instance the sources have of its service, failing that to
    the first there is. One on a service they do not include is left out.
    """
    available = {source.instance_id for source in sources if source.available}
    first_by_domain: dict[str, str] = {}
    for source in sorted(sources, key=lambda source: not source.available):
        first_by_domain.setdefault(source.domain, source.instance_id)
    within: list[ProviderMapping] = []
    for candidate in candidates:
        if candidate.provider_instance in available:
            within.append(candidate)
        elif instance := first_by_domain.get(candidate.provider_domain):
            within.append(replace(candidate, provider_instance=instance))
    return within


def _unambiguous_release_ids(
    barcodes: set[str], releases_by_barcode: dict[str, list[MusicBrainzBarcodeRelease]]
) -> set[str]:
    """Return release ids that at least one of the barcodes resolves to unambiguously."""
    release_ids: set[str] = set()
    for barcode in barcodes:
        resolved = {release.id for release in releases_by_barcode.get(barcode, [])}
        # only a barcode that maps to exactly one specific release is trustworthy evidence
        if len(resolved) == 1:
            release_ids |= resolved
    return release_ids


def _release_group_ids(
    barcodes: set[str], releases_by_barcode: dict[str, list[MusicBrainzBarcodeRelease]]
) -> set[str]:
    """Return every release-group id the barcodes resolve to."""
    return {
        release.release_group.id
        for barcode in barcodes
        for release in releases_by_barcode.get(barcode, [])
    }


def _recording_matches_track(recording: MusicBrainzRecording, track: Track) -> bool:
    """Return whether a release's recording is the given library track, by title and length."""
    if not compare_strings(recording.title, track.name, strict=False):
        return False
    if recording.length is None or not track.duration:
        return True
    return abs(recording.length / 1000 - track.duration) <= _TRACK_DURATION_TOLERANCE


def _matching_provider_track(db_track: Track, provider_tracks: Sequence[Track]) -> Track | None:
    """Return the provider track that is the given library track: by ISRC, else by position."""
    isrcs = _isrcs(db_track)
    if isrcs:
        # an ISRC is occasionally reused, so the durations must agree as well
        for provider_track in provider_tracks:
            if isrcs & _isrcs(provider_track) and _durations_agree(db_track, provider_track):
                return provider_track
    if not db_track.track_number:
        return None
    # a digital release stores its single disc as disc 0 or 1
    position = (db_track.disc_number or 1, db_track.track_number)
    for provider_track in provider_tracks:
        if (
            (provider_track.disc_number or 1, provider_track.track_number) == position
            and compare_strings(provider_track.name, db_track.name, strict=False)
            and _durations_agree(db_track, provider_track)
        ):
            return provider_track
    return None


def _durations_agree(track: Track, other: Track) -> bool:
    """Return whether two tracks' durations are within tolerance, an unknown duration passing."""
    if not track.duration or not other.duration:
        return True
    return abs(track.duration - other.duration) <= _TRACK_DURATION_TOLERANCE


def _isrcs(track: Track) -> set[str]:
    """Return a track's valid ISRCs in canonical form."""
    return {
        normalize_external_id(ExternalID.ISRC, value)
        for id_type, value in track.external_ids
        if id_type == ExternalID.ISRC and is_valid_isrc(value)
    }
