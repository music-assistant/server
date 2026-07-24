"""RecommendationsMixin for Audiobookshelf."""

from __future__ import annotations

import itertools
from collections.abc import Sequence
from typing import TYPE_CHECKING

from aioaudiobookshelf.schema.author import AuthorExpanded
from aioaudiobookshelf.schema.shelf import (
    LibraryItemMinifiedPodcast as ShelfLibraryItemMinifiedPodcast,
)
from aioaudiobookshelf.schema.shelf import (
    SeriesShelf,
    ShelfAuthors,
    ShelfBook,
    ShelfEpisode,
    ShelfLibraryItemMinified,
    ShelfPodcast,
    ShelfSeries,
)
from aioaudiobookshelf.schema.shelf import ShelfId as AbsShelfId
from aioaudiobookshelf.schema.shelf import ShelfType as AbsShelfType
from music_assistant_models.enums import MediaType
from music_assistant_models.media_items import (
    BrowseFolder,
    ItemMapping,
    MediaItemType,
    UniqueList,
)
from music_assistant_models.media_items.media_item import RecommendationFolder

from music_assistant.providers.audiobookshelf.constants import (
    ABS_SHELF_ID_ICONS,
    ABS_SHELF_ID_TRANSLATION_KEY,
    CONF_URL,
    AbsBrowsePaths,
)
from music_assistant.providers.audiobookshelf.helpers import handle_refresh_token
from music_assistant.providers.audiobookshelf.mixins.mixin_base import MixinBase
from music_assistant.providers.audiobookshelf.parsers import parse_podcast_episode


