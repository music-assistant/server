"""Manage MediaItems of type Artist."""

from __future__ import annotations

import asyncio
import contextlib
from itertools import zip_longest
from typing import TYPE_CHECKING, Any, Literal, NamedTuple, cast, overload

from music_assistant_models.auth import Scope
from music_assistant_models.enums import (
    AlbumType,
    ArtistType,
    ExternalID,
    ImageType,
    ListingType,
    MediaType,
    ProviderFeature,
    ProviderType,
    SortDirection,
    SortField,
)
from music_assistant_models.errors import (
    InvalidDataError,
    MediaNotFoundError,
    MusicAssistantError,
    ProviderUnavailableError,
)
from music_assistant_models.helpers import create_safe_string
from music_assistant_models.media_items import (
    Album,
    Artist,
    ArtistSummary,
    Audiobook,
    ItemMapping,
    MediaCollection,
    MediaItemImage,
    ProviderMapping,
    Track,
    UniqueList,
)

from music_assistant.constants import (
    DB_TABLE_ALBUM_ARTISTS,
    DB_TABLE_ALBUM_TRACKS,
    DB_TABLE_ARTISTS,
    DB_TABLE_AUDIOBOOK_ARTISTS,
    DB_TABLE_PROVIDER_MAPPINGS,
    DB_TABLE_TRACK_ARTISTS,
    VARIOUS_ARTISTS_MBID,
    VARIOUS_ARTISTS_NAME,
)
from music_assistant.controllers.music.constants import SIMILAR_ITEMS_LIMIT
from music_assistant.controllers.music.helpers import (
    metadata_for_update,
    provider_mappings_for_update,
)
from music_assistant.controllers.music.listing import apply_listing, resolve_listing_sort
from music_assistant.controllers.music.listing_cache import Listing
from music_assistant.helpers.collections import has_available_item
from music_assistant.helpers.compare import (
    compare_album,
    compare_album_name,
    compare_artist,
    compare_strings,
    compare_track,
)
from music_assistant.helpers.database import UNSET
from music_assistant.helpers.json import serialize_to_json
from music_assistant.models.music_provider import (
    PROVIDER_FETCH_ERRORS,
    MusicProvider,
    provider_fetch_log_level,
)

from .base import MediaControllerBase

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Container, Mapping, Sequence

    from music_assistant import MusicAssistant
    from music_assistant.models.media_capabilities import MusicDiscoveryMixin
    from music_assistant.providers.musicbrainz import MusicbrainzProvider, MusicBrainzReleaseGroup


