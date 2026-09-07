"""Manage MediaItems of type Audiobook."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime
from json import loads as json_loads
from typing import TYPE_CHECKING, Any, Literal, cast, overload

from music_assistant_models.auth import Scope
from music_assistant_models.enums import ArtistType, MediaType, ProviderFeature
from music_assistant_models.helpers import create_safe_string
from music_assistant_models.media_items import (
    Artist,
    Audiobook,
    AudiobookSummary,
    ItemMapping,
    ItemMappingSummary,
    MediaCollection,
    ProviderMapping,
    UniqueList,
)

from music_assistant.constants import (
    DB_TABLE_AUDIOBOOK_ARTISTS,
    DB_TABLE_AUDIOBOOKS,
    DB_TABLE_PLAYLOG,
)
from music_assistant.controllers.webserver.helpers.auth_middleware import get_current_user
from music_assistant.helpers.compare import (
    compare_audiobook,
    compare_media_item,
    loose_compare_strings,
)
from music_assistant.helpers.database import UNSET
from music_assistant.helpers.datetime import utc_timestamp
from music_assistant.helpers.json import serialize_to_json
from music_assistant.helpers.util import parse_optional_bool
from music_assistant.models.music_provider import MusicProvider

from .base import AudiobookSyncDetails, MediaControllerBase

if TYPE_CHECKING:
    from collections.abc import Mapping

    from music_assistant_models.auth import User

    from music_assistant import MusicAssistant


class AudiobooksController(MediaControllerBase[Audiobook]):
    """Controller managing MediaItems of type Audiobook."""

    db_table = DB_TABLE_AUDIOBOOKS
    media_type = MediaType.AUDIOBOOK
    item_cls = Audiobook
    summary_item_cls = AudiobookSummary

    def __init__(self, mass: MusicAssistant) -> None:
        """Initialize class."""
        super().__init__(mass)
        # register (extra) api handlers
        api_base = self.api_base
        self.mass.register_api_command(
            f"music/{api_base}/audiobook_versions", self.versions, required_scope=Scope.LIBRARY_READ
        )

    @property
    def base_query(self) -> tuple[str, dict[str, Any]]:
        """
        Return the base SELECT query for audiobooks and its bound query params.

        The playlog table is joined to hydrate per-user resume info (fully_played,
        resume_position_ms). When a session user is present the join is scoped to that
        user, so multi-user installs don't surface each other's resume state.
        """
        params: dict[str, Any] = {}
        # scope the playlog lookup to the session user (if any) and pick at most one
        # row (the most recent) so the join can never fan out the result set
        playlog_user_clause = ""
        if session_user := get_current_user():
            playlog_user_clause = "AND p2.userid = :playlog_userid "
            params["playlog_userid"] = session_user.user_id
        query = f"""
        SELECT
            audiobooks.*,
            {self._external_ids_query()} AS external_ids,
            {self._provider_mappings_query()} AS provider_mappings,
            (SELECT JSON_GROUP_ARRAY(
                json_object(
                 'item_id', artists.item_id,
                 'provider', 'library',
                 'name', artists.name,
                 'sort_name', artists.sort_name,
                 'media_type', 'artist',
                 'artist_type', artists.artist_type
            ))
            FROM artists JOIN audiobook_artists on audiobook_artists.audiobook_id = audiobooks.item_id WHERE artists.item_id = audiobook_artists.artist_id) AS audiobook_artists,
            playlog.fully_played AS fully_played,
            playlog.seconds_played AS seconds_played,
            playlog.seconds_played * 1000 as resume_position_ms
            FROM audiobooks
            LEFT JOIN playlog ON playlog.id = (
                SELECT p2.id FROM playlog p2
                WHERE p2.item_id = CAST(audiobooks.item_id AS TEXT)
                AND p2.media_type = 'audiobook'
                {playlog_user_clause}ORDER BY p2.timestamp DESC LIMIT 1)
            """
        return query, params

    @property
    def summary_query(self) -> tuple[str, dict[str, Any]]:
        """
        Return the slim SELECT query used for audiobook summary listings.

        Joins the playlog table the same way as the base query to hydrate the
        per-user resume info (fully_played, resume_position_ms).
        """
        params: dict[str, Any] = {}
        playlog_user_clause = ""
        if session_user := get_current_user():
            playlog_user_clause = "AND p2.userid = :playlog_userid "
            params["playlog_userid"] = session_user.user_id
        artists_query = self._artist_mappings_summary_query(
            DB_TABLE_AUDIOBOOK_ARTISTS, "audiobook_id", include_artist_type=True
        )
        query = f"""
        SELECT
            {self._summary_base_columns()},
            audiobooks.version,
            audiobooks.publisher,
            audiobooks.duration,
            audiobooks.authors,
            audiobooks.narrators,
            {self._provider_mappings_query()} AS provider_mappings,
            {artists_query} AS audiobook_artists,
            playlog.fully_played AS fully_played,
            playlog.seconds_played * 1000 as resume_position_ms
            FROM audiobooks
            LEFT JOIN playlog ON playlog.id = (
                SELECT p2.id FROM playlog p2
                WHERE p2.item_id = CAST(audiobooks.item_id AS TEXT)
                AND p2.media_type = 'audiobook'
                {playlog_user_clause}ORDER BY p2.timestamp DESC LIMIT 1)
            """
        return query, params

    if TYPE_CHECKING:

        @overload
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
            *,
            summary: bool = True,
            collapse_collections: Literal[False] = False,
            reachable_via: list[str] | None = None,
            **kwargs: Any,
        ) -> list[Audiobook]: ...

        @overload
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
            *,
            summary: bool = True,
            collapse_collections: Literal[True],
            reachable_via: list[str] | None = None,
            **kwargs: Any,
        ) -> list[Audiobook] | list[Audiobook | MediaCollection[Audiobook]]: ...

        @overload
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
            *,
            summary: bool = True,
            collapse_collections: bool,
            reachable_via: list[str] | None = None,
            **kwargs: Any,
        ) -> list[Audiobook] | list[Audiobook | MediaCollection[Audiobook]]: ...

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
        *,
        summary: bool = True,
        collapse_collections: bool = False,
        reachable_via: list[str] | None = None,
        **kwargs: Any,
    ) -> list[Audiobook] | list[Audiobook | MediaCollection[Audiobook]]:
        """
        Get in-database audiobooks.

        :param favorite: Filter by favorite status.
        :param search: Filter by search query.
        :param limit: Maximum number of items to return.
        :param offset: Number of items to skip.
        :param order_by: Order by field (e.g. 'sort_name', 'timestamp_added').
        :param provider: Filter by provider instance ID (single string or list).
        :param genre: Filter by genre id(s).
        :param summary: When True (default), return slim summary items containing only the
            fields needed for a list view. Set to False to get fully hydrated items.
        :param collapse_collections: Collapse available collections. Items in a collection won't
            be returned individually.
        :param reachable_via: Restrict results to items with a provider mapping reachable
            through one of these provider instance ids (OR semantics). See
            `MediaControllerBase.library_items` for the full semantics.
        """
        reachable_via = self._resolve_reachable_via(reachable_via)
        if reachable_via is not None and not reachable_via:
            return []
        extra_query_params: dict[str, Any] = {}
        extra_query_parts: list[str] = []
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
            played_only=played_only,
            in_library_only=True,
            summary=summary,
            collapse_collections=collapse_collections,
            reachable_via=reachable_via,
        )
        if search and len(result) < 25 and not offset:
            # append author items to result
            extra_query_parts = [
                "WHERE audiobooks.authors LIKE :search or audiobooks.narrators LIKE :search",
            ]
            extra_query_params["search"] = f"%{search}%"
            return result + await self.get_library_items_by_query(
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
                in_library_only=True,
                summary=summary,
                collapse_collections=collapse_collections,
                reachable_via=reachable_via,
            )
        return result

    async def versions(
        self,
        item_id: str,
        provider_instance_id_or_domain: str,
    ) -> UniqueList[Audiobook]:
        """Return all versions of an audiobook we can find on all providers."""
        audiobook = await self.get_provider_item(item_id, provider_instance_id_or_domain)
        search_query = audiobook.name
        result: UniqueList[Audiobook] = UniqueList()
        for provider_id in self.mass.music.get_unique_providers():
            provider = self.mass.get_provider(provider_id)
            if not isinstance(provider, MusicProvider):
                continue
            if MediaType.AUDIOBOOK not in provider.supported_media_types:
                continue
            result.extend(
                prov_item
                for prov_item in await self.search(search_query, provider_id)
                if loose_compare_strings(audiobook.name, prov_item.name)
                # make sure that the 'base' version is NOT included
                and not audiobook.provider_mappings.intersection(prov_item.provider_mappings)
            )
        return result

    async def match_provider(
        self, db_audiobook: Audiobook, provider: MusicProvider, strict: bool = True
    ) -> list[ProviderMapping]:
        """
        Try to find match on (streaming) provider for the provided (database) audiobook.

        This is used to link objects of different providers/qualities together.
        """
        self.logger.debug(
            "Trying to match audiobook %s on provider %s",
            db_audiobook.name,
            provider.name,
        )
        matches: list[ProviderMapping] = []
        author_name = db_audiobook.authors[0] if db_audiobook.authors else ""
        search_str = f"{author_name} - {db_audiobook.name}" if author_name else db_audiobook.name
        search_result = await self.search(search_str, provider.instance_id)
        for search_result_item in search_result:
            if not search_result_item.available:
                continue
            if not compare_media_item(db_audiobook, search_result_item, strict=strict):
                continue
            # we must fetch the full audiobook version, search results can be simplified objects
            prov_audiobook = await self.get_provider_item(
                search_result_item.item_id,
                search_result_item.provider,
                fallback=search_result_item,
            )
            if compare_audiobook(db_audiobook, prov_audiobook, strict=strict):
                # 100% match
                matches.extend(prov_audiobook.provider_mappings)
        if not matches:
            self.logger.debug(
                "Could not find match for Audiobook %s on provider %s",
                db_audiobook.name,
                provider.name,
            )
        return matches

    async def match_providers(self, db_audiobook: Audiobook) -> None:
        """
        Try to find match on all (streaming) providers for the provided (database) audiobook.

        This is used to link objects of different providers/qualities together.
        """
        if db_audiobook.provider != "library":
            return  # Matching only supported for database items

        # try to find match on all providers
        cur_provider_domains = {x.provider_domain for x in db_audiobook.provider_mappings}
        for provider in self.mass.music.providers:
            if provider.domain in cur_provider_domains:
                continue
            if ProviderFeature.SEARCH not in provider.supported_features:
                continue
            if MediaType.AUDIOBOOK not in provider.supported_media_types:
                continue
            if not provider.is_streaming_provider:
                # matching on unique providers is pointless as they push (all) their content to MA
                continue
            if match := await self.match_provider(db_audiobook, provider):
                # 100% match, we update the db with the additional provider mapping(s)
                await self.add_provider_mappings(db_audiobook.item_id, match)
                cur_provider_domains.add(provider.domain)

    async def remove_item_from_library(self, item_id: str | int, recursive: bool = True) -> None:
        """Delete item from the library(database)."""
        db_id = int(item_id)  # ensure integer
        # delete entry(s) from album artists table
        await self.mass.music.database.delete(DB_TABLE_AUDIOBOOK_ARTISTS, {"audiobook_id": db_id})
        # delete the album itself from db
        # this will raise if the item still has references and recursive is false
        await super().remove_item_from_library(item_id)

    async def _add_library_item(self, item: Audiobook, overwrite_existing: bool = False) -> int:
        """Add a new record to the database."""
        # only serialize str narrators/ authors to db
        _authors = [author for author in item.authors if isinstance(author, str)]
        _narrators = [narrator for narrator in item.narrators if isinstance(narrator, str)]
        db_id = await self.mass.music.database.insert(
            self.db_table,
            {
                "name": item.name,
                "sort_name": item.sort_name,
                "version": item.version,
                "favorite": item.favorite,
                "metadata": serialize_to_json(item.metadata),
                "publisher": item.publisher,
                "authors": serialize_to_json(_authors),
                "narrators": serialize_to_json(_narrators),
                "duration": item.duration,
                "search_name": create_safe_string(item.name, True, True),
                "search_sort_name": create_safe_string(item.sort_name or "", True, True),
                "timestamp_added": int(item.date_added.timestamp()) if item.date_added else UNSET,
            },
        )
        # update/set external id lookup table
        await self.set_external_ids(db_id, item.external_ids)
        # update/set provider_mappings table
        await self.set_provider_mappings(db_id, item.provider_mappings)
        self.logger.debug("added %s to database (id: %s)", item.name, db_id)
        await self._set_playlog(db_id, item)
        await self._set_artist_mappings(item, db_id)

        return db_id

    async def _set_artist_mappings(
        self, item: Audiobook, db_id: int, overwrite: bool = False
    ) -> None:
        # update artist mappings - the sync method in the provider model raises an exception
        # if not all entries are either of type str or Artist
        if overwrite:
            # on overwrite, clear the audiobook_artists table first
            await self.mass.music.database.delete(
                DB_TABLE_AUDIOBOOK_ARTISTS,
                {
                    "audiobook_id": db_id,
                },
            )
        if item.authors and isinstance(item.authors[0], Artist):
            # only for type checking
            authors = [author for author in item.authors if isinstance(author, Artist)]
            for author in authors:
                # just to be sure
                author.artist_type = ArtistType.AUTHOR
            await self._set_audiobook_authors_narrators(db_id, authors)
        if item.narrators and isinstance(item.narrators[0], Artist):
            # only for type checking
            narrators = [narrator for narrator in item.narrators if isinstance(narrator, Artist)]
            for narrator in narrators:
                # just to be sure
                narrator.artist_type = ArtistType.NARRATOR
            await self._set_audiobook_authors_narrators(db_id, narrators)

    async def _set_audiobook_authors_narrators(
        self,
        db_id: int,
        artists: Iterable[Artist | ItemMapping],
        overwrite: bool = False,
    ) -> None:
        """Write audiobook id and author/ narrator id to DB_TABLE_AUDIOBOOK_ARTISTS."""
        for artist in artists:
            await self._set_audiobook_author_narrator(db_id, artist=artist, overwrite=overwrite)

    async def _set_audiobook_author_narrator(
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
            DB_TABLE_AUDIOBOOK_ARTISTS,
            {
                "audiobook_id": db_id,
                "artist_id": int(db_artist.item_id),
            },
        )
        return ItemMapping.from_item(db_artist)

    async def _update_library_item(
        self,
        item_id: str | int,
        update: Audiobook,
        overwrite: bool = False,
        *,
        set_playlog: bool = True,
    ) -> None:
        """Update existing record in the database."""
        db_id = int(item_id)  # ensure integer
        cur_item = await self.get_library_item(db_id)
        metadata = update.metadata if overwrite else cur_item.metadata.update(update.metadata)
        if not overwrite and update.metadata.images is not None:
            # audiobooks have no image picker, so keep the cover in sync with the
            # provider instead of accumulating merged entries
            metadata.images = update.metadata.images
        if not overwrite and update.metadata.collections is not None:
            # always update collections to prevent stale empty ones
            metadata.collections = update.metadata.collections
        cur_item.external_ids.update(update.external_ids)
        name = update.name if overwrite else cur_item.name
        sort_name = update.sort_name if overwrite else cur_item.sort_name or update.sort_name
        # only serialize str narrators/ authors to db
        _update_authors = [author for author in update.authors if isinstance(author, str)]
        _update_narrators = [narrator for narrator in update.narrators if isinstance(narrator, str)]
        await self.mass.music.database.update(
            self.db_table,
            {"item_id": db_id},
            {
                "name": name,
                "sort_name": sort_name,
                "version": update.version if overwrite else cur_item.version or update.version,
                "metadata": serialize_to_json(metadata),
                "publisher": cur_item.publisher or update.publisher,
                "authors": serialize_to_json(
                    _update_authors if overwrite else cur_item.authors or _update_authors
                ),
                "narrators": serialize_to_json(
                    _update_narrators if overwrite else cur_item.narrators or _update_narrators
                ),
                "duration": update.duration if overwrite else cur_item.duration or update.duration,
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
        self.logger.debug("updated %s in database: (id %s)", update.name, db_id)
        if set_playlog:
            await self._set_playlog(db_id, update)
        await self._set_artist_mappings(update, db_id)

    async def _update_library_item_for_merge(self, item_id: int, update: Audiobook) -> None:
        """Merge audiobook model state without applying a source resume position."""
        await self._update_library_item(item_id, update, set_playlog=False)

    async def _set_playlog(self, db_id: int, media_item: Audiobook) -> None:
        """Update/set the playlog table for the given audiobook db item_id."""
        # Get user(s)
        user: User | None = None
        if session_user := get_current_user():
            # this is the active session user that triggered the action
            user = session_user
        elif provider_user := await self.mass.music._get_user_for_provider(
            media_item.provider_mappings
        ):
            # based on configured provider filter we can try to find a user
            user = provider_user
        if user:
            user_ids = [user.user_id]
        else:
            # NOTE: if no user was found, we will alter the playlog for all users
            user_ids = [user.user_id for user in await self.mass.webserver.auth.list_users()]

        # cleanup provider specific entries for this item
        # we always prefer the library playlog entry
        for prov_mapping in media_item.provider_mappings:
            for user_id in user_ids:
                await self.mass.music.database.delete(
                    DB_TABLE_PLAYLOG,
                    {
                        "media_type": self.media_type.value,
                        "item_id": prov_mapping.item_id,
                        "provider": prov_mapping.provider_instance,
                        "userid": user_id,
                    },
                )
        if media_item.fully_played is None and media_item.resume_position_ms is None:
            return

        for user_id in user_ids:
            cur_entry = await self.mass.music.database.get_row(
                DB_TABLE_PLAYLOG,
                {
                    "media_type": self.media_type.value,
                    "item_id": db_id,
                    "provider": "library",
                    "userid": user_id,
                },
            )
            seconds_played = int((media_item.resume_position_ms or 0) / 1000)
            # abort if nothing changed
            if (
                cur_entry
                and parse_optional_bool(cur_entry["fully_played"]) == media_item.fully_played
                and abs((cur_entry["seconds_played"] or 0) - seconds_played) <= 2
            ):
                return

            await self.mass.music.database.insert(
                DB_TABLE_PLAYLOG,
                {
                    "item_id": db_id,
                    "provider": "library",
                    "media_type": media_item.media_type.value,
                    "name": media_item.name,
                    "image": serialize_to_json(media_item.image.to_dict())
                    if media_item.image
                    else None,
                    "fully_played": media_item.fully_played,
                    "seconds_played": seconds_played,
                    "timestamp": utc_timestamp(),
                    "userid": user_id,
                },
                allow_replace=True,
            )

    async def _authors_narrators(self, column: str) -> UniqueList[str]:
        """Return all available authors."""
        assert self.mass.music.database is not None  # for type checking
        rows = await self.mass.music.database.get_rows_from_query(
            query=f"SELECT DISTINCT {column} FROM {DB_TABLE_AUDIOBOOKS}"
        )
        result: set[str] = set()
        for row in rows:
            result.update(json_loads(row[column]))
        return UniqueList(sorted(result))

    def _sync_details_query_parts(self) -> tuple[str, str, dict[str, Any]]:
        """Return extra (columns, joins, params) for the audiobooks sync-details query."""
        # the sync loop needs the (str vs Artist) type of the stored authors/narrators
        # plus the user-scoped resume state to detect changes on the provider side
        params: dict[str, Any] = {}
        # mirror base_query: scope the playlog lookup to the session user (if any) and
        # pick at most one row (the most recent) so the join can never fan out
        playlog_user_clause = ""
        if session_user := get_current_user():
            playlog_user_clause = "AND p2.userid = :playlog_userid "
            params["playlog_userid"] = session_user.user_id
        extra_columns = f"""
            , EXISTS (
                SELECT 1 FROM {DB_TABLE_AUDIOBOOK_ARTISTS}
                JOIN artists ON artists.item_id = audiobook_artists.artist_id
                WHERE audiobook_artists.audiobook_id = audiobooks.item_id
                AND artists.artist_type = '{ArtistType.AUTHOR.value}'
            ) AS has_author_artists
            , EXISTS (
                SELECT 1 FROM {DB_TABLE_AUDIOBOOK_ARTISTS}
                JOIN artists ON artists.item_id = audiobook_artists.artist_id
                WHERE audiobook_artists.audiobook_id = audiobooks.item_id
                AND artists.artist_type = '{ArtistType.NARRATOR.value}'
            ) AS has_narrator_artists
            , json_type(audiobooks.authors, '$[0]') AS first_author_type
            , json_type(audiobooks.narrators, '$[0]') AS first_narrator_type
            , playlog.fully_played AS fully_played
            , playlog.seconds_played * 1000 AS resume_position_ms
        """
        extra_joins = (
            f"LEFT JOIN {DB_TABLE_PLAYLOG} ON playlog.id = ("
            f"SELECT p2.id FROM {DB_TABLE_PLAYLOG} p2 "
            "WHERE p2.item_id = CAST(audiobooks.item_id AS TEXT) "
            "AND p2.media_type = 'audiobook' "
            f"{playlog_user_clause}ORDER BY p2.timestamp DESC LIMIT 1)"
        )
        return extra_columns, extra_joins, params

    def _parse_sync_details_row(self, db_row: Mapping[str, Any]) -> AudiobookSyncDetails:
        """Parse a raw sync-details db row into an AudiobookSyncDetails object."""
        # authors/narrators hydrate as str only when there are no linked Artist records
        # and the stored JSON column holds plain strings (mirrors _parse_db_row)
        resume_position_ms = db_row["resume_position_ms"]
        return AudiobookSyncDetails(
            item_id=db_row["item_id"],
            favorite=bool(db_row["favorite"]),
            date_added=datetime.fromtimestamp(db_row["timestamp_added"], tz=UTC),
            provider_mappings=self._parse_sync_details_mappings(db_row),
            author_is_str=not db_row["has_author_artists"]
            and db_row["first_author_type"] == "text",
            narrator_is_str=not db_row["has_narrator_artists"]
            and db_row["first_narrator_type"] == "text",
            fully_played=parse_optional_bool(db_row["fully_played"]),
            resume_position_ms=int(resume_position_ms) if resume_position_ms is not None else None,
        )

    def _parse_summary_row(self, db_row: Mapping[str, Any]) -> AudiobookSummary:
        """Parse a raw summary db row into an AudiobookSummary object."""
        item = cast("AudiobookSummary", super()._parse_summary_row(db_row))
        item.version = db_row["version"] or ""
        item.publisher = db_row["publisher"]
        item.duration = db_row["duration"] or 0
        item.fully_played = parse_optional_bool(db_row["fully_played"])
        item.resume_position_ms = db_row["resume_position_ms"]
        # authors/narrators: prefer the linked artist records (as slim mappings),
        # fall back to the plain string values stored on the audiobook itself
        authors: list[ItemMappingSummary] = []
        narrators: list[ItemMappingSummary] = []
        if raw_audiobook_artists := db_row["audiobook_artists"]:
            for artist in json_loads(raw_audiobook_artists):
                mapping = ItemMappingSummary(
                    media_type=MediaType.ARTIST,
                    item_id=str(artist["item_id"]),
                    provider="library",
                    name=artist["name"],
                    sort_name=artist["sort_name"],
                )
                if artist["artist_type"] == ArtistType.AUTHOR.value:
                    authors.append(mapping)
                elif artist["artist_type"] == ArtistType.NARRATOR.value:
                    narrators.append(mapping)
        item.authors = UniqueList(authors or json_loads(db_row["authors"] or "[]"))
        item.narrators = UniqueList(narrators or json_loads(db_row["narrators"] or "[]"))
        return item
