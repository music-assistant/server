"""Manage MediaItems of type Album."""

from __future__ import annotations

import contextlib
from collections.abc import Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

import aiohttp
from music_assistant_models.auth import Scope
from music_assistant_models.enums import AlbumType, ExternalID, MediaType, ProviderFeature
from music_assistant_models.errors import (
    InvalidDataError,
    MediaNotFoundError,
    MusicAssistantError,
    RetriesExhausted,
)
from music_assistant_models.helpers import create_safe_string
from music_assistant_models.media_items import (
    Album,
    AlbumSummary,
    Artist,
    ItemMapping,
    MediaItemImage,
    ProviderMapping,
    Track,
    UniqueList,
)

from music_assistant.constants import DB_TABLE_ALBUM_ARTISTS, DB_TABLE_ALBUM_TRACKS, DB_TABLE_ALBUMS
from music_assistant.controllers.music.helpers import (
    metadata_for_update,
    provider_mappings_for_update,
    search_name_match_clause,
)
from music_assistant.helpers.compare import (
    ALBUM_RETAIL_SUFFIX_KEYS,
    AlbumMatchEvidence,
    album_tracks_have_positions,
    compare_album_evidence,
    compare_artists,
    loose_compare_strings,
    strip_album_retail_suffix,
)
from music_assistant.helpers.database import UNSET
from music_assistant.helpers.external_ids import barcode_to_upc, is_valid_barcode
from music_assistant.helpers.json import serialize_to_json
from music_assistant.models.music_provider import MusicProvider

from .base import MediaControllerBase

if TYPE_CHECKING:
    from collections.abc import Mapping

    from music_assistant import MusicAssistant
    from music_assistant.providers.musicbrainz import MusicbrainzProvider
    from music_assistant.providers.musicbrainz.models import MusicBrainzBarcodeRelease


