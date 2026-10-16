"""Tests for the author socket events of the Audiobookshelf provider."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, Mock, patch

import pytest
from aioaudiobookshelf.schema.author import AuthorExpanded, AuthorMinified
from aioaudiobookshelf.schema.events_socket import AuthorRemoved
from music_assistant_models.enums import MediaType, ProviderType
from music_assistant_models.media_items import Audiobook, ProviderMapping

from music_assistant.providers.audiobookshelf.parsers import parse_author

from .abs_items import expanded_book

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from music_assistant_models.media_items import Artist

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


@pytest.mark.parametrize(("linked", "missed_link"), [(True, False), (False, False), (True, True)])
async def test_author_removed(
    provider: Audiobookshelf, music_mass: MusicAssistant, linked: bool, missed_link: bool
) -> None:
    """
    A removed author still linked to a book keeps the book, an unlinked one is removed.

    A missed link is one a running sync adds after the handler checked for linked books.
    """
    provider.mass = music_mass
    music_mass.streams = AsyncMock()
    await _add_book(provider, linked)

    lookup = music_mass.music.artists.get_library_author_narrator_audiobooks
    with patch.object(
        music_mass.music.artists,
        "get_library_author_narrator_audiobooks",
        AsyncMock(return_value=[]) if missed_link else lookup,
    ):
        await provider._socket_abs_author_removed(AuthorRemoved(id_="author1", library_id="lib1"))

    assert len(await music_mass.music.audiobooks.library_items()) == 1
    artist = await music_mass.music.get_library_item_by_prov_id(
        MediaType.ARTIST, "author1", provider.instance_id
    )
    assert (artist is not None) is linked


async def test_author_removed_during_sync(
    provider: Audiobookshelf, music_mass: MusicAssistant
) -> None:
    """A removal arriving during a sync waits for it, so the sync's older copy stays removed."""
    provider.mass = music_mass
    music_mass.streams = AsyncMock()
    music_mass.cache = AsyncMock()
    music_mass.cache.get.return_value = None
    provider.manifest.type = ProviderType.MUSIC
    await _add_book(provider, linked=False)
    listing = asyncio.Event()
    removal_sent = asyncio.Event()

    async def library_artists() -> AsyncGenerator[Artist]:
        listing.set()
        await removal_sent.wait()
        yield parse_author(
            abs_author=AuthorMinified(id_="author1", name="Ada Lovelace"),
            instance_id=provider.instance_id,
            domain=provider.domain,
            base_url="http://abs.local",
        )

    with (
        patch.object(provider._client, "get_all_libraries", AsyncMock(return_value=[])),
        patch.object(provider._client, "get_my_user", AsyncMock()),
        patch.object(provider, "get_library_artists", library_artists),
        patch.object(provider, "_set_playlog_from_user", AsyncMock()),
    ):
        sync = asyncio.create_task(provider.sync_library(MediaType.ARTIST))
        await listing.wait()
        removal = asyncio.create_task(
            provider._socket_abs_author_removed(AuthorRemoved(id_="author1", library_id="lib1"))
        )
        # without the lock the removal completes here, before the sync lists the author
        await asyncio.wait({removal}, timeout=0.5)
        removal_sent.set()
        await asyncio.gather(sync, removal)

    assert (
        await music_mass.music.get_library_item_by_prov_id(
            MediaType.ARTIST, "author1", provider.instance_id
        )
        is None
    )


async def test_author_merged(provider: Audiobookshelf, music_mass: MusicAssistant) -> None:
    """A merge relinks the book before the removal of the merged author arrives."""
    provider.mass = music_mass
    music_mass.streams = AsyncMock()
    music_mass.cache = AsyncMock()
    await _add_book(provider, linked=True)
    book = expanded_book(authors=[{"id": "author2", "name": "Grace Hopper"}])
    book.media.tracks = [Mock()]

    with patch.object(provider, "get_setup_value", return_value="http://abs.local"):
        # abs sends both right after another, the socket runs them as concurrent tasks
        await asyncio.gather(
            provider._socket_abs_item_changed(book),
            provider._socket_abs_author_removed(AuthorRemoved(id_="author1", library_id="lib1")),
        )

    [library_book] = await music_mass.music.audiobooks.library_items()
    assert [x.name for x in library_book.authors if not isinstance(x, str)] == ["Grace Hopper"]
    assert (
        await music_mass.music.get_library_item_by_prov_id(
            MediaType.ARTIST, "author1", provider.instance_id
        )
        is None
    )