class RecommendationsMixin(MixinBase):
    """RecommendationsMixin for Audiobookshelf."""

    if TYPE_CHECKING:
        # part of browse mixin:
        def _browse_root(self, append_mediatype_suffix: bool = True) -> Sequence[BrowseFolder]: ...
        def _browse_lib_audiobooks(self, current_path: str) -> Sequence[BrowseFolder]: ...

        # part of RecommendationPayloadMixin
        async def _recommendation_rows_from_payload(self) -> list[RecommendationFolder]: ...
        async def _recommendation_items_from_payload(
            self, item_id: str
        ) -> UniqueList[MediaItemType | ItemMapping | BrowseFolder]: ...

    async def get_recommendations(self) -> list[RecommendationFolder]:
        """Get the available recommendation rows, without items."""
        if len(self.libraries.audiobooks) + len(self.libraries.podcasts) == 0:
            self._log_no_libraries()
            return []
        rows = await self._recommendation_rows_from_payload()
        rows.append(self._browse_recommendation_row())
        return rows

    async def get_recommendation_items(
        self, item_id: str
    ) -> UniqueList[MediaItemType | ItemMapping | BrowseFolder]:
        """
        Get the items for a single recommendation row.

        :param item_id: The item_id of the row, as returned by get_recommendations.
        """
        if item_id == "browse":
            return self._browse_recommendation_items()
        return await self._recommendation_items_from_payload(item_id)

    @handle_refresh_token
    async def _fetch_recommendation_payload(self) -> list[RecommendationFolder]:
        """Fetch the personalized views of all libraries and parse them into shelf folders."""
        # We have to avoid "flooding" the home page, which becomes especially troublesome if users
        # have multiple libraries. Instead we collect per ShelfId, and make sure, that we always get
        # roughly the same amount of items per row, no matter the amount of libraries
        # List of list (one list per lib) here, such that we can pick the items per lib later.
        items_by_shelf_id: dict[AbsShelfId, list[list[MediaItemType | BrowseFolder]]] = {}

        all_libraries = {**self.libraries.audiobooks, **self.libraries.podcasts}
        max_items_per_row = 20
        num_libraries = len(all_libraries)

        if num_libraries == 0:
            self._log_no_libraries()
            return []

        limit_items_per_lib = max_items_per_row // num_libraries
        limit_items_per_lib = 1 if limit_items_per_lib == 0 else limit_items_per_lib

        for library_id in all_libraries:
            shelves = await self._client.get_library_personalized_view(
                library_id=library_id, limit=limit_items_per_lib
            )
            await self._recommendations_iter_shelves(shelves, library_id, items_by_shelf_id)

        folders: list[RecommendationFolder] = []
        for shelf_id, item_lists in items_by_shelf_id.items():
            # we have something like [[A, B], [C, D, E], [F]]
            # and want [A, C, F, B, D, E]
            recommendation_items = [
                x
                for x in itertools.chain.from_iterable(itertools.zip_longest(*item_lists))
                if x is not None
            ][:max_items_per_row]

            # shelf ids follow pattern:
            # recently-added
            # newest-episodes
            # etc
            name = f"{shelf_id.capitalize().replace('-', ' ')}"
            folders.append(
                RecommendationFolder(
                    item_id=f"{shelf_id}",
                    name=name,
                    icon=ABS_SHELF_ID_ICONS.get(shelf_id),
                    translation_key=ABS_SHELF_ID_TRANSLATION_KEY.get(shelf_id),
                    items=UniqueList(recommendation_items),
                    provider=self.instance_id,
                )
            )

        return folders

    def _browse_recommendation_row(self) -> RecommendationFolder:
        """Build the static browse row descriptor, without items."""
        translation_key = "libraries"
        if len(self.libraries.audiobooks) <= 1 and len(self.libraries.podcasts) == 0:
            translation_key = "library"
        return RecommendationFolder(
            item_id="browse",
            name="Libraries",
            icon="mdi-bookshelf",
            translation_key=translation_key,
            provider=self.instance_id,
        )

    def _browse_recommendation_items(
        self,
    ) -> UniqueList[MediaItemType | ItemMapping | BrowseFolder]:
        """Build the items of the browse row from the known libraries."""
        # Browse "recommendation" for convenience. If the user has
        # multiple audiobook libraries, we return a listing of them.
        # If there is only a single audiobook library, we add the folders
        # from _browse_lib_audiobooks, i.e. Authors, Narrators etc.
        # Podcast libs do not have filter folders, so always the root folders.
        browse_items: list[MediaItemType | BrowseFolder] = []
        if len(self.libraries.audiobooks) <= 1:
            # audiobooklibs are first, and we have at max 1 audiobook lib
            _browse_root = self._browse_root(append_mediatype_suffix=False)
            if len(self.libraries.audiobooks) == 0:
                browse_items.extend(_browse_root)
            else:
                assert isinstance(_browse_root[0], BrowseFolder)
                _path = _browse_root[0].path
                browse_items.extend(self._browse_lib_audiobooks(current_path=_path))
                # add podcast roots
                browse_items.extend(_browse_root[1:])
        else:
            browse_items = list(self._browse_root())
        return UniqueList(browse_items)

    async def _recommendations_iter_shelves(
        self,
        shelves: list[ShelfBook | ShelfPodcast | ShelfAuthors | ShelfEpisode | ShelfSeries],
        library_id: str,
        items_by_shelf_id: dict[AbsShelfId, list[list[MediaItemType | BrowseFolder]]],
    ) -> None:
        # ruff: noqa: PLR0915
        for shelf in shelves:
            media_type: MediaType
            match shelf.type_:
                case AbsShelfType.PODCAST:
                    media_type = MediaType.PODCAST
                case AbsShelfType.EPISODE:
                    media_type = MediaType.PODCAST_EPISODE
                case AbsShelfType.BOOK:
                    media_type = MediaType.AUDIOBOOK
                case AbsShelfType.SERIES | AbsShelfType.AUTHORS:
                    media_type = MediaType.FOLDER

            items: list[MediaItemType | BrowseFolder] = []
            # Recently added is the _only_ case, where we get a full podcast
            # We have a podcast object with only the episodes matching the
            # shelf.id_ otherwise.
            match shelf.id_:
                case (
                    AbsShelfId.RECENTLY_ADDED
                    | AbsShelfId.LISTEN_AGAIN
                    | AbsShelfId.DISCOVER
                    | AbsShelfId.NEWEST_EPISODES
                    | AbsShelfId.CONTINUE_LISTENING
                ):
                    for entity in shelf.entities:
                        assert isinstance(entity, ShelfLibraryItemMinified)
                        item: MediaItemType | None = None
                        if media_type in [MediaType.PODCAST, MediaType.AUDIOBOOK]:
                            item = await self.mass.music.get_library_item_by_prov_id(
                                media_type=media_type,
                                provider_instance_id_or_domain=self.instance_id,
                                item_id=entity.id_,
                            )
                        elif media_type == MediaType.PODCAST_EPISODE:
                            podcast_id = entity.id_
                            if entity.recent_episode is None:
                                continue
                            _cover_path = None
                            _cover_version = None
                            _podcast_title = None
                            if isinstance(entity, ShelfLibraryItemMinifiedPodcast):
                                _cover_path = entity.media.cover_path
                                _cover_version = entity.updated_at
                                _podcast_title = entity.media.metadata.title
                            # we only have a PodcastEpisode here, with limited information
                            item = parse_podcast_episode(
                                episode=entity.recent_episode,
                                prov_podcast_id=podcast_id,
                                prov_podcast_name=_podcast_title,
                                instance_id=self.instance_id,
                                domain=self.domain,
                                token=self._client.token,
                                base_url=str(self.config.get_value(CONF_URL)).rstrip("/"),
                                cover_path=_cover_path,
                                cover_version=_cover_version,
                            )
                        if item is not None:
                            items.append(item)
                case AbsShelfId.RECENT_SERIES | AbsShelfId.CONTINUE_SERIES:
                    # We jump into a browse folder here if we have SeriesShelf, set path up as if
                    # browse function used.
                    if isinstance(shelf, ShelfSeries):
                        for entity in shelf.entities:
                            assert isinstance(entity, SeriesShelf)
                            if len(entity.books) == 0:
                                continue
                            path = (
                                f"{self.instance_id}://"
                                f"{AbsBrowsePaths.LIBRARIES_BOOK} {library_id}/"
                                f"{AbsBrowsePaths.SERIES}/{entity.id_}"
                            )
                            items.append(
                                BrowseFolder(
                                    item_id=entity.id_,
                                    name=entity.name,
                                    provider=self.instance_id,
                                    path=path,
                                )
                            )
                    elif isinstance(shelf, ShelfBook) and media_type == MediaType.AUDIOBOOK:
                        # Single books, must be audiobooks
                        for entity in shelf.entities:
                            item = await self.mass.music.get_library_item_by_prov_id(
                                media_type=media_type,
                                provider_instance_id_or_domain=self.instance_id,
                                item_id=entity.id_,
                            )
                            if item is not None:
                                items.append(item)
                case AbsShelfId.NEWEST_AUTHORS:
                    # same as for series, use a folder
                    for entity in shelf.entities:
                        assert isinstance(entity, AuthorExpanded)
                        if entity.num_books == 0:
                            continue
                        path = (
                            f"{self.instance_id}://"
                            f"{AbsBrowsePaths.LIBRARIES_BOOK} {library_id}/"
                            f"{AbsBrowsePaths.AUTHORS}/{entity.id_}"
                        )
                        items.append(
                            BrowseFolder(
                                item_id=entity.id_,
                                name=entity.name,
                                provider=self.instance_id,
                                path=path,
                            )
                        )
            if not items:
                continue

            # add collected items
            assert isinstance(shelf.id_, AbsShelfId)
            items_collected = items_by_shelf_id.get(shelf.id_, [])
            items_collected.append(items)
            items_by_shelf_id[shelf.id_] = items_collected