# expected failures from a provider album-track lookup: a missing item or a transient
# provider/transport outage. Either leaves that tracklist unavailable so the (best-effort,
# multi-provider) match can continue rather than aborting the whole operation.
_ALBUM_TRACK_LOOKUP_ERRORS = (
    MediaNotFoundError,
    RetriesExhausted,
    TimeoutError,
    aiohttp.ClientError,
)


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
        order_by: str = "sort_name",
        provider: str | list[str] | None = None,
        genre: int | list[int] | None = None,
        played_only: bool = False,
        album_types: list[AlbumType] | None = None,
        *,
        summary: bool = True,
        reachable_via: list[str] | None = None,
        **kwargs: Any,
    ) -> list[Album]:
        """
        Get in-database albums.

        :param favorite: Filter by favorite status.
        :param search: Filter by search query.
        :param limit: Maximum number of items to return.
        :param offset: Number of items to skip.
        :param order_by: Order by field (e.g. 'sort_name', 'timestamp_added').
        :param provider: Filter by provider instance ID (single string or list).
        :param album_types: Filter by album types.
        :param genre: Filter by genre id(s).
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
        artist_table_joined = False
        # optional album type filter
        if album_types:
            extra_query_parts.append("albums.album_type IN :album_types")
            extra_query_params["album_types"] = [x.value for x in album_types]
        if order_by and "album_artist_name" in order_by:
            # join artist table to allow sorting on artist name
            extra_join_parts.append(
                "JOIN album_artists ON album_artists.album_id = albums.item_id "
                "JOIN artists ON artists.item_id = album_artists.artist_id "
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
            artist_clause = "AND " + search_name_match_clause(
                "artists", artist_str, "search_artist", extra_query_params
            )
            # use join with artists table to filter on artist name
            extra_join_parts.append(
                "JOIN album_artists ON album_artists.album_id = albums.item_id "
                "JOIN artists ON artists.item_id = album_artists.artist_id " + artist_clause
                if not artist_table_joined
                else artist_clause
            )
            artist_table_joined = True
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

        # Calculate how many more items we need to reach the original limit
        remaining_limit = limit - len(result)

        if search and len(result) < 25 and not offset and remaining_limit > 0:
            # append artist items to result
            search = create_safe_string(search, True, True)
            artist_clause = "AND " + search_name_match_clause(
                "artists", search, "search_artist", extra_query_params
            )
            extra_join_parts.append(
                "JOIN album_artists ON album_artists.album_id = albums.item_id "
                "JOIN artists ON artists.item_id = album_artists.artist_id " + artist_clause
                if not artist_table_joined
                else artist_clause
            )
            existing_uris = {item.uri for item in result}

            for album in await self.get_library_items_by_query(
                favorite=favorite,
                search=None,
                limit=remaining_limit,
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

        :param favorite_only: Only count albums marked as favorite.
        :param album_types: Only count albums of these types.
        """
        sql_query = f"SELECT item_id FROM {self.db_table}"
        query_parts: list[str] = []
        query_params: dict[str, Any] = {}
        if favorite_only:
            query_parts.append("favorite = 1")
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
        # delete entry(s) from albumtracks table
        await self.mass.music.database.delete(DB_TABLE_ALBUM_TRACKS, {"album_id": db_id})
        # delete entry(s) from album artists table
        await self.mass.music.database.delete(DB_TABLE_ALBUM_ARTISTS, {"album_id": db_id})
        # delete the album itself from db
        # this will raise if the item still has references and recursive is false
        await super().remove_item_from_library(item_id)

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
    ) -> list[Track]:
        """Return album tracks for the given provider album id."""
        # always check if we have a library item for this album
        library_album = await self.get_library_item_by_prov_id(
            item_id, provider_instance_id_or_domain
        )
        if not library_album:
            album_tracks = await self._get_provider_album_tracks(
                item_id, provider_instance_id_or_domain
            )
            # some album-track listings omit the parent album and its image; backfill both
            # from the provider album so the queue shows the album name and artwork.
            if album_tracks and (not album_tracks[0].album or not album_tracks[0].image):
                prov_album = await self.get_provider_item(item_id, provider_instance_id_or_domain)
                album_mapping = ItemMapping.from_item(prov_album)
                for track in album_tracks:
                    if prov_album.image and not track.image:
                        track.metadata.add_image(prov_album.image)
                    if track.album is None:
                        track.album = album_mapping
            return album_tracks

        # respect the current user's provider filter (if any) for both the
        # in-library tracks and the live provider fetches below
        allowed_providers = self._ensure_provider_filter(None)
        db_items = await self.get_library_album_tracks(
            library_album.item_id, provider_filter=allowed_providers
        )
        result: list[Track] = list(db_items)
        if in_library_only:
            # return in-library items only
            return sorted(db_items, key=lambda x: (x.disc_number, x.track_number))

        # return all (unique) items from all providers
        # because we are returning the items from all providers combined,
        # we need to make sure that we don't return duplicates
        unique_ids: set[str] = {f"{x.disc_number}.{x.track_number}" for x in db_items}
        unique_ids.update({f"{x.name.lower()}.{x.version.lower()}" for x in db_items})
        for db_item in db_items:
            unique_ids.update(x.item_id for x in db_item.provider_mappings)
        for provider_mapping in library_album.provider_mappings:
            if (
                allowed_providers is not None
                and provider_mapping.provider_instance not in allowed_providers
            ):
                continue
            provider_tracks = await self._get_provider_album_tracks(
                provider_mapping.item_id, provider_mapping.provider_instance
            )
            for provider_track in provider_tracks:
                # In some cases (looking at you YTM) the disc/track number is not obtained from
                # library_tracks. Ensure to update the disc/track number when interacting with
                # album tracks
                db_track = next(
                    (
                        x
                        for x in db_items
                        if x.sort_name == provider_track.sort_name
                        and x.version == provider_track.version
                    ),
                    None,
                )
                if (
                    db_track
                    and db_track.track_number == 0
                    and db_track.track_number != provider_track.track_number
                ):
                    await self._set_album_track(
                        db_id=int(library_album.item_id),
                        db_track_id=int(db_track.item_id),
                        track=provider_track,
                    )
                if provider_track.item_id in unique_ids:
                    continue
                unique_id = f"{provider_track.disc_number}.{provider_track.track_number}"
                if unique_id in unique_ids:
                    continue
                unique_id = f"{provider_track.name.lower()}.{provider_track.version.lower()}"
                if unique_id in unique_ids:
                    continue
                unique_ids.add(unique_id)
                provider_track.album = library_album
                # always prefer album image
                album_images = [library_album.image] if library_album.image else []
                track_images: list[MediaItemImage] = provider_track.metadata.images or []
                provider_track.metadata.images = UniqueList(album_images + track_images)
                result.append(provider_track)
        # NOTE: we need to return the results sorted on disc/track here
        # to ensure the correct order at playback
        return sorted(result, key=lambda x: (x.disc_number, x.track_number))

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
            # YTM hates us and won't surface certain variants via search. If
            # this crops up in other providers it's worth better integrating
            # this case.
            if (
                (
                    mapped_id := next(
                        (
                            p.item_id
                            for p in album.provider_mappings
                            if p.provider_domain == "ytmusic"
                        ),
                        None,
                    )
                )
                and hasattr(provider, "get_album_versions")
                and callable(provider.get_album_versions)
            ):
                result.extend(await provider.get_album_versions(mapped_id))
        return result

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

        Links albums of different providers/qualities together. Sparse provider search
        results only rule out a confident non-match; a candidate that still looks
        ambiguous is confirmed against the full provider album, its tracklist and, as a
        last resort, MusicBrainz before its provider mapping is accepted.
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
        processed_domains = set()
        for provider in self.mass.music.providers:
            if provider.domain in processed_domains:
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
                processed_domains.add(provider.domain)

    def album_from_item_mapping(self, item: ItemMapping) -> Album:
        """Create an Album object from an ItemMapping object."""
        domain, instance_id = None, None
        if prov := self.mass.get_provider(item.provider):
            domain = prov.domain
            instance_id = prov.instance_id
        return Album.from_dict(
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
                "favorite": item.favorite,
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
        """Search one provider and return the mappings of every confirmed album match."""
        self.logger.debug("Trying to match album %s on provider %s", db_album.name, provider.name)
        matches: list[ProviderMapping] = []
        search_str = (
            f"{db_album.artists[0].name} - {db_album.name}" if db_album.artists else db_album.name
        )
        for search_result_item in await self.search(search_str, provider.instance_id):
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
        except _ALBUM_TRACK_LOOKUP_ERRORS as err:
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
            except _ALBUM_TRACK_LOOKUP_ERRORS as err:
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
        except (RetriesExhausted, InvalidDataError, TimeoutError, aiohttp.ClientError) as err:
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

    def _parse_summary_row(self, db_row: Mapping[str, Any]) -> AlbumSummary:
        """Parse a raw summary db row into an AlbumSummary object."""
        item = cast("AlbumSummary", super()._parse_summary_row(db_row))
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
