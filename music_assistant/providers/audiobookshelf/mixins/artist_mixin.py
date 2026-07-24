"""ArtistsMixin for Audiobookshelf."""

from __future__ import annotations

from collections.abc import AsyncGenerator, Sequence
from typing import TYPE_CHECKING, cast

from aioaudiobookshelf.schema.calls_authors import (
    AuthorWithItems as AbsAuthorWithItems,
)
from aioaudiobookshelf.schema.library import LibraryMediaType as AbsLibraryMediaType
from music_assistant_models.enums import MediaType

from music_assistant.providers.audiobookshelf.constants import CONF_URL
from music_assistant.providers.audiobookshelf.helpers import NarratorHelper, handle_refresh_token
from music_assistant.providers.audiobookshelf.mixins.mixin_base import AbsMixinBase
from music_assistant.providers.audiobookshelf.parsers import (
    parse_author,
    parse_narrator,
)

if TYPE_CHECKING:
    from music_assistant_models.media_items import (
        Artist,
        Audiobook,
    )


class AbsArtistsMixin(AbsMixinBase):
    """ArtistsMixin for Audiobookshelf."""

    if TYPE_CHECKING:
        # part of browse Mixin
        async def _browse_narrator_books(
            self, library_id: str, narrator_filter_str: str
        ) -> Sequence[Audiobook]: ...

    async def get_library_artists(self) -> AsyncGenerator[Artist]:
        """Get authors and narrators."""
        libraries = await self._client.get_all_libraries()
        library_ids_audiobook: set[str] = set()
        for library in libraries:
            if library.media_type == AbsLibraryMediaType.BOOK:
                library_ids_audiobook.add(library.id_)
        for book_lib_id in library_ids_audiobook:
            for abs_author in await self._client.get_library_authors(library_id=book_lib_id):
                self.libraries.authors[book_lib_id].add(abs_author.id_)
                yield parse_author(
                    abs_author=abs_author,
                    instance_id=self.instance_id,
                    domain=self.domain,
                    token=self._client.token,
                    base_url=str(self.config.get_value(CONF_URL)).rstrip("/"),
                )
            for abs_narrator in await self._client.get_library_narrators(library_id=book_lib_id):
                self.libraries.narrators[book_lib_id].add(abs_narrator.id_)
                yield parse_narrator(
                    abs_narrator=abs_narrator, instance_id=self.instance_id, domain=self.domain
                )

    @handle_refresh_token
    async def get_author_audiobooks(self, prov_artist_id: str) -> list[Audiobook]:
        """Get audiobooks of author."""
        abs_author = await self._client.get_author(
            author_id=prov_artist_id, include_items=True, include_series=False
        )
        if not isinstance(abs_author, AbsAuthorWithItems):
            raise TypeError("Unexpected type of author.")

        book_ids = {x.id_ for x in abs_author.library_items}
        audiobooks: list[Audiobook] = []
        for book_id in book_ids:
            mass_item = await self.mass.music.get_library_item_by_prov_id(
                media_type=MediaType.AUDIOBOOK,
                item_id=book_id,
                provider_instance_id_or_domain=self.instance_id,
            )
            if mass_item is not None:
                mass_item = cast("Audiobook", mass_item)
                audiobooks.append(mass_item)
        return audiobooks

    @handle_refresh_token
    async def get_narrator_audiobooks(self, prov_artist_id: str) -> list[Audiobook]:
        """Get audiobooks of narrator."""
        narrator_lib_id: str = ""
        # get library of artist:
        for lib_id, narrator_ids in self.libraries.narrators.items():
            if prov_artist_id in narrator_ids:
                narrator_lib_id = lib_id
                break
        if narrator_lib_id:
            return list(
                await self._browse_narrator_books(
                    library_id=narrator_lib_id, narrator_filter_str=prov_artist_id
                )
            )
        return []

    @handle_refresh_token
    async def get_artist(self, prov_artist_id: str) -> Artist:
        """Get an author or narrator."""
        for library_id, narrator_ids in self.libraries.narrators.items():
            if prov_artist_id in narrator_ids:
                for abs_narrator in await self._client.get_library_narrators(library_id=library_id):
                    if abs_narrator.id_ == prov_artist_id:
                        return parse_narrator(
                            abs_narrator=abs_narrator,
                            instance_id=self.instance_id,
                            domain=self.domain,
                        )

        return parse_author(
            abs_author=await self._client.get_author(author_id=prov_artist_id),
            instance_id=self.instance_id,
            domain=self.domain,
            token=self._client.token,
            base_url=str(self.config.get_value(CONF_URL)).rstrip("/"),
        )

    @handle_refresh_token
    async def _update_book_narrators(self, library_id: str) -> None:
        # narrators are not expanded in ABS' response, so acquire them here
        narrators = await self._client.get_library_narrators(library_id=library_id)
        audiobook_narrators: dict[str, set[NarratorHelper]] = {}
        for narrator in narrators:
            async for response in self._client.get_library_items(
                library_id=library_id, filter_str=f"narrators.{narrator.id_}"
            ):
                if not response.results:
                    break
                for item in response.results:
                    narrator_set = audiobook_narrators.get(item.id_, set())
                    narrator_set.add(NarratorHelper(id_=narrator.id_, name=narrator.name))
                    audiobook_narrators[item.id_] = narrator_set
        self.libraries.audiobook_narrators = {
            **self.libraries.audiobook_narrators,
            **audiobook_narrators,
        }
