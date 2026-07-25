"""BrowseMixin for AudiobookShelf."""

from __future__ import annotations

from collections.abc import Sequence
from typing import cast

from aioaudiobookshelf.schema.calls_authors import (
    AuthorWithItemsAndSeries as AbsAuthorWithItemsAndSeries,
)
from aioaudiobookshelf.schema.calls_series import SeriesWithProgress as AbsSeriesWithProgress
from music_assistant_models.enums import MediaType
from music_assistant_models.media_items import (
    Audiobook,
    BrowseFolder,
    ItemMapping,
    MediaItemType,
)

from music_assistant.providers.audiobookshelf.constants import (
    ABS_BROWSE_ITEMS_BOOK_TO_PATH,
    ABS_BROWSE_ITEMS_PODCAST_TO_PATH,
    AbsBrowseItemsBookTranslationKey,
    AbsBrowseItemsPodcastTranslationKey,
    AbsBrowsePaths,
)
from music_assistant.providers.audiobookshelf.helpers import handle_refresh_token
from music_assistant.providers.audiobookshelf.mixins.mixin_base import AbsMixinBase


class AbsBrowseMixin(AbsMixinBase):
    """Browse Mixin for Audiobookshelf."""

    @handle_refresh_token
    async def browse(self, path: str) -> Sequence[MediaItemType | ItemMapping | BrowseFolder]:
        """
        Browse for audiobookshelf.

        Generates this view:
        Library_Name_A (Audiobooks)
            Audiobooks
                Audiobook_1
                Audiobook_2
            Series
                Series_1
                    Audiobook_1
                    Audiobook_2
                Series_2
                    Audiobook_3
                    Audiobook_4
            Collections
                Collection_1
                    Audiobook_1
                    Audiobook_2
                Collection_2
                    Audiobook_3
                    Audiobook_4
            Authors
                Author_1
                    Series_1
                    Audiobook_1
                    Audiobook_2
                Author_2
                    Audiobook_3
        Library_Name_B (Podcasts)
            Podcast_1
            Podcast_2
        """
        # ruff: noqa: PLR0911 # to many return
        item_path = path.split("://", 1)[1]
        if not item_path:
            return self._browse_root()
        sub_path = item_path.split("/")
        lib_key, lib_id = sub_path[0].split(" ")
        if len(sub_path) == 1:
            if lib_key == AbsBrowsePaths.LIBRARIES_PODCAST:
                return self._browse_lib_podcasts(current_path=path)
            return self._browse_lib_audiobooks(current_path=path)
        if len(sub_path) == 2:
            item_key = sub_path[1]
            match item_key:
                case AbsBrowsePaths.AUTHORS:
                    return await self._browse_authors(current_path=path, library_id=lib_id)
                case AbsBrowsePaths.NARRATORS:
                    return await self._browse_narrators(current_path=path, library_id=lib_id)
                case AbsBrowsePaths.SERIES:
                    return await self._browse_series(current_path=path, library_id=lib_id)
                case AbsBrowsePaths.COLLECTIONS:
                    return await self._browse_collections(current_path=path, library_id=lib_id)
                case AbsBrowsePaths.AUDIOBOOKS:
                    return await self._browse_books(library_id=lib_id)
                case AbsBrowsePaths.PODCASTS:
                    return await self._browse_podcasts(library_id=lib_id)
                case AbsBrowsePaths.PLAYLISTS:
                    return await self._browse_playlists(library_id=lib_id, browse_path=lib_key)
        elif len(sub_path) == 3:
            item_key, item_id = sub_path[1:3]
            match item_key:
                case AbsBrowsePaths.AUTHORS:
                    return await self._browse_author_books(current_path=path, author_id=item_id)
                case AbsBrowsePaths.NARRATORS:
                    return await self._browse_narrator_books(
                        library_id=lib_id, narrator_filter_str=item_id
                    )
                case AbsBrowsePaths.SERIES:
                    return await self._browse_series_books(series_id=item_id)
                case AbsBrowsePaths.COLLECTIONS:
                    return await self._browse_collection_books(collection_id=item_id)
        elif len(sub_path) == 4:
            # series within author
            series_id = sub_path[3]
            return await self._browse_series_books(series_id=series_id)
        return []

    def _browse_root(self, append_mediatype_suffix: bool = True) -> Sequence[BrowseFolder]:
        items = []

        def _get_folder(
            path: str, lib_id: str, lib_name: str, translation_key: str | None = None
        ) -> BrowseFolder:
            return BrowseFolder(
                item_id=lib_id,
                name=lib_name,
                translation_key=translation_key,
                translation_params=[lib_name],
                provider=self.instance_id,
                path=f"{self.instance_id}://{path}",
            )

        if len(self.libraries.audiobooks) == 0 and len(self.libraries.podcasts) == 0:
            self._log_no_libraries()
            return []

        translation_key: str | None
        for lib_id, lib in self.libraries.audiobooks.items():
            path = f"{AbsBrowsePaths.LIBRARIES_BOOK} {lib_id}"
            translation_key = None
            if append_mediatype_suffix:
                translation_key = AbsBrowseItemsBookTranslationKey.AUDIOBOOKS_LIBRARY
            items.append(
                _get_folder(path, lib_id, lib_name=lib.name, translation_key=translation_key)
            )
        for lib_id, lib in self.libraries.podcasts.items():
            path = f"{AbsBrowsePaths.LIBRARIES_PODCAST} {lib_id}"
            translation_key = None
            if append_mediatype_suffix:
                translation_key = AbsBrowseItemsPodcastTranslationKey.PODCASTS_LIBRARY
            items.append(
                _get_folder(path, lib_id, lib_name=lib.name, translation_key=translation_key)
            )
        return items

    def _browse_lib_podcasts(self, current_path: str) -> Sequence[BrowseFolder]:
        items = []
        for translation_key in AbsBrowseItemsPodcastTranslationKey:
            if "library" in translation_key:
                continue
            path = current_path + "/" + ABS_BROWSE_ITEMS_PODCAST_TO_PATH[translation_key]
            items.append(
                BrowseFolder(
                    item_id=translation_key.lower(),
                    name="",
                    translation_key=translation_key,
                    provider=self.instance_id,
                    path=path,
                )
            )
        return items

    async def _browse_podcasts(self, library_id: str) -> list[MediaItemType]:
        """Browse podcasts."""
        if len(self.libraries.podcasts[library_id].item_ids) == 0:
            self._log_no_helper_item_ids()
        items = []
        for podcast_id in self.libraries.podcasts[library_id].item_ids:
            mass_item = await self.mass.music.get_library_item_by_prov_id(
                media_type=MediaType.PODCAST,
                item_id=podcast_id,
                provider_instance_id_or_domain=self.instance_id,
            )
            if mass_item is not None:
                items.append(mass_item)
        return sorted(items, key=lambda x: x.name)

    def _browse_lib_audiobooks(self, current_path: str) -> Sequence[BrowseFolder]:
        items = []
        for translation_key in AbsBrowseItemsBookTranslationKey:
            if "library" in translation_key or "entry" in translation_key:
                continue
            path = current_path + "/" + ABS_BROWSE_ITEMS_BOOK_TO_PATH[translation_key]
            items.append(
                BrowseFolder(
                    item_id=translation_key.lower(),
                    name="",
                    translation_key=translation_key,
                    provider=self.instance_id,
                    path=path,
                )
            )
        return items

    async def _browse_authors(self, current_path: str, library_id: str) -> Sequence[BrowseFolder]:
        abs_authors = await self._client.get_library_authors(library_id=library_id)
        items = []
        for author in abs_authors:
            path = f"{current_path}/{author.id_}"
            items.append(
                BrowseFolder(
                    item_id=author.id_,
                    name=author.name,
                    provider=self.instance_id,
                    path=path,
                )
            )

        return sorted(items, key=lambda x: x.name)

    async def _browse_narrators(self, current_path: str, library_id: str) -> Sequence[BrowseFolder]:
        abs_narrators = await self._client.get_library_narrators(library_id=library_id)
        items = []
        for narrator in abs_narrators:
            path = f"{current_path}/{narrator.id_}"
            items.append(
                BrowseFolder(
                    item_id=narrator.id_,
                    name=narrator.name,
                    provider=self.instance_id,
                    path=path,
                )
            )

        return sorted(items, key=lambda x: x.name)

    async def _browse_series(self, current_path: str, library_id: str) -> Sequence[BrowseFolder]:
        items = []
        async for response in self._client.get_library_series(library_id=library_id):
            if not response.results:
                break
            for abs_series in response.results:
                path = f"{current_path}/{abs_series.id_}"
                items.append(
                    BrowseFolder(
                        item_id=abs_series.id_,
                        name=abs_series.name,
                        provider=self.instance_id,
                        path=path,
                    )
                )

        return sorted(items, key=lambda x: x.name)

    async def _browse_collections(
        self, current_path: str, library_id: str
    ) -> Sequence[BrowseFolder]:
        items = []
        async for response in self._client.get_library_collections(library_id=library_id):
            if not response.results:
                break
            for abs_collection in response.results:
                path = f"{current_path}/{abs_collection.id_}"
                items.append(
                    BrowseFolder(
                        item_id=abs_collection.id_,
                        name=abs_collection.name,
                        provider=self.instance_id,
                        path=path,
                    )
                )
        return sorted(items, key=lambda x: x.name)

    @handle_refresh_token
    async def _browse_playlists(self, library_id: str, browse_path: str) -> Sequence[MediaItemType]:
        items = []
        if browse_path == AbsBrowsePaths.LIBRARIES_PODCAST:
            playlists = self.libraries.playlists_podcasts
            if len(self.libraries.playlists_podcasts) == 0:
                self._log_no_helper_item_ids()
        elif browse_path == AbsBrowsePaths.LIBRARIES_BOOK:
            playlists = self.libraries.playlists_audiobooks
            if len(self.libraries.playlists_audiobooks) == 0:
                self._log_no_helper_item_ids()
        else:
            raise RuntimeError("Unknown media type in browse playlist.")
        for playlist_id in playlists[library_id]:
            mass_item = await self.mass.music.get_library_item_by_prov_id(
                media_type=MediaType.PLAYLIST,
                item_id=playlist_id,
                provider_instance_id_or_domain=self.instance_id,
            )
            if mass_item is not None:
                items.append(mass_item)
        return sorted(items, key=lambda x: x.name)

    async def _browse_books(self, library_id: str) -> Sequence[MediaItemType]:
        if len(self.libraries.audiobooks[library_id].item_ids) == 0:
            self._log_no_helper_item_ids()
        items = []
        for book_id in self.libraries.audiobooks[library_id].item_ids:
            mass_item = await self.mass.music.get_library_item_by_prov_id(
                media_type=MediaType.AUDIOBOOK,
                item_id=book_id,
                provider_instance_id_or_domain=self.instance_id,
            )
            if mass_item is not None:
                items.append(mass_item)
        return sorted(items, key=lambda x: x.name)

    async def _browse_author_books(
        self, current_path: str, author_id: str
    ) -> Sequence[MediaItemType | BrowseFolder]:
        items: list[MediaItemType | BrowseFolder] = []

        abs_author = await self._client.get_author(
            author_id=author_id, include_items=True, include_series=True
        )
        if not isinstance(abs_author, AbsAuthorWithItemsAndSeries):
            raise TypeError("Unexpected type of author.")

        book_ids = {x.id_ for x in abs_author.library_items}
        series_book_ids = set()

        for series in abs_author.series:
            series_book_ids.update([x.id_ for x in series.items])
            path = f"{current_path}/{series.id_}"
            items.append(
                BrowseFolder(
                    item_id=series.id_,
                    name=series.name,
                    translation_key="series_entry",
                    translation_params=[series.name],
                    provider=self.instance_id,
                    path=path,
                )
            )
        book_ids = book_ids.difference(series_book_ids)
        for book_id in book_ids:
            mass_item = await self.mass.music.get_library_item_by_prov_id(
                media_type=MediaType.AUDIOBOOK,
                item_id=book_id,
                provider_instance_id_or_domain=self.instance_id,
            )
            if mass_item is not None:
                items.append(mass_item)

        return items

    async def _browse_narrator_books(
        self, library_id: str, narrator_filter_str: str
    ) -> Sequence[Audiobook]:
        items: list[Audiobook] = []
        async for response in self._client.get_library_items(
            library_id=library_id, filter_str=f"narrators.{narrator_filter_str}"
        ):
            if not response.results:
                break
            for item in response.results:
                mass_item = await self.mass.music.get_library_item_by_prov_id(
                    media_type=MediaType.AUDIOBOOK,
                    item_id=item.id_,
                    provider_instance_id_or_domain=self.instance_id,
                )
                if mass_item is not None:
                    mass_item = cast("Audiobook", mass_item)
                    items.append(mass_item)

        return sorted(items, key=lambda x: x.name)

    async def _browse_series_books(self, series_id: str) -> Sequence[MediaItemType]:
        items = []

        abs_series = await self._client.get_series(series_id=series_id, include_progress=True)
        if not isinstance(abs_series, AbsSeriesWithProgress):
            raise TypeError("Unexpected series type.")

        for book_id in abs_series.progress.library_item_ids:
            # these are sorted in abs by sequence
            mass_item = await self.mass.music.get_library_item_by_prov_id(
                media_type=MediaType.AUDIOBOOK,
                item_id=book_id,
                provider_instance_id_or_domain=self.instance_id,
            )
            if mass_item is not None:
                items.append(mass_item)

        return items

    async def _browse_collection_books(self, collection_id: str) -> Sequence[MediaItemType]:
        items = []
        abs_collection = await self._client.get_collection(collection_id=collection_id)
        for book in abs_collection.books:
            mass_item = await self.mass.music.get_library_item_by_prov_id(
                media_type=MediaType.AUDIOBOOK,
                item_id=book.id_,
                provider_instance_id_or_domain=self.instance_id,
            )
            if mass_item is not None:
                items.append(mass_item)
        return items