class ArtistsController(MediaControllerBase[Artist]):
    """Controller managing MediaItems of type Artist."""

    db_table = DB_TABLE_ARTISTS
    media_type = MediaType.ARTIST
    item_cls = Artist
    summary_item_cls = ArtistSummary

    def __init__(self, mass: MusicAssistant) -> None:
        """Initialize class."""
        super().__init__(mass)
        self._db_add_lock = asyncio.Lock()
        # register (extra) api handlers
        api_base = self.api_base
        self.mass.register_api_command(
            f"music/{api_base}/artist_albums", self.albums, required_scope=Scope.LIBRARY_READ
        )
        self.mass.register_api_command(
            f"music/{api_base}/artist_tracks", self.tracks, required_scope=Scope.LIBRARY_READ
        )
        self.mass.register_api_command(
            f"music/{api_base}/artist_tracks_count",
            self.tracks_count,
            required_scope=Scope.LIBRARY_READ,
        )
        self.mass.register_api_command(
            f"music/{api_base}/artist_albums_count",
            self.albums_count,
            required_scope=Scope.LIBRARY_READ,
        )
        self.mass.register_api_command(
            f"music/{api_base}/artist_appears_on",
            self.appears_on,
            required_scope=Scope.LIBRARY_READ,
        )
        self.mass.register_api_command(
            f"music/{api_base}/artist_appears_on_count",
            self.appears_on_count,
            required_scope=Scope.LIBRARY_READ,
        )
        self.mass.register_api_command(
            f"music/{api_base}/discography", self.discography, required_scope=Scope.LIBRARY_READ
        )
        self.mass.register_api_command(
            f"music/{api_base}/discography_count",
            self.discography_count,
            required_scope=Scope.LIBRARY_READ,
        )
        self.mass.register_api_command(
            f"music/{api_base}/top_tracks", self.top_tracks, required_scope=Scope.LIBRARY_READ
        )
        self.mass.register_api_command(
            f"music/{api_base}/top_albums", self.top_albums, required_scope=Scope.LIBRARY_READ
        )
        self.mass.register_api_command(
            f"music/{api_base}/artist_audiobooks",
            self.audiobooks,
            required_scope=Scope.LIBRARY_READ,
        )
        self.mass.register_api_command(
            f"music/{api_base}/similar_artists",
            self.similar_artists,
            required_scope=Scope.LIBRARY_READ,
        )
        self.mass.register_api_command(
            f"music/{api_base}/library_artist_types",
            self.get_library_artist_types,
            required_scope=Scope.LIBRARY_READ,
        )

    @property
    def summary_query(self) -> tuple[str, dict[str, Any]]:
        """Return the slim SELECT query used for artist summary listings."""
        query = f"""
        SELECT
            {self._summary_base_columns()},
            artists.artist_type,
            {self._provider_mappings_query()} AS provider_mappings
            FROM artists"""
        return query, {}

    async def library_count(
        self,
        favorite_only: bool = False,
        album_artists_only: bool = False,
        artist_type: ArtistType | None = None,
    ) -> int:
        """
        Return the number of artists in the library.

        Restricted to the providers the current user is allowed to see when that user
        has a provider filter set.

        :param favorite_only: Only count the artists the current user likes.
        :param album_artists_only: Only count artists credited on an album in the library.
        :param artist_type: Only count artists of this type.
        """
        sql_query = f"SELECT item_id FROM {self.db_table}"
        query_parts = []
        query_params: dict[str, Any] = {}
        if artist_type:
            query_parts.append(f"artist_type = '{artist_type}'")
        if favorite_only:
            query_parts.append(self._favorite_filter_clause(query_params, True))
        if album_artists_only:
            query_parts.append(self._album_artist_clause(query_params))
        if provider_filter := self._ensure_provider_filter(None):
            query_parts.append(
                self._provider_filter_clause(query_params, provider_filter, in_library_only=True)
            )
        if query_parts:
            sql_query += f" WHERE {' AND '.join(query_parts)}"
        return await self.mass.music.database.get_count_from_query(sql_query, query_params)

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
        album_artists_only: bool = False,
        artist_type: ArtistType | None = None,
        *,
        sort_field: SortField | None = None,
        sort_direction: SortDirection | None = None,
        summary: bool = True,
        reachable_via: list[str] | None = None,
        random_seed: int | None = None,
        **kwargs: Any,
    ) -> list[Artist]:
        """
        Get in-database (album) artists.

        :param favorite: Only include the current user's likes (True) or dislikes (False).
        :param search: Filter by search query.
        :param limit: Maximum number of items to return.
        :param offset: Number of items to skip.
        :param random_seed: Keep random ordering stable across pages with the same seed.
        :param order_by: DEPRECATED - use sort_field and sort_direction instead.
        :param provider: Filter by provider instance ID (single string or list).
        :param genre: Filter by genre id(s).
        :param played_only: Filter to only played artists.
        :param album_artists_only: Only return artists credited on an album in the library.
        :param artist_type: The artist's type
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
        if artist_type:
            extra_query_parts = [f"artist_type = '{artist_type}'"]
        if album_artists_only and artist_type in (None, ArtistType.SINGER):
            extra_query_parts.append(self._album_artist_clause(extra_query_params))
        return await self.get_library_items_by_query(
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
            played_only=played_only,
            in_library_only=True,
            summary=summary,
            reachable_via=reachable_via,
            random_seed=random_seed,
        )

    async def tracks(
        self,
        item_id: str,
        provider_instance_id_or_domain: str,
        provider_filter: str | None = None,
        search: str | None = None,
        sort_field: SortField | None = None,
        sort_direction: SortDirection | None = None,
        limit: int | None = 500,
        offset: int = 0,
        favorite: bool | None = None,
        random_seed: int | None = None,
    ) -> list[Track]:
        """
        Return the tracks of an artist, searched, sorted and paged.

        For a library item, the in-library tracks are returned, optionally limited to a single
        provider instance with the provider_filter. For a provider item, that provider's
        tracks listing is returned (which may be empty if it is not supported), assembled once
        and served from the cache for a while.

        :param item_id: The item ID of the artist.
        :param provider_instance_id_or_domain: The provider instance ID or domain of the artist.
        :param provider_filter: Optional provider instance ID to limit the (library) result to.
        :param search: Only list the tracks whose name, album name or artist name contains
            this text.
        :param sort_field: Sort field, the sort name when omitted.
        :param sort_direction: Sort direction, the field's default when omitted.
        :param limit: Maximum number of tracks to return; 0 returns them all.
        :param offset: Number of tracks to skip.
        :param random_seed: Keep random ordering stable across pages with the same seed.
        :param favorite: Only list the tracks the current user likes (True) or dislikes (False).
        :raises InvalidDataError: When the sort field is not offered for artist tracks.
        """
        # an unsupported sort is rejected before the tracks are assembled
        sort_field, sort_direction = resolve_listing_sort(
            ListingType.ARTIST_TRACKS, sort_field, sort_direction
        )
        if provider_instance_id_or_domain == "library":
            tracks = await self.get_library_artist_tracks(item_id, provider_filter=provider_filter)
        else:
            self._validate_provider_filter(provider_instance_id_or_domain, provider_filter)
            tracks = await self._cached_item_listing(
                ListingType.ARTIST_TRACKS,
                item_id,
                provider_instance_id_or_domain,
                lambda instance_id: self.get_provider_artist_tracks(item_id, instance_id),
                item_type=Track,
                # without an artist tracks listing the artist's albums are listed, whose
                # library tracks follow the calling user's music sources
                narrowed_to=self._ensure_provider_filter(None),
            )
        if favorite is not None:
            tracks = [track for track in tracks if track.favorite is favorite]
        return apply_listing(
            tracks,
            ListingType.ARTIST_TRACKS,
            search=search,
            sort_field=sort_field,
            sort_direction=sort_direction,
            limit=limit,
            offset=offset,
            random_seed=random_seed,
        )

    async def tracks_count(
        self,
        item_id: str,
        provider_instance_id_or_domain: str,
        provider_filter: str | None = None,
        search: str | None = None,
        favorite: bool | None = None,
    ) -> int:
        """
        Return the number of tracks in an artist listing.

        :param item_id: The artist item ID.
        :param provider_instance_id_or_domain: The artist's provider or ``library``.
        :param provider_filter: Restrict library tracks to this provider instance.
        :param search: Apply the artist track search.
        :param favorite: Only include the calling user's likes or dislikes.
        """
        # the listing search also matches album and artist names, which the SQL search does
        # not, and a provider artist has no library rows: both count the cached listing
        if provider_instance_id_or_domain != "library" or search:
            return len(
                await self.tracks(
                    item_id,
                    provider_instance_id_or_domain,
                    provider_filter=provider_filter,
                    search=search,
                    limit=0,
                    favorite=favorite,
                )
            )
        artist = await self.get_library_item(item_id)
        if artist.artist_type != ArtistType.SINGER:
            return 0
        return await self.mass.music.tracks.get_library_items_count_by_query(
            favorite=favorite,
            provider_filter=self._ensure_provider_filter(provider_filter),
            extra_query_parts=[
                f"tracks.item_id in (select track_id from {DB_TABLE_TRACK_ARTISTS} "
                "where artist_id = :artist_id)"
            ],
            extra_query_params={"artist_id": int(item_id)},
            in_library_only=True,
        )

    async def albums(  # noqa: PLR0913
        self,
        item_id: str,
        provider_instance_id_or_domain: str,
        provider_filter: str | None = None,
        search: str | None = None,
        sort_field: SortField | None = None,
        sort_direction: SortDirection | None = None,
        limit: int | None = 500,
        offset: int = 0,
        album_types: list[AlbumType] | None = None,
        favorite: bool | None = None,
        random_seed: int | None = None,
    ) -> list[Album]:
        """
        Return the albums of an artist, searched, sorted and paged.

        For a library item, the in-library albums are returned, optionally limited to a single
        provider instance with the provider_filter. For a provider item, that provider's
        albums listing is returned (which may be empty if it is not supported), assembled once
        and served from the cache for a while.

        :param item_id: The item ID of the artist.
        :param provider_instance_id_or_domain: The provider instance ID or domain of the artist.
        :param provider_filter: Optional provider instance ID to limit the (library) result to.
        :param search: Only list the albums whose name or artist name contains this text.
        :param sort_field: Sort field, the sort name when omitted.
        :param sort_direction: Sort direction, the field's default when omitted.
        :param limit: Maximum number of albums to return; 0 returns them all.
        :param offset: Number of albums to skip.
        :param random_seed: Keep random ordering stable across pages with the same seed.
        :param album_types: Only list albums whose type is included.
        :param favorite: Only list the albums the current user likes (True) or dislikes (False).
        :raises InvalidDataError: When the sort field is not offered for artist albums.
        """
        sort_field, sort_direction = resolve_listing_sort(
            ListingType.ARTIST_ALBUMS, sort_field, sort_direction
        )
        if provider_instance_id_or_domain == "library":
            albums = await self.get_library_artist_albums(item_id, provider_filter=provider_filter)
        else:
            self._validate_provider_filter(provider_instance_id_or_domain, provider_filter)
            albums = await self._cached_item_listing(
                ListingType.ARTIST_ALBUMS,
                item_id,
                provider_instance_id_or_domain,
                lambda instance_id: self.get_provider_artist_albums(item_id, instance_id),
                item_type=Album,
            )
        if album_types:
            albums = [album for album in albums if album.album_type in album_types]
        if favorite is not None:
            albums = [album for album in albums if album.favorite is favorite]
        return apply_listing(
            albums,
            ListingType.ARTIST_ALBUMS,
            search=search,
            sort_field=sort_field,
            sort_direction=sort_direction,
            limit=limit,
            offset=offset,
            random_seed=random_seed,
        )

    async def albums_count(
        self,
        item_id: str,
        provider_instance_id_or_domain: str,
        provider_filter: str | None = None,
        search: str | None = None,
        album_types: list[AlbumType] | None = None,
        favorite: bool | None = None,
    ) -> int:
        """
        Return the number of albums in an artist listing.

        :param item_id: The artist item ID.
        :param provider_instance_id_or_domain: The artist's provider or ``library``.
        :param provider_filter: Restrict library albums to this provider instance.
        :param search: Apply the artist album search.
        :param album_types: Restrict the count to these album types.
        :param favorite: Only include the calling user's likes or dislikes.
        """
        # the listing search also matches artist names, which the SQL search does not, and a
        # provider artist has no library rows: both count the cached listing
        if provider_instance_id_or_domain != "library" or search:
            return len(
                await self.albums(
                    item_id,
                    provider_instance_id_or_domain,
                    provider_filter=provider_filter,
                    search=search,
                    limit=0,
                    album_types=album_types,
                    favorite=favorite,
                )
            )
        artist = await self.get_library_item(item_id)
        if artist.artist_type != ArtistType.SINGER:
            return 0
        query_parts = [
            f"albums.item_id in (select album_id from {DB_TABLE_ALBUM_ARTISTS} "
            "where artist_id = :artist_id)"
        ]
        query_params: dict[str, Any] = {"artist_id": int(item_id)}
        if album_types:
            query_parts.append("albums.album_type in :album_types")
            query_params["album_types"] = [album_type.value for album_type in album_types]
        return await self.mass.music.albums.get_library_items_count_by_query(
            favorite=favorite,
            provider_filter=self._ensure_provider_filter(provider_filter),
            extra_query_parts=query_parts,
            extra_query_params=query_params,
            in_library_only=True,
        )

    async def appears_on(
        self,
        item_id: str,
        provider_instance_id_or_domain: str,
        provider_filter: str | None = None,
        search: str | None = None,
        sort_field: SortField | None = None,
        sort_direction: SortDirection | None = None,
        limit: int | None = 500,
        offset: int = 0,
        random_seed: int | None = None,
    ) -> list[Album]:
        """
        Return the albums an artist appears on without being an album artist.

        These are the albums of the artist's library tracks, newest first by default, as
        summary items. Only available for library artists; empty for a provider item.

        :param item_id: The item ID of the artist.
        :param provider_instance_id_or_domain: The provider instance ID or domain of the artist.
        :param provider_filter: Optional provider instance ID to limit the tracks to.
        :param search: Only list the albums whose name or artist name contains this text.
        :param sort_field: Sort field, newest first (ORIGINAL) when omitted.
        :param sort_direction: Sort direction, the field's default when omitted.
        :param limit: Maximum number of albums to return; 0 returns them all.
        :param offset: Number of albums to skip.
        :param random_seed: Keep random ordering stable across pages with the same seed.
        :raises InvalidDataError: When the sort field is not offered for this listing.
        """
        sort_field, sort_direction = resolve_listing_sort(
            ListingType.ARTIST_APPEARS_ON, sort_field, sort_direction
        )
        if provider_instance_id_or_domain != "library":
            return []
        return apply_listing(
            await self.get_library_artist_appears_on(item_id, provider_filter=provider_filter),
            ListingType.ARTIST_APPEARS_ON,
            search=search,
            sort_field=sort_field,
            sort_direction=sort_direction,
            limit=limit,
            offset=offset,
            random_seed=random_seed,
        )

    async def appears_on_count(
        self,
        item_id: str,
        provider_instance_id_or_domain: str,
        provider_filter: str | None = None,
        search: str | None = None,
    ) -> int:
        """
        Return the number of albums an artist appears on without being an album artist.

        :param item_id: The artist item ID.
        :param provider_instance_id_or_domain: The artist's provider or ``library``.
        :param provider_filter: Restrict to tracks from this provider instance.
        :param search: Apply the appears-on album search.
        """
        if provider_instance_id_or_domain != "library":
            return 0
        # the listing search also matches artist names, which the SQL search does not
        if search:
            return len(
                await self.appears_on(
                    item_id,
                    provider_instance_id_or_domain,
                    provider_filter=provider_filter,
                    search=search,
                    limit=0,
                )
            )
        query = await self._get_library_artist_appears_on_query(item_id, provider_filter)
        if query is None:
            return 0
        query_parts, query_params = query
        return await self.mass.music.albums.get_library_items_count_by_query(
            extra_query_parts=query_parts,
            extra_query_params=query_params,
        )

    async def discography(
        self,
        item_id: str,
        provider_instance_id_or_domain: str,
        search: str | None = None,
        sort_field: SortField | None = None,
        sort_direction: SortDirection | None = None,
        limit: int | None = 500,
        offset: int = 0,
        random_seed: int | None = None,
    ) -> list[Album]:
        """
        Return a library artist's discography as MusicBrainz knows it, searched, sorted and paged.

        Every album, EP and single credited to the artist is listed, newest first by default.
        The ones in the library are the library albums themselves; the others are MusicBrainz
        albums, which resolve to the user's music providers when opened or added to the library.

        :param item_id: The library item id of the artist.
        :param provider_instance_id_or_domain: The provider of the artist, must be ``library``.
        :param search: Only list the albums whose name or artist name contains this text.
        :param sort_field: Sort field, newest first (ORIGINAL) when omitted.
        :param sort_direction: Sort direction, the field's default when omitted.
        :param limit: Maximum number of albums to return; 0 returns them all.
        :param offset: Number of albums to skip.
        :param random_seed: Keep random ordering stable across pages with the same seed.
        :raises InvalidDataError: When the artist is not a library artist, or the sort field
            is not offered for a discography.
        """
        sort_field, sort_direction = resolve_listing_sort(
            ListingType.ARTIST_DISCOGRAPHY, sort_field, sort_direction
        )
        data = await self._get_artist_discography_data(
            item_id, provider_instance_id_or_domain, include_library_albums=True
        )
        if data is None:
            return []
        artist, musicbrainz, library_albums, release_groups = data
        albums_by_release_group: dict[str, Album] = {}
        unidentified_albums: list[Album] = []
        for album in library_albums:
            if release_group_id := album.get_external_id(ExternalID.MB_RELEASEGROUP):
                albums_by_release_group[release_group_id] = album
            else:
                unidentified_albums.append(album)
        for album in unidentified_albums:
            if group := _release_group_of(
                album,
                release_groups,
                albums_by_release_group,
                musicbrainz.album_type_from_release_group,
            ):
                albums_by_release_group[group.id] = album
        artist_mapping = ItemMapping.from_item(artist)
        # the Cover Art Archive provider, if loaded, fetches a cover once its image is shown,
        # so the listing itself costs no lookups
        cover_provider = "coverartarchive" if self.mass.get_provider("coverartarchive") else None
        discography: list[Album] = []
        for group in release_groups:
            if library_album := albums_by_release_group.get(group.id):
                discography.append(library_album)
                continue
            discography.append(
                _album_from_release_group(
                    group,
                    artist_mapping,
                    musicbrainz.album_type_from_release_group(group),
                    cover_provider,
                )
            )
        return apply_listing(
            discography,
            ListingType.ARTIST_DISCOGRAPHY,
            search=search,
            sort_field=sort_field,
            sort_direction=sort_direction,
            limit=limit,
            offset=offset,
            random_seed=random_seed,
        )

    async def discography_count(
        self,
        item_id: str,
        provider_instance_id_or_domain: str,
        search: str | None = None,
    ) -> int:
        """
        Return the number of releases in a library artist's discography.

        :param item_id: The library artist ID.
        :param provider_instance_id_or_domain: Must be ``library``.
        :param search: Apply the discography search.
        """
        # the search runs over the assembled listing; without one only the release groups count
        if search:
            return len(
                await self.discography(
                    item_id,
                    provider_instance_id_or_domain,
                    search=search,
                    limit=0,
                )
            )
        data = await self._get_artist_discography_data(
            item_id, provider_instance_id_or_domain, include_library_albums=False
        )
        return len(data.release_groups) if data else 0

    async def top_tracks(
        self,
        item_id: str,
        provider_instance_id_or_domain: str,
        provider_filter: str | None = None,
        search: str | None = None,
        sort_field: SortField | None = None,
        sort_direction: SortDirection | None = None,
        limit: int | None = 500,
        offset: int = 0,
        random_seed: int | None = None,
    ) -> list[Track]:
        """
        Return the top/featured tracks of an artist, searched, sorted and paged.

        For a library item, the top tracks of the artist's music providers are aggregated (and
        deduplicated), falling back to metadata providers when those have none, optionally
        limited to a single provider instance. For a provider item, that provider's top tracks
        listing is returned (may be empty if not supported). Either is assembled once and
        served from the cache for a while.

        :param item_id: The item ID of the artist.
        :param provider_instance_id_or_domain: The provider instance ID or domain of the artist.
        :param provider_filter: Optional provider instance ID to limit the result to.
        :param search: Only list the tracks whose name, album name or artist name contains
            this text.
        :param sort_field: Sort field, the providers' ranking (ORIGINAL) when omitted.
        :param sort_direction: Sort direction, the field's default when omitted.
        :param limit: Maximum number of tracks to return; 0 returns them all.
        :param offset: Number of tracks to skip.
        :param random_seed: Keep random ordering stable across pages with the same seed.
        :raises InvalidDataError: When the sort field is not offered for top tracks.
        """
        sort_field, sort_direction = resolve_listing_sort(
            ListingType.ARTIST_TOP_TRACKS, sort_field, sort_direction
        )
        if provider_instance_id_or_domain == "library":
            tracks = await self._cached_item_listing(
                ListingType.ARTIST_TOP_TRACKS,
                item_id,
                provider_instance_id_or_domain,
                lambda _library: self.get_library_artist_toptracks(item_id, provider_filter),
                item_type=Track,
                narrowed_to=self._ensure_provider_filter(provider_filter),
            )
        else:
            self._validate_provider_filter(provider_instance_id_or_domain, provider_filter)
            tracks = await self._cached_item_listing(
                ListingType.ARTIST_TOP_TRACKS,
                item_id,
                provider_instance_id_or_domain,
                lambda instance_id: self.get_provider_artist_toptracks(item_id, instance_id),
                item_type=Track,
            )
        return apply_listing(
            tracks,
            ListingType.ARTIST_TOP_TRACKS,
            search=search,
            sort_field=sort_field,
            sort_direction=sort_direction,
            limit=limit,
            offset=offset,
            random_seed=random_seed,
        )

    async def top_albums(
        self,
        item_id: str,
        provider_instance_id_or_domain: str,
        provider_filter: str | None = None,
        search: str | None = None,
        sort_field: SortField | None = None,
        sort_direction: SortDirection | None = None,
        limit: int | None = 500,
        offset: int = 0,
        random_seed: int | None = None,
    ) -> list[Album]:
        """
        Return the top/featured albums of an artist, searched, sorted and paged.

        For a library item, the top albums of all the artist's providers are aggregated (and
        deduplicated), optionally limited to a single provider instance. For a provider
        item, that provider's top albums listing is returned (may be empty if not supported).
        Either is assembled once and served from the cache for a while.

        :param item_id: The item ID of the artist.
        :param provider_instance_id_or_domain: The provider instance ID or domain of the artist.
        :param provider_filter: Optional provider instance ID to limit the result to.
        :param search: Only list the albums whose name or artist name contains this text.
        :param sort_field: Sort field, the providers' ranking (ORIGINAL) when omitted.
        :param sort_direction: Sort direction, the field's default when omitted.
        :param limit: Maximum number of albums to return; 0 returns them all.
        :param offset: Number of albums to skip.
        :param random_seed: Keep random ordering stable across pages with the same seed.
        :raises InvalidDataError: When the sort field is not offered for top albums.
        """
        sort_field, sort_direction = resolve_listing_sort(
            ListingType.ARTIST_TOP_ALBUMS, sort_field, sort_direction
        )
        if provider_instance_id_or_domain == "library":
            albums = await self._cached_item_listing(
                ListingType.ARTIST_TOP_ALBUMS,
                item_id,
                provider_instance_id_or_domain,
                lambda _library: self.get_library_artist_topalbums(item_id, provider_filter),
                item_type=Album,
                narrowed_to=self._ensure_provider_filter(provider_filter),
            )
        else:
            self._validate_provider_filter(provider_instance_id_or_domain, provider_filter)
            albums = await self._cached_item_listing(
                ListingType.ARTIST_TOP_ALBUMS,
                item_id,
                provider_instance_id_or_domain,
                lambda instance_id: self.get_provider_artist_topalbums(item_id, instance_id),
                item_type=Album,
            )
        return apply_listing(
            albums,
            ListingType.ARTIST_TOP_ALBUMS,
            search=search,
            sort_field=sort_field,
            sort_direction=sort_direction,
            limit=limit,
            offset=offset,
            random_seed=random_seed,
        )

    async def similar_artists(
        self,
        item_id: str,
        provider_instance_id_or_domain: str,
        provider_filter: str | None = None,
        limit: int | None = 500,
        search: str | None = None,
        sort_field: SortField | None = None,
        sort_direction: SortDirection | None = None,
        offset: int = 0,
        random_seed: int | None = None,
    ) -> list[Artist]:
        """
        Return the artists similar to an artist, searched, sorted and paged.

        For a library item, the similar artists of the artist's music providers are aggregated
        (and deduplicated), falling back to metadata providers when those have none, optionally
        limited to a single provider instance. For a provider item, that provider's similar
        artists listing is returned (may be empty if not supported). Either holds at most
        SIMILAR_ITEMS_LIMIT artists, assembled once and served from the cache for a while.

        :param item_id: The item ID of the artist.
        :param provider_instance_id_or_domain: The provider instance ID or domain of the artist.
        :param provider_filter: Optional provider instance ID to limit the result to.
        :param limit: Maximum number of similar artists to return; 0 returns them all.
        :param search: Only list the artists whose name contains this text.
        :param sort_field: Sort field, the providers' ranking (ORIGINAL) when omitted.
        :param sort_direction: Sort direction, the field's default when omitted.
        :param offset: Number of similar artists to skip.
        :param random_seed: Keep random ordering stable across pages with the same seed.
        :raises InvalidDataError: When the sort field is not offered for similar artists.
        """
        sort_field, sort_direction = resolve_listing_sort(
            ListingType.SIMILAR_ARTISTS, sort_field, sort_direction
        )
        if provider_instance_id_or_domain == "library":
            artists = await self._cached_item_listing(
                ListingType.SIMILAR_ARTISTS,
                item_id,
                provider_instance_id_or_domain,
                lambda _library: self.get_library_artist_similar_artists(
                    item_id, provider_filter, limit=SIMILAR_ITEMS_LIMIT
                ),
                item_type=Artist,
                narrowed_to=self._ensure_provider_filter(provider_filter),
            )
        else:
            self._validate_provider_filter(provider_instance_id_or_domain, provider_filter)
            artists = await self._cached_item_listing(
                ListingType.SIMILAR_ARTISTS,
                item_id,
                provider_instance_id_or_domain,
                lambda instance_id: self.get_provider_artist_similar_artists(
                    item_id, instance_id, limit=SIMILAR_ITEMS_LIMIT
                ),
                item_type=Artist,
            )
        return apply_listing(
            artists,
            ListingType.SIMILAR_ARTISTS,
            search=search,
            sort_field=sort_field,
            sort_direction=sort_direction,
            limit=limit,
            offset=offset,
            random_seed=random_seed,
        )

    if TYPE_CHECKING:

        @overload
        async def audiobooks(
            self,
            item_id: str,
            provider_instance_id_or_domain: str,
            artist_type: ArtistType = ArtistType.AUTHOR,
            in_library_only: bool = False,
            search: str | None = None,
            sort_field: SortField | None = None,
            sort_direction: SortDirection | None = None,
            limit: int | None = 500,
            offset: int = 0,
            *,
            provider_filter: str | None = None,
            collapse_collections: Literal[False] = False,
            random_seed: int | None = None,
        ) -> list[Audiobook]: ...

        @overload
        async def audiobooks(
            self,
            item_id: str,
            provider_instance_id_or_domain: str,
            artist_type: ArtistType = ArtistType.AUTHOR,
            in_library_only: bool = False,
            search: str | None = None,
            sort_field: SortField | None = None,
            sort_direction: SortDirection | None = None,
            limit: int | None = 500,
            offset: int = 0,
            *,
            provider_filter: str | None = None,
            collapse_collections: Literal[True],
            random_seed: int | None = None,
        ) -> list[Audiobook | MediaCollection[Audiobook]]: ...

    async def audiobooks(  # noqa: PLR0913
        self,
        item_id: str,
        provider_instance_id_or_domain: str,
        artist_type: ArtistType = ArtistType.AUTHOR,
        in_library_only: bool = False,
        search: str | None = None,
        sort_field: SortField | None = None,
        sort_direction: SortDirection | None = None,
        limit: int | None = 500,
        offset: int = 0,
        *,
        provider_filter: str | None = None,
        collapse_collections: bool = False,
        random_seed: int | None = None,
    ) -> list[Audiobook] | list[Audiobook | MediaCollection[Audiobook]]:
        """
        Return the audiobooks of an artist, searched, sorted and paged.

        Artist_type can be omitted for in-library artists.

        :param item_id: The item ID of the artist.
        :param provider_instance_id_or_domain: The provider instance ID or domain of the artist.
        :param artist_type: Whether the artist is listed as the author or the narrator.
        :param in_library_only: Only list the audiobooks that are in the library.
        :param search: Only list the audiobooks (or collections) whose name contains this text.
        :param sort_field: Sort field, the sort name when omitted; a collection has no
            duration of its own and sorts as the shortest.
        :param sort_direction: Sort direction, the field's default when omitted.
        :param limit: Maximum number of audiobooks to return; 0 returns them all.
        :param offset: Number of audiobooks to skip.
        :param random_seed: Keep random ordering stable across pages with the same seed.
        :param provider_filter: Only list audiobooks from this provider instance.
        :param collapse_collections: Collapse available collections. Only applies to
            in-library items; when in_library_only is False, provider items are
            appended as plain audiobooks alongside the collapsed collections.
        :raises InvalidDataError: When the sort field is not offered for artist audiobooks.
        """
        sort_field, sort_direction = resolve_listing_sort(
            ListingType.ARTIST_AUDIOBOOKS, sort_field, sort_direction
        )

        def _listed(
            items: Sequence[Audiobook | MediaCollection[Audiobook]],
        ) -> list[Audiobook | MediaCollection[Audiobook]]:
            return apply_listing(
                items,
                ListingType.ARTIST_AUDIOBOOKS,
                search=search,
                sort_field=sort_field,
                sort_direction=sort_direction,
                limit=limit,
                offset=offset,
                random_seed=random_seed,
            )

        if artist_type == ArtistType.SINGER:
            self.logger.warning("Audiobooks not supported for artist_type SINGER.")
            return []
        # always check if we have a library item for this artist
        library_artist = await self.get_library_item_by_prov_id(
            item_id, provider_instance_id_or_domain
        )
        if library_artist and library_artist.artist_type == ArtistType.SINGER:
            self.logger.debug(
                "Ignoring audiobook request for artist of type %s", library_artist.artist_type
            )
            return []
        if not library_artist:
            self._validate_provider_filter(provider_instance_id_or_domain, provider_filter)
            if artist_type == ArtistType.AUTHOR:
                return _listed(
                    await self.get_provider_author_audiobooks(
                        item_id, provider_instance_id_or_domain
                    )
                )
            if artist_type == ArtistType.NARRATOR:
                return _listed(
                    await self.get_provider_narrator_audiobooks(
                        item_id, provider_instance_id_or_domain
                    )
                )
            return []

        db_items = await self.get_library_author_narrator_audiobooks(
            library_artist.item_id,
            artist_type=library_artist.artist_type,
            collapse_collections=collapse_collections,
            provider_filter=provider_filter,
        )
        result: list[Audiobook] | list[Audiobook | MediaCollection[Audiobook]] = db_items
        if in_library_only:
            # return in-library items only
            return _listed(result)
        # return all (unique) items from all providers
        # initialize unique_ids with db_items to prevent duplicates
        unique_ids: set[str] = set()
        for item in db_items:
            if isinstance(item, MediaCollection):
                for collection_item in item.items:
                    unique_ids.add(f"{collection_item.name}.{collection_item.version}")
            else:
                unique_ids.add(f"{item.name}.{item.version}")
        unique_providers = (
            [provider_filter] if provider_filter else self.mass.music.get_unique_providers()
        )
        audiobook_method = (
            self.get_provider_author_audiobooks
            if artist_type == ArtistType.AUTHOR
            else self.get_provider_narrator_audiobooks
        )
        provider_error: Exception | None = None
        for provider_mapping in library_artist.provider_mappings:
            if provider_mapping.provider_instance not in unique_providers:
                continue
            try:
                provider_audiobooks = await audiobook_method(
                    provider_mapping.item_id, provider_mapping.provider_instance
                )
            except PROVIDER_FETCH_ERRORS as err:
                # one failing provider must not take the whole listing down: the audiobooks
                # from the library and the other providers are still playable
                provider_error = err
                self.logger.log(
                    provider_fetch_log_level(err),
                    "Unable to fetch audiobooks for %s from provider %s: %s",
                    library_artist.name,
                    provider_mapping.provider_instance,
                    err,
                )
                continue
            for provider_audiobook in provider_audiobooks:
                unique_id = f"{provider_audiobook.name}.{provider_audiobook.version}"
                if unique_id in unique_ids:
                    continue
                unique_ids.add(unique_id)
                # prefer db item
                if db_item := await self.mass.music.audiobooks.get_library_item_by_prov_id(
                    provider_audiobook.item_id, provider_audiobook.provider
                ):
                    result.append(db_item)
                elif not in_library_only:
                    result.append(provider_audiobook)
        if provider_error is not None and not has_available_item(result):
            # nothing could be played at all, so surface the reason instead of an empty list
            raise provider_error
        return _listed(result)

    async def get_library_author_narrator_audiobooks(
        self,
        item_id: str | int,
        artist_type: ArtistType,
        *,
        collapse_collections: bool = False,
        provider_filter: str | None = None,
    ) -> list[Audiobook] | list[Audiobook | MediaCollection[Audiobook]]:
        """Return all in-library audiobooks for an author/ narrator."""
        db_id = int(item_id)  # ensure integer
        library_item = await self.get_library_item(db_id)
        if library_item.artist_type != artist_type:
            self.logger.debug("Audiobooks only available for artists of type %s", artist_type)
            return []
        subquery = (
            f"SELECT audiobook_id FROM {DB_TABLE_AUDIOBOOK_ARTISTS} WHERE artist_id = :artist_id"
        )
        query = f"audiobooks.item_id in ({subquery})"
        return await self.mass.music.audiobooks.get_library_items_by_query(
            limit=0,
            extra_query_parts=[query],
            extra_query_params={"artist_id": db_id},
            collapse_collections=collapse_collections,
            provider_filter=self._ensure_provider_filter(provider_filter),
        )

    async def get_provider_author_audiobooks(
        self,
        item_id: str,
        provider_instance_id_or_domain: str,
    ) -> list[Audiobook]:
        """Return audiobooks for an author on given provider."""
        assert provider_instance_id_or_domain != "library"
        if not (prov := self.mass.get_provider(provider_instance_id_or_domain)):
            return []
        prov = cast("MusicProvider", prov)
        if ProviderFeature.AUTHOR_AUDIOBOOKS in prov.supported_features:
            return await prov.get_author_audiobooks(item_id)
        # fallback implementation using the db
        return await self._get_db_author_narrator_audiobooks(
            item_id=item_id,
            provider_instance_id_or_domain=provider_instance_id_or_domain,
            artist_type=ArtistType.AUTHOR,
        )

    async def get_provider_narrator_audiobooks(
        self,
        item_id: str,
        provider_instance_id_or_domain: str,
    ) -> list[Audiobook]:
        """Return audiobooks for an author on given provider."""
        assert provider_instance_id_or_domain != "library"
        if not (prov := self.mass.get_provider(provider_instance_id_or_domain)):
            return []
        prov = cast("MusicProvider", prov)
        if ProviderFeature.NARRATOR_AUDIOBOOKS in prov.supported_features:
            return await prov.get_narrator_audiobooks(item_id)
        # fallback implementation using the db
        return await self._get_db_author_narrator_audiobooks(
            item_id=item_id,
            provider_instance_id_or_domain=provider_instance_id_or_domain,
            artist_type=ArtistType.NARRATOR,
        )

    async def get_provider_artist_toptracks(
        self,
        item_id: str,
        provider_instance_id_or_domain: str,
    ) -> list[Track]:
        """
        Return the top tracks for an artist on the given provider.

        Each track is resolved to its in-library equivalent where available.
        """
        provider = self.mass.get_provider(
            provider_instance_id_or_domain, provider_type=MusicProvider
        )
        if provider is None or not provider.available:
            return []  # guard against unavailable provider
        if not provider.supports_feature(ProviderFeature.ARTIST_TOPTRACKS):
            self.logger.warning(
                "Provider %s does not support fetching artist top tracks.",
                provider.name,
            )
            return []  # guard against unsupported feature
        tracks = await provider.get_artist_toptracks(item_id)
        # resolve to in-library equivalents (in parallel) where available
        resolved = await asyncio.gather(
            *(
                self.mass.music.tracks.get_library_item_by_prov_id(track.item_id, track.provider)
                for track in tracks
            )
        )
        return [
            library_track or track for library_track, track in zip(resolved, tracks, strict=True)
        ]

    async def get_library_artist_toptracks(
        self,
        item_id: str | int,
        provider_filter: str | None = None,
    ) -> Listing[Track]:
        """
        Return the top tracks for an in-library artist, aggregated across its providers.

        The result combines (and deduplicates, preserving order) the top tracks from the music
        providers attached to the artist. Metadata and plugin providers implementing the feature
        are only consulted when those return nothing. Empty when no provider yields a result,
        incomplete when a provider failed or is not loaded.

        :param item_id: The library item ID of the artist.
        :param provider_filter: Optional provider instance ID to limit the result to.
        """
        ref_item = await self.get_library_item(item_id)
        listings, incomplete = await self._get_library_artist_listings(
            ref_item,
            ProviderFeature.ARTIST_TOPTRACKS,
            self._ensure_provider_filter(provider_filter),
            # streaming provider results are resolved to library items
            lambda mapping: self.get_provider_artist_toptracks(
                mapping.item_id, mapping.provider_instance
            ),
            lambda prov: prov.get_artist_toptracks(ref_item),
        )
        # interleave the providers' rankings by position (zip), deduplicating with the compare
        # helper (which also matches on version/duration)
        result: list[Track] = []
        for row in zip_longest(*listings):
            for candidate in row:
                if candidate is None or any(
                    compare_track(existing, candidate) for existing in result
                ):
                    continue
                result.append(candidate)
        return Listing(result, complete=not incomplete)

    async def get_provider_artist_topalbums(
        self,
        item_id: str,
        provider_instance_id_or_domain: str,
    ) -> list[Album]:
        """
        Return the top/featured albums for an artist on the given provider.

        Each album is resolved to its in-library equivalent where available.
        """
        provider = self.mass.get_provider(
            provider_instance_id_or_domain, provider_type=MusicProvider
        )
        if provider is None or not provider.available:
            return []  # guard against unavailable provider
        if not provider.supports_feature(ProviderFeature.ARTIST_TOPALBUMS):
            self.logger.warning(
                "Provider %s does not support fetching artist top albums.",
                provider.name,
            )
            return []  # guard against unsupported feature
        albums = await provider.get_artist_topalbums(item_id)
        # resolve to in-library equivalents (in parallel) where available
        resolved = await asyncio.gather(
            *(
                self.mass.music.albums.get_library_item_by_prov_id(album.item_id, album.provider)
                for album in albums
            )
        )
        return [
            library_album or album for library_album, album in zip(resolved, albums, strict=True)
        ]

    async def get_library_artist_topalbums(
        self,
        item_id: str | int,
        provider_filter: str | None = None,
    ) -> Listing[Album]:
        """
        Return the top albums for an in-library artist, aggregated across all its providers.

        The result combines (and deduplicates, preserving order) the top albums from every
        provider attached to the artist and any metadata/plugin provider implementing the
        feature. Empty when no provider yields a result, incomplete when a provider failed or is
        not loaded.

        :param item_id: The library item ID of the artist.
        :param provider_filter: Optional provider instance ID to limit the result to.
        """
        ref_item = await self.get_library_item(item_id)
        allowed = self._ensure_provider_filter(provider_filter)
        # fetch each provider's ranked top albums in parallel
        # streaming providers attached to the artist (results resolved to library items)
        mappings, unanswered = self._provider_mappings_for_feature(
            ref_item, ProviderFeature.ARTIST_TOPALBUMS, allowed
        )
        fetches = [
            self.get_provider_artist_topalbums(mapping.item_id, mapping.provider_instance)
            for mapping in mappings
        ]
        # metadata/plugin providers implementing the feature
        for prov in self.mass.get_providers_supporting_feature(
            ProviderFeature.ARTIST_TOPALBUMS,
            priority=(ProviderType.METADATA, ProviderType.PLUGIN),
        ):
            if allowed is not None and prov.instance_id not in allowed:
                continue
            fetches.append(cast("MusicDiscoveryMixin", prov).get_artist_topalbums(ref_item))
        per_provider = await asyncio.gather(*fetches, return_exceptions=True)
        # drop (and log) any provider that failed so one bad provider can't sink the listing
        listings: list[list[Album]] = []
        failed = False
        for listing in per_provider:
            if isinstance(listing, BaseException):
                failed = True
                self.logger.warning(
                    "Error fetching top albums for artist %s from a provider",
                    ref_item.name,
                    exc_info=listing,
                )
                continue
            listings.append(listing)
        # interleave the providers' rankings by position (zip), deduplicating with the compare
        # helper (which also matches on version/duration)
        result: list[Album] = []
        for row in zip_longest(*listings):
            for candidate in row:
                if candidate is None or any(
                    compare_album(existing, candidate) for existing in result
                ):
                    continue
                result.append(candidate)
        return Listing(result, complete=not (failed or unanswered))

    async def get_provider_artist_tracks(
        self,
        item_id: str,
        provider_instance_id_or_domain: str,
    ) -> Listing[Track]:
        """
        Return all tracks for an artist on given provider.

        Without an artist tracks listing of its own, the provider's tracks of the artist's
        albums are listed: a failing album leaves the listing incomplete, and is only raised
        when it leaves nothing to play.
        """
        provider = self.mass.get_provider(
            provider_instance_id_or_domain, provider_type=MusicProvider
        )
        if provider is None or not provider.available:
            return Listing([])  # guard against unavailable provider
        if provider.supports_feature(ProviderFeature.ARTIST_TRACKS):
            return Listing(await provider.get_artist_tracks(item_id))
        # fallback: enumerate (and dedupe) the tracks of all the artist's albums on the provider
        result: list[Track] = []
        unique_ids: set[str] = set()
        provider_error: Exception | None = None
        for album in await self.get_provider_artist_albums(item_id, provider_instance_id_or_domain):
            try:
                album_tracks = await self.mass.music.albums.tracks(
                    album.item_id, album.provider, limit=None
                )
            except PROVIDER_FETCH_ERRORS as err:
                # one failing album must not drop the artist's other tracks on this provider
                provider_error = err
                self.logger.log(
                    provider_fetch_log_level(err),
                    "Unable to fetch tracks for album %s from provider %s: %s",
                    album.name,
                    provider_instance_id_or_domain,
                    err,
                )
                continue
            for track in album_tracks:
                unique_id = f"{track.name}.{track.version}"
                if unique_id in unique_ids:
                    continue
                unique_ids.add(unique_id)
                result.append(track)
        if provider_error is not None and not any(track.available for track in result):
            # nothing could be played at all, so surface the reason instead of an empty list
            raise provider_error
        return Listing(result, complete=provider_error is None)

    async def get_library_artist_tracks(
        self,
        item_id: str | int,
        provider_filter: str | None = None,
    ) -> list[Track]:
        """Return all in-library tracks for an artist, optionally limited to a single provider."""
        db_id = int(item_id)  # ensure integer
        library_item = await self.get_library_item(db_id)
        if library_item.artist_type != ArtistType.SINGER:
            self.logger.debug("Tracks only available for artists of type ARTIST")
            return []
        subquery = f"SELECT track_id FROM {DB_TABLE_TRACK_ARTISTS} WHERE artist_id = :artist_id"
        query = f"tracks.item_id in ({subquery})"
        return await self.mass.music.tracks.get_library_items_by_query(
            limit=0,
            extra_query_parts=[query],
            extra_query_params={"artist_id": db_id},
            provider_filter=self._ensure_provider_filter(provider_filter),
            in_library_only=True,
        )

    async def get_provider_artist_albums(
        self,
        item_id: str,
        provider_instance_id_or_domain: str,
    ) -> list[Album]:
        """Return albums for an artist on given provider."""
        provider = self.mass.get_provider(
            provider_instance_id_or_domain, provider_type=MusicProvider
        )
        if provider is None or not provider.available:
            return []  # guard against unavailable provider
        if not provider.supports_feature(ProviderFeature.ARTIST_ALBUMS):
            self.logger.warning(
                "Provider %s does not support fetching all artist albums.",
                provider.name,
            )
            return []  # guard against unsupported feature
        return await provider.get_artist_albums(item_id)

    async def get_library_artist_albums(
        self,
        item_id: str | int,
        provider_filter: str | None = None,
    ) -> list[Album]:
        """Return all in-library albums for an artist, optionally limited to a single provider."""
        db_id = int(item_id)  # ensure integer
        library_item = await self.get_library_item(db_id)
        if library_item.artist_type != ArtistType.SINGER:
            self.logger.debug("Albums only available for artists of type ARTIST")
            return []
        subquery = f"SELECT album_id FROM {DB_TABLE_ALBUM_ARTISTS} WHERE artist_id = :artist_id"
        query = f"albums.item_id in ({subquery})"
        return await self.mass.music.albums.get_library_items_by_query(
            limit=0,
            extra_query_parts=[query],
            extra_query_params={"artist_id": db_id},
            provider_filter=self._ensure_provider_filter(provider_filter),
            in_library_only=True,
        )

    async def get_library_artist_appears_on(
        self,
        item_id: str | int,
        provider_filter: str | None = None,
    ) -> list[Album]:
        """
        Return the albums of an artist's library tracks on which it is not an album artist.

        :param item_id: The library item ID of the artist.
        :param provider_filter: Optional provider instance ID to limit the tracks to.
        """
        query = await self._get_library_artist_appears_on_query(item_id, provider_filter)
        if query is None:
            return []
        query_parts, query_params = query
        return await self.mass.music.albums.get_library_items_by_query(
            extra_query_parts=query_parts,
            extra_query_params=query_params,
            limit=0,  # no limit, the full list is returned
            sort_field=SortField.YEAR,
            sort_direction=SortDirection.DESC,
            summary=True,
        )

    async def get_provider_artist_similar_artists(
        self,
        item_id: str,
        provider_instance_id_or_domain: str,
        limit: int = 25,
    ) -> list[Artist]:
        """
        Return similar artists for an artist on the given provider.

        Each artist is resolved to its in-library equivalent where available.
        """
        provider = self.mass.get_provider(
            provider_instance_id_or_domain, provider_type=MusicProvider
        )
        if provider is None or not provider.available:
            return []  # guard against unavailable provider
        if not provider.supports_feature(ProviderFeature.SIMILAR_ARTISTS):
            self.logger.warning(
                "Provider %s does not support fetching similar artists.",
                provider.name,
            )
            return []  # guard against unsupported feature
        artists = await provider.get_similar_artists(item_id, limit=limit)
        # resolve to in-library equivalents (in parallel) where available
        resolved = await asyncio.gather(
            *(
                self.get_library_item_by_prov_id(artist.item_id, artist.provider)
                for artist in artists
            )
        )
        return [
            library_artist or artist
            for library_artist, artist in zip(resolved, artists, strict=True)
        ]

    async def get_library_artist_similar_artists(
        self,
        item_id: str | int,
        provider_filter: str | None = None,
        limit: int = 25,
    ) -> Listing[Artist]:
        """
        Return similar artists for an in-library artist, aggregated across its providers.

        The result combines (and deduplicates, preserving order) the similar artists from the
        music providers attached to the artist. Metadata and plugin providers implementing the
        feature are only consulted when those return nothing. Empty when no provider yields a
        result, incomplete when a provider failed or is not loaded.

        :param item_id: The library item ID of the artist.
        :param provider_filter: Optional provider instance ID to limit the result to.
        :param limit: Maximum number of similar artists to return.
        """
        ref_item = await self.get_library_item(item_id)
        listings, incomplete = await self._get_library_artist_listings(
            ref_item,
            ProviderFeature.SIMILAR_ARTISTS,
            self._ensure_provider_filter(provider_filter),
            # streaming provider results are resolved to library items
            lambda mapping: self.get_provider_artist_similar_artists(
                mapping.item_id, mapping.provider_instance, limit=limit
            ),
            lambda prov: prov.get_similar_artists(ref_item, limit=limit),
        )
        # interleave the providers' results by position (zip), deduplicating with the compare
        # helper, and cap to the requested limit
        result: list[Artist] = []
        for row in zip_longest(*listings):
            for candidate in row:
                if candidate is None or any(
                    compare_artist(existing, candidate) for existing in result
                ):
                    continue
                result.append(candidate)
        return Listing(result[:limit], complete=not incomplete)

    async def get_library_artist_types(self) -> list[ArtistType]:
        """Get all supported in-library artist types."""
        artist_types: list[ArtistType] = []
        query = f"SELECT DISTINCT artist_type FROM {DB_TABLE_ARTISTS}"
        rows = await self.mass.music.database.get_rows_from_query(query)
        for row in rows:
            artist_types.append(ArtistType(row["artist_type"]))
        return artist_types

    async def remove_item_from_library(self, item_id: str | int, recursive: bool = True) -> None:
        """Delete record from the database."""
        db_id = int(item_id)  # ensure integer
        library_item = await self.get_library_item(db_id)

        if library_item.artist_type == ArtistType.SINGER:
            await self._remove_music_artist_from_library(db_id=db_id, recursive=recursive)
        elif library_item.artist_type in (ArtistType.AUTHOR, ArtistType.NARRATOR):
            await self._remove_author_narrator_from_library(db_id=db_id, recursive=recursive)
        else:
            raise MusicAssistantError(f"Unknown artist_type {library_item.artist_type}.")

        # delete the artist itself from db
        # this will raise if the item still has references and recursive is false
        await super().remove_item_from_library(db_id)

    async def match_provider(
        self, db_artist: Artist, provider: MusicProvider, strict: bool = True
    ) -> list[ProviderMapping]:
        """
        Try to find match on (streaming) provider for the provided (database) artist.

        This is used to link objects of different providers/qualities together.

        :param strict: How strictly the candidate artist itself must match; the reference
            track/album only ever has to corroborate it, never match exactly.
        """
        self.logger.debug("Trying to match artist %s on provider %s", db_artist.name, provider.name)
        # try to get a match with some reference tracks of this artist
        ref_tracks = await self.mass.music.artists.tracks(db_artist.item_id, db_artist.provider)
        if len(ref_tracks) < 10:
            # fetch reference tracks from provider(s) attached to the artist
            for provider_mapping in db_artist.provider_mappings:
                with contextlib.suppress(ProviderUnavailableError, MediaNotFoundError):
                    ref_tracks += await self.mass.music.artists.tracks(
                        provider_mapping.item_id, provider_mapping.provider_instance
                    )
        for ref_track in ref_tracks:
            search_str = f"{db_artist.name} - {ref_track.name}"
            search_results = await self.mass.music.tracks.search(search_str, provider.domain)
            for search_result_item in search_results:
                # the reference track must corroborate the candidate, not merely share its title
                if not compare_track(ref_track, search_result_item, strict=False):
                    continue
                # get matching artist from track
                for search_item_artist in search_result_item.artists:
                    if matches := await self._confirm_artist_match(
                        db_artist, search_item_artist, strict
                    ):
                        return matches
        # try to get a match with some reference albums of this artist
        ref_albums = await self.mass.music.artists.albums(db_artist.item_id, db_artist.provider)
        if len(ref_albums) < 10:
            # fetch reference albums from provider(s) attached to the artist
            for provider_mapping in db_artist.provider_mappings:
                with contextlib.suppress(ProviderUnavailableError, MediaNotFoundError):
                    ref_albums += await self.mass.music.artists.albums(
                        provider_mapping.item_id, provider_mapping.provider_instance
                    )
        for ref_album in ref_albums:
            if ref_album.album_type == AlbumType.COMPILATION:
                continue
            if not ref_album.artists:
                continue
            search_str = f"{db_artist.name} - {ref_album.name}"
            search_result_albums = await self.mass.music.albums.search(search_str, provider.domain)
            for search_result_album in search_result_albums:
                # only the album's identity matters here: a different edition is still the
                # same record by the same artist, so the credits below decide the match
                if not compare_album_name(search_result_album.name, ref_album.name):
                    continue
                for search_album_artist in search_result_album.artists:
                    if matches := await self._confirm_artist_match(
                        db_artist, search_album_artist, strict
                    ):
                        return matches
        self.logger.debug(
            "Could not find match for Artist %s on provider %s",
            db_artist.name,
            provider.name,
        )
        return []

    async def match_providers(self, db_artist: Artist) -> None:
        """
        Try to find matching artists on all providers for the provided (database) item_id.

        This is used to link objects of different providers together.
        """
        if db_artist.provider != "library":
            return  # Matching only supported for database items

        # try to find match on all providers

        cur_provider_domains = {
            x.provider_domain for x in db_artist.provider_mappings if x.available
        }
        # the links MusicBrainz keeps name the artist on the other providers outright, so
        # those providers are linked first and only the remaining ones are searched
        if musicbrainz := self._musicbrainz_link_provider():
            cur_provider_domains |= await self._link_musicbrainz_entity(
                db_artist, lambda: musicbrainz.resolve_artist(db_artist, [], [])
            )
        for provider in self.mass.music.providers:
            if provider.domain in cur_provider_domains:
                continue
            if ProviderFeature.SEARCH not in provider.supported_features:
                continue
            if MediaType.ARTIST not in provider.supported_media_types:
                continue
            if not provider.is_streaming_provider:
                # matching on unique providers is pointless as they push (all) their content to MA
                continue
            if match := await self.match_provider(db_artist, provider):
                # 100% match, we update the db with the additional provider mapping(s)
                await self.add_provider_mappings(db_artist.item_id, match)
                cur_provider_domains.add(provider.domain)

    async def get_merged_library_item(self, artist: Artist) -> Artist:
        """
        Return the library artist the given one was merged into, else the given artist.

        :param artist: A library artist.
        """
        if not artist.provider_mappings:
            return artist
        # a merge moves the provider mappings to the surviving artist
        mapping = next(iter(artist.provider_mappings))
        return (
            await self.get_library_item_by_prov_id(mapping.item_id, mapping.provider_instance)
            or artist
        )

    async def merge_library_artists_by_mbid(self, artist_id: int, mbid: str) -> int:
        """
        Merge the library artists carrying the given MusicBrainz artist id into the oldest.

        Only artists of the same type merge, so an author and a singer are left apart.

        :param artist_id: Library id of an artist carrying the MusicBrainz id.
        :param mbid: The MusicBrainz artist id.
        :return: The library id the given artist is found under after the merge.
        """
        artist_types = {
            int(item.item_id): item.artist_type
            for item in await self.get_library_items_by_external_id(
                mbid, ExternalID.MB_ARTIST, limit=None
            )
        }
        artist_type = artist_types.get(artist_id)
        # the oldest library artist survives and keeps its name
        target_id, *source_ids = sorted(
            {artist_id, *(item_id for item_id, t in artist_types.items() if t == artist_type)}
        )
        for source_id in source_ids:
            with contextlib.suppress(MediaNotFoundError):  # already merged by a concurrent add
                await self.merge_library_items(target_id, source_id)
        return target_id

    def artist_from_item_mapping(self, item: ItemMapping) -> Artist:
        """Create an Artist object from an ItemMapping object."""
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
        return Artist.from_dict({**item.to_dict(), "provider_mappings": provider_mappings})

    def _validate_provider_filter(
        self, provider_instance_id_or_domain: str, provider_filter: str | None
    ) -> None:
        """Raise when a provider filter is set that does not match the requested provider."""
        if provider_filter is not None and provider_filter != provider_instance_id_or_domain:
            raise MusicAssistantError(
                f"provider_filter '{provider_filter}' does not match the requested "
                f"provider '{provider_instance_id_or_domain}'"
            )

    def _provider_mappings_for_feature(
        self, ref_item: Artist, feature: ProviderFeature, allowed: list[str] | None
    ) -> tuple[list[ProviderMapping], bool]:
        """
        Return the artist's provider mappings that can be queried for the given feature.

        A streaming provider is returned once, through one of its instances.

        :returns: The mappings, and whether a mapped source is not loaded, so cannot answer.
        """
        mappings: list[ProviderMapping] = []
        unanswered = False
        queried_streaming_domains: set[str] = set()
        # sorted so the same (preferably available) instance is picked each time
        for provider_mapping in sorted(
            ref_item.provider_mappings,
            key=lambda mapping: (not mapping.available, mapping.provider_instance),
        ):
            if allowed is not None and provider_mapping.provider_instance not in allowed:
                continue
            music_prov = self.mass.get_provider(
                provider_mapping.provider_instance, provider_type=MusicProvider
            )
            if music_prov is None:
                unanswered = True
                continue
            if feature not in music_prov.supported_features:
                continue
            # instances of the same streaming provider return the same catalog result
            if music_prov.is_streaming_provider:
                if music_prov.domain in queried_streaming_domains:
                    continue
                queried_streaming_domains.add(music_prov.domain)
            mappings.append(provider_mapping)
        return mappings, unanswered

    async def _get_library_artist_listings[T](
        self,
        ref_item: Artist,
        feature: ProviderFeature,
        allowed: list[str] | None,
        music_fetch: Callable[[ProviderMapping], Awaitable[list[T]]],
        discovery_fetch: Callable[[MusicDiscoveryMixin], Awaitable[list[T]]],
    ) -> tuple[list[list[T]], bool]:
        """
        Return the per-provider listings of a feature for an in-library artist.

        The music providers attached to the artist are queried in parallel. Metadata and plugin
        providers implementing the feature are queried only when those all answered and
        returned nothing. Providers that fail are logged and left out.

        :param ref_item: The in-library artist.
        :param feature: The feature the listing is for.
        :param allowed: Optional provider instance IDs to limit the providers to.
        :param music_fetch: Fetches the listing for one of the artist's provider mappings.
        :param discovery_fetch: Fetches the listing from a metadata or plugin provider.
        :returns: The listings of the providers that answered, and whether they are incomplete:
            a provider failed, or a mapped source is not loaded.
        """
        mappings, unanswered = self._provider_mappings_for_feature(ref_item, feature, allowed)
        listings, failed = await self._gather_provider_listings(
            ref_item, feature, [music_fetch(mapping) for mapping in mappings]
        )
        if any(listings) or failed:
            # a music provider that failed (e.g. rate limited) must not turn into more
            # requests against the same services through a metadata provider
            return listings, failed or unanswered
        # metadata providers such as Last.fm resolve every item through music-service searches,
        # so they are a fallback rather than a parallel source
        listings, failed = await self._gather_provider_listings(
            ref_item,
            feature,
            [
                discovery_fetch(cast("MusicDiscoveryMixin", prov))
                for prov in self.mass.get_providers_supporting_feature(
                    feature, priority=(ProviderType.METADATA, ProviderType.PLUGIN)
                )
                if allowed is None or prov.instance_id in allowed
            ],
        )
        return listings, failed or unanswered

    async def _gather_provider_listings[T](
        self, ref_item: Artist, feature: ProviderFeature, fetches: list[Awaitable[list[T]]]
    ) -> tuple[list[list[T]], bool]:
        """
        Await the provider listings in parallel, leaving out (and logging) failed ones.

        :param ref_item: The in-library artist.
        :param feature: The feature the listings are for.
        :param fetches: The pending listing of every provider to query.
        :returns: The listings of the providers that answered, and whether any provider failed.
        """
        per_provider = await asyncio.gather(*fetches, return_exceptions=True)
        # drop (and log) any provider that failed so one bad provider can't sink the listing
        listings: list[list[T]] = []
        failed = False
        for listing in per_provider:
            if isinstance(listing, BaseException):
                failed = True
                self.logger.warning(
                    "Error fetching %s for artist %s from a provider",
                    feature,
                    ref_item.name,
                    exc_info=listing,
                )
                continue
            listings.append(listing)
        return listings, failed

    async def _confirm_artist_match(
        self, db_artist: Artist, candidate: Artist | ItemMapping, strict: bool
    ) -> list[ProviderMapping]:
        """
        Return the provider mappings of a candidate artist that confirms as the given artist.

        :param candidate: The artist as credited on a search result, which may be a simplified
            object without external ids.
        """
        if not compare_artist(db_artist, candidate, strict=strict):
            return []
        # only the full artist carries the external ids and artist type that can still reject
        # the candidate, so a credit the provider cannot resolve confirms nothing; a credit
        # that resolves to a library item is already owned by another artist
        with contextlib.suppress(MediaNotFoundError):
            prov_artist = await self.get_provider_item(candidate.item_id, candidate.provider)
            if prov_artist.provider != "library" and compare_artist(
                db_artist, prov_artist, strict=strict
            ):
                return list(prov_artist.provider_mappings)
        return []

    async def _add_library_item(
        self, item: Artist | ItemMapping, overwrite_existing: bool = False
    ) -> int:
        """Add a new item record to the database."""
        # If item is an ItemMapping, convert it
        if isinstance(item, ItemMapping):
            item = self.artist_from_item_mapping(item)
        # enforce various artists name + id
        if compare_strings(item.name, VARIOUS_ARTISTS_NAME):
            item.mbid = VARIOUS_ARTISTS_MBID
        if item.mbid == VARIOUS_ARTISTS_MBID:
            item.name = VARIOUS_ARTISTS_NAME
        # no existing item matched: insert item
        db_id = await self.mass.music.database.insert(
            self.db_table,
            {
                "name": item.name,
                "sort_name": item.sort_name,
                "metadata": serialize_to_json(item.metadata),
                "search_name": create_safe_string(item.name, True, True),
                "search_sort_name": create_safe_string(item.sort_name or "", True, True),
                "timestamp_added": int(item.date_added.timestamp()) if item.date_added else UNSET,
                "artist_type": item.artist_type,
            },
        )
        # update/set external id lookup table
        await self.set_external_ids(db_id, item.external_ids)
        # update/set provider_mappings table
        await self.set_provider_mappings(db_id, item.provider_mappings)
        self.logger.debug("added %s to database (id: %s)", item.name, db_id)
        return db_id

    async def _update_library_item(
        self, item_id: str | int, update: Artist | ItemMapping, overwrite: bool = False
    ) -> None:
        """Update existing record in the database."""
        db_id = int(item_id)  # ensure integer
        cur_item = await self.get_library_item(db_id)
        if isinstance(update, ItemMapping):
            # NOTE that artist is the only mediatype where its accepted we
            # receive an itemmapping from streaming providers
            update = self.artist_from_item_mapping(update)
            metadata = cur_item.metadata
        else:
            metadata = metadata_for_update(cur_item.metadata, update.metadata, overwrite)
        cur_item.external_ids.update(update.external_ids)
        # enforce various artists name + id
        mbid = cur_item.mbid
        if (not mbid or overwrite) and getattr(update, "mbid", None):
            if compare_strings(update.name, VARIOUS_ARTISTS_NAME):
                update.mbid = VARIOUS_ARTISTS_MBID
            if update.mbid == VARIOUS_ARTISTS_MBID:
                update.name = VARIOUS_ARTISTS_NAME

        name = update.name if overwrite else cur_item.name
        sort_name = update.sort_name if overwrite else cur_item.sort_name or update.sort_name
        await self.mass.music.database.update(
            self.db_table,
            {"item_id": db_id},
            {
                "name": name,
                "sort_name": sort_name,
                "metadata": serialize_to_json(metadata),
                "search_name": create_safe_string(name, True, True),
                "search_sort_name": create_safe_string(sort_name or "", True, True),
                "timestamp_added": int(update.date_added.timestamp())
                if update.date_added
                else UNSET,
                "artist_type": update.artist_type,
            },
        )
        self.logger.debug("updated %s in database: %s", update.name, db_id)
        # update/set external id lookup table
        await self.set_external_ids(
            db_id, update.external_ids if overwrite else cur_item.external_ids
        )
        # update/set provider_mappings table
        provider_mappings = provider_mappings_for_update(
            cur_item.provider_mappings, update.provider_mappings, overwrite
        )
        await self.set_provider_mappings(db_id, provider_mappings, overwrite)
        self.logger.debug("updated %s in database: (id %s)", update.name, db_id)

    async def _validate_library_item_merge(self, target: Artist, source: Artist) -> None:
        """Validate that two artists have the same role."""
        await super()._validate_library_item_merge(target, source)
        if target.artist_type != source.artist_type:
            msg = (
                f"Cannot merge artist '{source.name}' into '{target.name}': "
                "artists must have the same role."
            )
            raise InvalidDataError(msg)

    async def _remove_music_artist_from_library(self, db_id: int, recursive: bool) -> None:
        # recursively also remove artist albums
        for db_row in await self.mass.music.database.get_rows_from_query(
            f"SELECT album_id FROM {DB_TABLE_ALBUM_ARTISTS} WHERE artist_id = :artist_id",
            {"artist_id": db_id},
            limit=5000,
        ):
            if not recursive:
                raise MusicAssistantError("Artist still has albums linked")
            with contextlib.suppress(MediaNotFoundError):
                await self.mass.music.albums.remove_item_from_library(db_row["album_id"])
        # recursively also remove artist tracks
        for db_row in await self.mass.music.database.get_rows_from_query(
            f"SELECT track_id FROM {DB_TABLE_TRACK_ARTISTS} WHERE artist_id = :artist_id",
            {"artist_id": db_id},
            limit=5000,
        ):
            if not recursive:
                raise MusicAssistantError("Artist still has tracks linked")
            with contextlib.suppress(MediaNotFoundError):
                await self.mass.music.tracks.remove_item_from_library(db_row["track_id"])

    async def _remove_author_narrator_from_library(self, db_id: int, recursive: bool) -> None:
        # recursively also remove author/ narrator audiobooks
        for db_row in await self.mass.music.database.get_rows_from_query(
            f"SELECT audiobook_id FROM {DB_TABLE_AUDIOBOOK_ARTISTS} WHERE artist_id = :artist_id",
            {"artist_id": db_id},
            limit=5000,
        ):
            if not recursive:
                raise MusicAssistantError("Artist still has audiobooks linked")
            with contextlib.suppress(MediaNotFoundError):
                await self.mass.music.audiobooks.remove_item_from_library(db_row["audiobook_id"])

    async def _get_db_author_narrator_audiobooks(
        self, item_id: str, provider_instance_id_or_domain: str, artist_type: ArtistType
    ) -> list[Audiobook]:
        if db_author_narrator := await self.mass.music.artists.get_library_item_by_prov_id(
            item_id,
            provider_instance_id_or_domain,
        ):
            if db_author_narrator.artist_type != artist_type:
                self.logger.debug("Artist type must be %s.", artist_type)
                return []
            db_artist_id = int(db_author_narrator.item_id)  # ensure integer
            subquery = f"SELECT audiobook_id FROM {DB_TABLE_AUDIOBOOK_ARTISTS} WHERE artist_id = :artist_id"
            query = f"audiobooks.item_id in ({subquery})"
            return await self.mass.music.audiobooks.get_library_items_by_query(
                limit=0,
                extra_query_parts=[query],
                extra_query_params={"artist_id": db_artist_id},
                provider_filter=[provider_instance_id_or_domain],
            )
        return []

    def _parse_summary_row(
        self, db_row: Mapping[str, Any], hidden_sources: set[str]
    ) -> ArtistSummary:
        """Parse a raw summary db row into an ArtistSummary object."""
        item = cast("ArtistSummary", super()._parse_summary_row(db_row, hidden_sources))
        item.artist_type = ArtistType(db_row["artist_type"])
        return item

    async def _get_artist_discography_data(
        self,
        item_id: str,
        provider_instance_id_or_domain: str,
        *,
        include_library_albums: bool,
    ) -> _DiscographyData | None:
        if provider_instance_id_or_domain != "library":
            msg = "A discography is only available for library artists"
            raise InvalidDataError(msg)
        artist = await self.get_library_item(item_id)
        musicbrainz = cast("MusicbrainzProvider | None", self.mass.get_provider("musicbrainz"))
        if musicbrainz is None:
            return None
        library_albums = (
            await self.albums(item_id, "library", limit=None)
            if include_library_albums or not artist.mbid
            else []
        )
        mbid = artist.mbid
        if not mbid:
            # An unidentified artist is resolved against its library albums without storing it.
            mb_artist = await musicbrainz.resolve_artist(artist, library_albums, [])
            mbid = mb_artist.id if mb_artist else None
        if not mbid or mbid == VARIOUS_ARTISTS_MBID:
            return None
        release_groups = await musicbrainz.browse_release_groups_by_artist(mbid)
        if not release_groups:
            return None
        return _DiscographyData(artist, musicbrainz, library_albums, release_groups)

    async def _get_library_artist_appears_on_query(
        self,
        item_id: str | int,
        provider_filter: str | None,
    ) -> tuple[list[str], dict[str, Any]] | None:
        db_id = int(item_id)
        library_item = await self.get_library_item(db_id)
        if library_item.artist_type != ArtistType.SINGER:
            self.logger.debug("Albums only available for artists of type ARTIST")
            return None
        query_params: dict[str, Any] = {"artist_id": db_id}
        track_mapping_conditions = [
            f"{DB_TABLE_PROVIDER_MAPPINGS}.item_id = {DB_TABLE_ALBUM_TRACKS}.track_id",
            f"{DB_TABLE_PROVIDER_MAPPINGS}.media_type = '{MediaType.TRACK.value}'",
            f"{DB_TABLE_PROVIDER_MAPPINGS}.in_library = 1",
        ]
        if provider_instances := self._ensure_provider_filter(provider_filter):
            track_mapping_conditions.append(
                f"{DB_TABLE_PROVIDER_MAPPINGS}.provider_instance IN :provider_instances"
            )
            query_params["provider_instances"] = provider_instances
        track_albums = (
            f"SELECT {DB_TABLE_ALBUM_TRACKS}.album_id FROM {DB_TABLE_ALBUM_TRACKS} "
            f"JOIN {DB_TABLE_TRACK_ARTISTS} "
            f"ON {DB_TABLE_TRACK_ARTISTS}.track_id = {DB_TABLE_ALBUM_TRACKS}.track_id "
            f"WHERE {DB_TABLE_TRACK_ARTISTS}.artist_id = :artist_id "
            f"AND EXISTS(SELECT 1 FROM {DB_TABLE_PROVIDER_MAPPINGS} "
            f"WHERE {' AND '.join(track_mapping_conditions)})"
        )
        own_albums = f"SELECT album_id FROM {DB_TABLE_ALBUM_ARTISTS} WHERE artist_id = :artist_id"
        return [
            f"albums.item_id IN ({track_albums})",
            f"albums.item_id NOT IN ({own_albums})",
        ], query_params

    def _album_artist_clause(self, query_params: dict[str, Any]) -> str:
        """Return the condition that keeps artists credited on an album in the user's library."""
        # an album only stored because one of its tracks is in the library does not count,
        # nor does an album on a music source the current user may not see
        mapping_conditions = [
            f"{DB_TABLE_PROVIDER_MAPPINGS}.item_id = {DB_TABLE_ALBUM_ARTISTS}.album_id",
            f"{DB_TABLE_PROVIDER_MAPPINGS}.media_type = '{MediaType.ALBUM.value}'",
            f"{DB_TABLE_PROVIDER_MAPPINGS}.in_library = 1",
        ]
        if provider_instances := self._ensure_provider_filter(None):
            mapping_conditions.append(
                f"{DB_TABLE_PROVIDER_MAPPINGS}.provider_instance IN :album_artist_providers"
            )
            query_params["album_artist_providers"] = provider_instances
        return (
            f"EXISTS(SELECT 1 FROM {DB_TABLE_ALBUM_ARTISTS} "
            f"JOIN {DB_TABLE_PROVIDER_MAPPINGS} ON {' AND '.join(mapping_conditions)} "
            f"WHERE {DB_TABLE_ALBUM_ARTISTS}.artist_id = {DB_TABLE_ARTISTS}.item_id)"
        )


