"""AudiobooksMixin for Audiobookshelf."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING

from aioaudiobookshelf.schema.library import LibraryItemExpandedBook as AbsLibraryItemExpandedBook
from music_assistant_models.enums import MediaType

from music_assistant.providers.audiobookshelf.constants import CONF_URL
from music_assistant.providers.audiobookshelf.helpers import NarratorHelper, handle_refresh_token
from music_assistant.providers.audiobookshelf.mixins.mixin_base import AbsMixinBase
from music_assistant.providers.audiobookshelf.parsers import parse_audiobook

if TYPE_CHECKING:
    from aioaudiobookshelf.schema.media_progress import MediaProgress
    from music_assistant_models.media_items import Audiobook


class AbsAudiobooksMixin(AbsMixinBase):
    """Audiobooks handling for Audiobookshelf."""

    if TYPE_CHECKING:
        # part of artist mixin
        async def _update_book_narrators(self, library_id: str) -> None: ...

    async def get_library_audiobooks(self) -> AsyncGenerator[Audiobook]:
        """
        Get Audiobook libraries.

        Need expanded version for chapters.
        """
        for book_lib_id in self.libraries.audiobooks:
            async for response in self._client.get_library_items(library_id=book_lib_id):
                if not response.results:
                    break
                book_ids = [x.id_ for x in response.results]
                # store uuids
                self.libraries.audiobooks[book_lib_id].item_ids.update(book_ids)
                # use expanded version for chapters/ caching.
                books_expanded = await self._client.get_library_item_batch_book(item_ids=book_ids)
                for book_expanded in books_expanded:
                    # If the book has no audiofiles, we skip -> ebook only.
                    if len(book_expanded.media.tracks) == 0:
                        continue
                    mass_audiobook = parse_audiobook(
                        abs_audiobook=book_expanded,
                        audiobook_narrators=await self._get_audiobook_narrators(book_expanded),
                        instance_id=self.instance_id,
                        domain=self.domain,
                        token=self._client.token,
                        base_url=str(self.config.get_value(CONF_URL)).rstrip("/"),
                    )
                    yield mass_audiobook

    @handle_refresh_token
    async def get_audiobook(self, prov_audiobook_id: str) -> Audiobook:
        """
        Get a single audiobook.

        Progress is added here.
        """
        progress = await self._client.get_my_media_progress(item_id=prov_audiobook_id)
        abs_audiobook = await self._get_abs_expanded_audiobook(prov_audiobook_id=prov_audiobook_id)
        return parse_audiobook(
            abs_audiobook=abs_audiobook,
            audiobook_narrators=await self._get_audiobook_narrators(abs_audiobook),
            instance_id=self.instance_id,
            domain=self.domain,
            token=self._client.token,
            base_url=str(self.config.get_value(CONF_URL)).rstrip("/"),
            media_progress=progress,
        )

    @handle_refresh_token
    async def _get_abs_expanded_audiobook(
        self, prov_audiobook_id: str
    ) -> AbsLibraryItemExpandedBook:
        abs_audiobook = await self._client.get_library_item_book(
            book_id=prov_audiobook_id, expanded=True
        )
        assert isinstance(abs_audiobook, AbsLibraryItemExpandedBook)

        return abs_audiobook

    async def _get_audiobook_narrators(
        self, book: AbsLibraryItemExpandedBook
    ) -> set[NarratorHelper]:
        """Get narrators of an audiobook, either from cache or API calls."""
        if cached_narrators := self.libraries.audiobook_narrators.get(book.id_):
            return cached_narrators
        await self._update_book_narrators(book.library_id)
        return self.libraries.audiobook_narrators.get(book.id_, set())

    async def _update_playlog_book(self, progress: MediaProgress) -> None:
        # helper progress also ensures no useless progress updates,
        # see comment above
        self.progress_guard.add_progress(progress.library_item_id)
        if progress.current_time is None:
            return
        mass_audiobook = await self.mass.music.get_library_item_by_prov_id(
            media_type=MediaType.AUDIOBOOK,
            item_id=progress.library_item_id,
            provider_instance_id_or_domain=self.instance_id,
        )
        if mass_audiobook is None:
            return
        if int(progress.current_time) == 0 and not progress.is_finished:
            await self.mass.music.mark_item_unplayed(mass_audiobook)
        else:
            await self.mass.music.mark_item_played(
                mass_audiobook,
                fully_played=progress.is_finished,
                seconds_played=int(progress.current_time),
            )
