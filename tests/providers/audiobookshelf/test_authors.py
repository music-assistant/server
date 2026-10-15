"""Tests for the author socket events of the Audiobookshelf provider."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import pytest
from aioaudiobookshelf.schema.author import AuthorExpanded, AuthorMinified
from aioaudiobookshelf.schema.events_socket import AuthorRemoved
from music_assistant_models.enums import MediaType
from music_assistant_models.media_items import Audiobook, ProviderMapping

from music_assistant.providers.audiobookshelf.parsers import parse_author

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant
    from music_assistant.providers.audiobookshelf import Audiobookshelf


async def _add_book(provider: Audiobookshelf, linked: bool) -> None:
    """Add a book and its author to the library, as the book's socket update does."""
    # a book update carries the author without its image
    author = parse_author(
        abs_author=AuthorMinified(id_="author1", name="Ada Lovelace"),
        instance_id=provider.instance_id,
        domain=provider.domain,
        base_url="http://abs.local",
    )
    book = Audiobook(
        item_id="book1",
        provider=provider.instance_id,
        name="Book",
        provider_mappings={
            ProviderMapping(
                item_id="book1",
                provider_domain=provider.domain,
                provider_instance=provider.instance_id,
                in_library=True,
            )
        },
    )
    if linked:
        book.authors.set([author])
    else:
        await provider.mass.music.artists.add_item_to_library(author)
    await provider.mass.music.audiobooks.add_item_to_library(book)


@pytest.mark.parametrize("author_id", ["author1", "author2"])
async def test_author_updated(
    provider: Audiobookshelf, music_mass: MusicAssistant, author_id: str
) -> None:
    """An updated author adds its image to the library artist, an unknown one is not added."""
    provider.mass = music_mass
    await _add_book(provider, linked=True)
    abs_author = AuthorExpanded(
        id_=author_id,
        name="Ada Lovelace",
        image_path=f"/metadata/authors/{author_id}.jpg",
        added_at=1000,
        updated_at=2000,
        num_books=1,
    )

    with patch.object(provider, "get_setup_value", return_value="http://abs.local"):
        await provider._socket_abs_author_updated(abs_author)

    artist = await music_mass.music.get_library_item_by_prov_id(
        MediaType.ARTIST, "author1", provider.instance_id
    )
    assert artist is not None
    assert (
        await music_mass.music.get_library_item_by_prov_id(
            MediaType.ARTIST, "author2", provider.instance_id
        )
        is None
    )
    expected = ["http://abs.local/api/authors/author1/image"] if author_id == "author1" else []
    assert [x.path for x in artist.metadata.images or []] == expected


@pytest.mark.parametrize("linked", [True, False])
async def test_author_removed(
    provider: Audiobookshelf, music_mass: MusicAssistant, linked: bool
) -> None:
    """A removed author still linked to a book keeps the book, an unlinked one is removed."""
    provider.mass = music_mass
    music_mass.streams = AsyncMock()
    await _add_book(provider, linked)

    await provider._socket_abs_author_removed(AuthorRemoved(id_="author1", library_id="lib1"))

    assert len(await music_mass.music.audiobooks.library_items()) == 1
    artist = await music_mass.music.get_library_item_by_prov_id(
        MediaType.ARTIST, "author1", provider.instance_id
    )
    assert (artist is not None) is linked
