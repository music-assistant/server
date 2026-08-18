"""Manage MediaItems of type Artist."""

from __future__ import annotations

import asyncio
import contextlib
from itertools import zip_longest
from typing import TYPE_CHECKING, Any, Literal, cast, overload

from music_assistant_models.auth import Scope
from music_assistant_models.enums import (
    AlbumType,
    ArtistType,
    MediaType,
    ProviderFeature,
    ProviderType,
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
    ProviderMapping,
    Track,
)

from music_assistant.constants import (
    DB_TABLE_ALBUM_ARTISTS,
    DB_TABLE_ARTISTS,
    DB_TABLE_AUDIOBOOK_ARTISTS,
    DB_TABLE_TRACK_ARTISTS,
    VARIOUS_ARTISTS_MBID,
    VARIOUS_ARTISTS_NAME,
)
from music_assistant.helpers.compare import (
    compare_album,
    compare_album_name,
    compare_artist,
    compare_strings,
    compare_track,
)
from music_assistant.helpers.database import UNSET
from music_assistant.helpers.json import serialize_to_json
from music_assistant.models.music_provider import MusicProvider

from .base import MediaControllerBase

if TYPE_CHECKING:
    from collections.abc import Mapping

    from music_assistant import MusicAssistant
    from music_assistant.models.metadata_provider import MetadataProvider


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

        :param favorite_only: Only count artists marked as favorite.
        :param album_artists_only: Only count artists that have albums.
        :param artist_type: Only count artists of this type.
        """
        sql_query = f"SELECT item_id FROM {self.db_table}"
        query_parts = []
        query_params: dict[str, Any] = {}
        if artist_type:
            query_parts.append(f"artist_type = '{artist_type}'")
        if favorite_only:
            query_parts.append("favorite = 1")
        if album_artists_only:
            query_parts.append(
                f"item_id in (select {DB_TABLE_ALBUM_ARTISTS}.artist_id "
                f"FROM {DB_TABLE_ALBUM_ARTISTS})"
            )
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
        order_by: str = "sort_name",
        provider: str | list[str] | None = None,
        genre: int | list[int] | None = None,
        played_only: bool = False,
        album_artists_only: bool = False,
        artist_type: ArtistType | None = None,
        *,
        summary: bool = True,
        reachable_via: list[str] | None = None,
        **kwargs: Any,
    ) -> list[Artist]:
        """
        Get in-database (album) artists.

        :param favorite: Filter by favorite status.
        :param search: Filter by search query.
        :param limit: Maximum number of items to return.
        :param offset: Number of items to skip.
        :param order_by: Order by field (e.g. 'sort_name', 'timestamp_added').
        :param provider: Filter by provider instance ID (single string or list).
        :param album_artists_only: Only return artists that have albums.
        :param genre: Filter by genre id(s).
        :param artist_type: The artist's type
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
        if artist_type:
            extra_query_parts = [f"artist_type = '{artist_type}'"]
        if album_artists_only and artist_type in (None, ArtistType.SINGER):
            extra_query_parts.append(
                f"artists.item_id in (select {DB_TABLE_ALBUM_ARTISTS}.artist_id "
                f"from {DB_TABLE_ALBUM_ARTISTS})"
            )
        return await self.get_library_items_by_query(
            favorite=favorite,
            search=search,
            genre_ids=genre,
            limit=limit,
            offset=offset,
            order_by=order_by,
            provider_filter=self._provider_filter_considering_reachability(provider, reachable_via),
            extra_query_parts=extra_query_parts,
            extra_query_params=extra_query_params,
            played_only=played_only,
            in_library_only=True,
            summary=summary,
            reachable_via=reachable_via,
        )

    async def tracks(
        self,
        item_id: str,
        provider_instance_id_or_domain: str,
        provider_filter: str | None = None,
    ) -> list[Track]:
        """
        Return the tracks for a artist.

        For a library item, the in-library tracks are returned, optionally limited to a single
        provider instance with the provider_filter. For a provider item, that provider's
        tracks listing is returned (which may be empty if it is not supported).

        :param item_id: The item ID of the artist.
        :param provider_instance_id_or_domain: The provider instance ID or domain of the artist.
        :param provider_filter: Optional provider instance ID to limit the (library) result to.
        """
        if provider_instance_id_or_domain == "library":
            return await self.get_library_artist_tracks(item_id, provider_filter=provider_filter)
        self._validate_provider_filter(provider_instance_id_or_domain, provider_filter)
        return await self.get_provider_artist_tracks(item_id, provider_instance_id_or_domain)

    async def albums(
        self,
        item_id: str,
        provider_instance_id_or_domain: str,
        provider_filter: str | None = None,
    ) -> list[Album]:
        """
        Return the albums for an artist.

        For a library item, the in-library albums are returned, optionally limited to a single
        provider instance with the provider_filter. For a provider item, that provider's
        albums listing is returned (which may be empty if it is not supported).

        :param item_id: The item ID of the artist.
        :param provider_instance_id_or_domain: The provider instance ID or domain of the artist.
        :param provider_filter: Optional provider instance ID to limit the (library) result to.
        """
        if provider_instance_id_or_domain == "library":
            return await self.get_library_artist_albums(item_id, provider_filter=provider_filter)
        self._validate_provider_filter(provider_instance_id_or_domain, provider_filter)
        return await self.get_provider_artist_albums(item_id, provider_instance_id_or_domain)

    async def top_tracks(
        self,
        item_id: str,
        provider_instance_id_or_domain: str,
        provider_filter: str | None = None,
    ) -> list[Track]:
        """
        Return the top/featured tracks for an artist.

        For a library item, the top tracks of all the artist's providers are aggregated (and
        deduplicated), optionally limited to a single provider instance. For a provider
        item, that provider's top tracks listing is returned (may be empty if not supported).

        :param item_id: The item ID of the artist.
        :param provider_instance_id_or_domain: The provider instance ID or domain of the artist.
        :param provider_filter: Optional provider instance ID to limit the result to.
        """
        if provider_instance_id_or_domain == "library":
            return await self.get_library_artist_toptracks(item_id, provider_filter=provider_filter)
        self._validate_provider_filter(provider_instance_id_or_domain, provider_filter)
        return await self.get_provider_artist_toptracks(item_id, provider_instance_id_or_domain)

    async def top_albums(
        self,
        item_id: str,
        provider_instance_id_or_domain: str,
        provider_filter: str | None = None,
    ) -> list[Album]:
        """
        Return the top/featured albums for an artist.

        For a library item, the top albums of all the artist's providers are aggregated (and
        deduplicated), optionally limited to a single provider instance. For a provider
        item, that provider's top albums listing is returned (may be empty if not supported).

        :param item_id: The item ID of the artist.
        :param provider_instance_id_or_domain: The provider instance ID or domain of the artist.
        :param provider_filter: Optional provider instance ID to limit the result to.
        """
        if provider_instance_id_or_domain == "library":
            return await self.get_library_artist_topalbums(item_id, provider_filter=provider_filter)
        self._validate_provider_filter(provider_instance_id_or_domain, provider_filter)
        return await self.get_provider_artist_topalbums(item_id, provider_instance_id_or_domain)

    async def similar_artists(
        self,
        item_id: str,
        provider_instance_id_or_domain: str,
        provider_filter: str | None = None,
        limit: int = 25,
    ) -> list[Artist]:
        """
        Return similar artists for an artist.

        For a library item, the similar artists of all the artist's providers are aggregated
        (and deduplicated), optionally limited to a single provider instance. For a provider
        item, that provider's similar artists listing is returned (may be empty if not
        supported).

        :param item_id: The item ID of the artist.
        :param provider_instance_id_or_domain: The provider instance ID or domain of the artist.
        :param provider_filter: Optional provider instance ID to limit the result to.
        :param limit: Maximum number of similar artists to return.
        """
        if provider_instance_id_or_domain == "library":
            return await self.get_library_artist_similar_artists(
                item_id, provider_filter=provider_filter, limit=limit
            )
        self._validate_provider_filter(provider_instance_id_or_domain, provider_filter)
        return await self.get_provider_artist_similar_artists(
            item_id, provider_instance_id_or_domain, limit=limit
        )

    if TYPE_CHECKING:

        @overload
        async def audiobooks(
            self,
            item_id: str,
            provider_instance_id_or_domain: str,
            artist_type: ArtistType = ArtistType.AUTHOR,
            in_library_only: bool = False,
            *,
            collapse_collections: Literal[False] = False,
        ) -> list[Audiobook]: ...

        @overload
        async def audiobooks(
            self,
            item_id: str,
            provider_instance_id_or_domain: str,
            artist_type: ArtistType = ArtistType.AUTHOR,
            in_library_only: bool = False,
            *,
            collapse_collections: Literal[True],
        ) -> list[Audiobook | MediaCollection[Audiobook]]: ...

    async def audiobooks(
        self,
        item_id: str,
        provider_instance_id_or_domain: str,
        artist_type: ArtistType = ArtistType.AUTHOR,
        in_library_only: bool = False,
        *,
        collapse_collections: bool = False,
    ) -> list[Audiobook] | list[Audiobook | MediaCollection[Audiobook]]:
        """
        Return audiobooks for an artist.

        Artist_type can be omitted for in-library artists.

        :param collapse_collections: Collapse available collections. Only applies to
            in-library items; when in_library_only is False, provider items are
            appended as plain audiobooks alongside the collapsed collections.
        """
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
            if artist_type == ArtistType.AUTHOR:
                return await self.get_provider_author_audiobooks(
                    item_id, provider_instance_id_or_domain
                )
            if artist_type == ArtistType.NARRATOR:
                return await self.get_provider_narrator_audiobooks(
                    item_id, provider_instance_id_or_domain
                )
            return []

        db_items = await self.get_library_author_narrator_audiobooks(
            library_artist.item_id,
            artist_type=library_artist.artist_type,
            collapse_collections=collapse_collections,
        )
        result: list[Audiobook] | list[Audiobook | MediaCollection[Audiobook]] = db_items
        if in_library_only:
            # return in-library items only
            return result
        # return all (unique) items from all providers
        # initialize unique_ids with db_items to prevent duplicates
        unique_ids: set[str] = set()
        for item in db_items:
            if isinstance(item, MediaCollection):
                for collection_item in item.items:
                    unique_ids.add(f"{collection_item.name}.{collection_item.version}")
            else:
                unique_ids.add(f"{item.name}.{item.version}")
        unique_providers = self.mass.music.get_unique_providers()
        audiobook_method = (
            self.get_provider_author_audiobooks
            if artist_type == ArtistType.AUTHOR
            else self.get_provider_narrator_audiobooks
        )
        for provider_mapping in library_artist.provider_mappings:
            if provider_mapping.provider_instance not in unique_providers:
                continue
            provider_audiobooks = await audiobook_method(
                provider_mapping.item_id, provider_mapping.provider_instance
            )
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
        return result

    async def get_library_author_narrator_audiobooks(
        self,
        item_id: str | int,
        artist_type: ArtistType,
        *,
        collapse_collections: bool = False,
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
            extra_query_parts=[query],
            extra_query_params={"artist_id": db_id},
            collapse_collections=collapse_collections,
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
    ) -> list[Track]:
        """
        Return the top tracks for an in-library artist, aggregated across all its providers.

        The result combines (and deduplicates, preserving order) the top tracks from every
        provider attached to the artist and any metadata/plugin provider implementing the
        feature. Empty when no provider yields a result.

        :param item_id: The library item ID of the artist.
        :param provider_filter: Optional provider instance ID to limit the result to.
        """
        ref_item = await self.get_library_item(item_id)
        allowed = self._ensure_provider_filter(provider_filter)
        # fetch each provider's ranked top tracks in parallel
        fetches = []
        # streaming providers attached to the artist (results resolved to library items)
        for provider_mapping in ref_item.provider_mappings:
            if allowed is not None and provider_mapping.provider_instance not in allowed:
                continue
            music_prov = self.mass.get_provider(
                provider_mapping.provider_instance, provider_type=MusicProvider
            )
            if (
                music_prov is None
                or ProviderFeature.ARTIST_TOPTRACKS not in music_prov.supported_features
            ):
                continue
            fetches.append(
                self.get_provider_artist_toptracks(
                    provider_mapping.item_id, provider_mapping.provider_instance
                )
            )
        # metadata/plugin providers implementing the feature
        for prov in self.mass.get_providers_supporting_feature(
            ProviderFeature.ARTIST_TOPTRACKS,
            priority=(ProviderType.METADATA, ProviderType.PLUGIN),
        ):
            if allowed is not None and prov.instance_id not in allowed:
                continue
            fetches.append(cast("MetadataProvider", prov).get_artist_toptracks(ref_item))
        per_provider = await asyncio.gather(*fetches, return_exceptions=True)
        # drop (and log) any provider that failed so one bad provider can't sink the listing
        listings: list[list[Track]] = []
        for listing in per_provider:
            if isinstance(listing, BaseException):
                self.logger.warning(
                    "Error fetching top tracks for artist %s from a provider",
                    ref_item.name,
                    exc_info=listing,
                )
                continue
            listings.append(listing)
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
        return result

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
    ) -> list[Album]:
        """
        Return the top albums for an in-library artist, aggregated across all its providers.

        The result combines (and deduplicates, preserving order) the top albums from every
        provider attached to the artist and any metadata/plugin provider implementing the
        feature. Empty when no provider yields a result.

        :param item_id: The library item ID of the artist.
        :param provider_filter: Optional provider instance ID to limit the result to.
        """
        ref_item = await self.get_library_item(item_id)
        allowed = self._ensure_provider_filter(provider_filter)
        # fetch each provider's ranked top albums in parallel
        fetches = []
        # streaming providers attached to the artist (results resolved to library items)
        for provider_mapping in ref_item.provider_mappings:
            if allowed is not None and provider_mapping.provider_instance not in allowed:
                continue
            music_prov = self.mass.get_provider(
                provider_mapping.provider_instance, provider_type=MusicProvider
            )
            if (
                music_prov is None
                or ProviderFeature.ARTIST_TOPALBUMS not in music_prov.supported_features
            ):
                continue
            fetches.append(
                self.get_provider_artist_topalbums(
                    provider_mapping.item_id, provider_mapping.provider_instance
                )
            )
        # metadata/plugin providers implementing the feature
        for prov in self.mass.get_providers_supporting_feature(
            ProviderFeature.ARTIST_TOPALBUMS,
            priority=(ProviderType.METADATA, ProviderType.PLUGIN),
        ):
            if allowed is not None and prov.instance_id not in allowed:
                continue
            fetches.append(cast("MetadataProvider", prov).get_artist_topalbums(ref_item))
        per_provider = await asyncio.gather(*fetches, return_exceptions=True)
        # drop (and log) any provider that failed so one bad provider can't sink the listing
        listings: list[list[Album]] = []
        for listing in per_provider:
            if isinstance(listing, BaseException):
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
        return result

    async def get_provider_artist_tracks(
        self,
        item_id: str,
        provider_instance_id_or_domain: str,
    ) -> list[Track]:
        """Return all tracks for an artist on given provider."""
        provider = self.mass.get_provider(
            provider_instance_id_or_domain, provider_type=MusicProvider
        )
        if provider is None or not provider.available:
            return []  # guard against unavailable provider
        if provider.supports_feature(ProviderFeature.ARTIST_TRACKS):
            return await provider.get_artist_tracks(item_id)
        # fallback: enumerate (and dedupe) the tracks of all the artist's albums on the provider
        result: list[Track] = []
        unique_ids: set[str] = set()
        for album in await self.get_provider_artist_albums(item_id, provider_instance_id_or_domain):
            for track in await self.mass.music.albums.tracks(album.item_id, album.provider):
                unique_id = f"{track.name}.{track.version}"
                if unique_id in unique_ids:
                    continue
                unique_ids.add(unique_id)
                result.append(track)
        return result

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
            extra_query_parts=[query],
            extra_query_params={"artist_id": db_id},
            provider_filter=self._ensure_provider_filter(provider_filter),
            in_library_only=True,
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
    ) -> list[Artist]:
        """
        Return similar artists for an in-library artist, aggregated across all its providers.

        The result combines (and deduplicates, preserving order) the similar artists from
        every provider attached to the artist and any metadata/plugin provider implementing
        the feature. Empty when no provider yields a result.

        :param item_id: The library item ID of the artist.
        :param provider_filter: Optional provider instance ID to limit the result to.
        :param limit: Maximum number of similar artists to return.
        """
        ref_item = await self.get_library_item(item_id)
        allowed = self._ensure_provider_filter(provider_filter)
        # fetch each provider's similar artists in parallel
        fetches = []
        # streaming providers attached to the artist (results resolved to library items)
        for provider_mapping in ref_item.provider_mappings:
            if allowed is not None and provider_mapping.provider_instance not in allowed:
                continue
            music_prov = self.mass.get_provider(
                provider_mapping.provider_instance, provider_type=MusicProvider
            )
            if (
                music_prov is None
                or ProviderFeature.SIMILAR_ARTISTS not in music_prov.supported_features
            ):
                continue
            fetches.append(
                self.get_provider_artist_similar_artists(
                    provider_mapping.item_id, provider_mapping.provider_instance, limit=limit
                )
            )
        # metadata/plugin providers implementing the feature
        for prov in self.mass.get_providers_supporting_feature(
            ProviderFeature.SIMILAR_ARTISTS,
            priority=(ProviderType.METADATA, ProviderType.PLUGIN),
        ):
            if allowed is not None and prov.instance_id not in allowed:
                continue
            fetches.append(
                cast("MetadataProvider", prov).get_similar_artists(ref_item, limit=limit)
            )
        per_provider = await asyncio.gather(*fetches, return_exceptions=True)
        # drop (and log) any provider that failed so one bad provider can't sink the listing
        listings: list[list[Artist]] = []
        for listing in per_provider:
            if isinstance(listing, BaseException):
                self.logger.warning(
                    "Error fetching similar artists for %s from a provider",
                    ref_item.name,
                    exc_info=listing,
                )
                continue
            listings.append(listing)
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
        return result[:limit]

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
        for provider in self.mass.music.providers:
            if provider.domain in cur_provider_domains:
                continue
            if ProviderFeature.SEARCH not in provider.supported_features:
                continue
            if not self.mass.music.library_supported(provider, MediaType.ARTIST):
                continue
            if not provider.is_streaming_provider:
                # matching on unique providers is pointless as they push (all) their content to MA
                continue
            if match := await self.match_provider(db_artist, provider):
                # 100% match, we update the db with the additional provider mapping(s)
                await self.add_provider_mappings(db_artist.item_id, match)
                cur_provider_domains.add(provider.domain)

    def artist_from_item_mapping(self, item: ItemMapping) -> Artist:
        """Create an Artist object from an ItemMapping object."""
        domain, instance_id = None, None
        if prov := self.mass.get_provider(item.provider):
            domain = prov.domain
            instance_id = prov.instance_id
        return Artist.from_dict(
            {
                **item.to_dict(),
                "provider_mappings": [
                    {
                        "item_id": item.item_id,
                        "provider_domain": domain,
                        "provider_instance": instance_id,
                        "available": item.available,
                    }
                ],
            }
        )

    def _validate_provider_filter(
        self, provider_instance_id_or_domain: str, provider_filter: str | None
    ) -> None:
        """Raise when a provider filter is set that does not match the requested provider."""
        if provider_filter is not None and provider_filter != provider_instance_id_or_domain:
            raise MusicAssistantError(
                f"provider_filter '{provider_filter}' does not match the requested "
                f"provider '{provider_instance_id_or_domain}'"
            )

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
                "favorite": item.favorite,
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
            metadata = update.metadata if overwrite else cur_item.metadata.update(update.metadata)
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
        provider_mappings = (
            update.provider_mappings
            if overwrite
            else {*update.provider_mappings, *cur_item.provider_mappings}
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
                extra_query_parts=[query],
                extra_query_params={"artist_id": db_artist_id},
                provider_filter=[provider_instance_id_or_domain],
            )
        return []

    def _parse_summary_row(self, db_row: Mapping[str, Any]) -> ArtistSummary:
        """Parse a raw summary db row into an ArtistSummary object."""
        item = cast("ArtistSummary", super()._parse_summary_row(db_row))
        item.artist_type = ArtistType(db_row["artist_type"])
        return item