_SHORT_FORM_TYPES = {AlbumType.SINGLE, AlbumType.EP}


class _DiscographyData(NamedTuple):
    """The pieces a library artist's discography is assembled from."""

    artist: Artist
    musicbrainz: MusicbrainzProvider
    library_albums: list[Album]
    release_groups: list[MusicBrainzReleaseGroup]


def _release_group_of(
    album: Album,
    release_groups: Sequence[MusicBrainzReleaseGroup],
    claimed: Container[str],
    album_type_of: Callable[[MusicBrainzReleaseGroup], AlbumType],
) -> MusicBrainzReleaseGroup | None:
    """
    Return the one release group a library album not identified on MusicBrainz is.

    Same-titled groups are told apart by the album's type and year; a group that remains
    ambiguous is none of them.

    :param album: The library album, without a release group id.
    :param release_groups: The release groups of the album's artist.
    :param claimed: The ids of the release groups other library albums are.
    :param album_type_of: Maps a release group to the album type it describes.
    """
    candidates = [
        group
        for group in release_groups
        if group.id not in claimed and compare_album_name(album.name, group.title)
    ]
    if len(candidates) > 1:
        # the type tells a same-titled single or EP from the album, and no more: the music
        # services type a live album "album" and an EP "single" or even "album"
        candidates = [
            group
            for group in candidates
            if _album_types_agree(album.album_type, album_type_of(group))
        ] or candidates
    if len(candidates) > 1 and album.year:
        # same-titled groups of one kind, self-titled albums mostly, are told apart by year
        candidates = [group for group in candidates if group.first_release_year == album.year]
    return candidates[0] if len(candidates) == 1 else None


def _album_types_agree(library_type: AlbumType, group_type: AlbumType) -> bool:
    """Return whether two album types can describe one release; a single or EP is never an album."""
    if AlbumType.UNKNOWN in (library_type, group_type):
        return True
    return (library_type in _SHORT_FORM_TYPES) == (group_type in _SHORT_FORM_TYPES)


def _album_from_release_group(
    release_group: MusicBrainzReleaseGroup,
    artist: ItemMapping,
    album_type: AlbumType,
    cover_provider: str | None,
) -> Album:
    """
    Return a MusicBrainz release group as an album that is not (yet) on any music provider.

    :param release_group: The release group as the artist browse lists it.
    :param artist: The library artist the album is credited to.
    :param album_type: The album type the release group's types map to.
    :param cover_provider: The metadata provider resolving the group's cover by its id once
        the image is requested, if one is loaded.
    """
    album = Album(
        item_id=release_group.id,
        provider="musicbrainz",
        name=release_group.title,
        artists=UniqueList([artist]),
        year=release_group.first_release_year,
        album_type=album_type,
        external_ids={(ExternalID.MB_RELEASEGROUP, release_group.id)},
        provider_mappings=set(),
        # resolves to a playable album on a music service only when opened or added
        is_playable=False,
    )
    if cover_provider:
        album.metadata.add_image(
            MediaItemImage(
                type=ImageType.THUMB,
                path=release_group.id,
                provider=cover_provider,
                remotely_accessible=False,
            )
        )
    return album
