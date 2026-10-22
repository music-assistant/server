"""Tests for author audiobook listings when one of the author's providers fails."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Literal
from unittest.mock import AsyncMock, patch

import pytest
from music_assistant_models.auth import User, UserRole
from music_assistant_models.config_entries import ProviderAccess
from music_assistant_models.enums import ArtistType, ProviderSharing
from music_assistant_models.errors import MediaNotFoundError
from music_assistant_models.helpers import set_global_cache_values
from music_assistant_models.media_items import (
    Artist,
    Audiobook,
    MediaCollection,
    MediaItemCollection,
    MediaItemMetadata,
    ProviderMapping,
    UniqueList,
)

from music_assistant.mass import MusicAssistant
from tests.common import set_music_source_access

pytestmark = pytest.mark.asyncio

_PROVIDERS = ["local_inst", "streaming_inst"]


def _mapping(provider_instance: str, item_id: str, in_library: bool = True) -> ProviderMapping:
    return ProviderMapping(
        item_id=item_id,
        provider_domain=provider_instance.removesuffix("_inst"),
        provider_instance=provider_instance,
        in_library=in_library,
    )


async def _seed_author(
    mass: MusicAssistant,
    *,
    library_audiobooks: tuple[str, ...] = (),
    collection_name: str | None = None,
) -> Artist:
    """
    Seed a library author mapped to a local provider and a non-library streaming provider.

    :param library_audiobooks: Names of in-library audiobooks (on the local provider) to link.
    :param collection_name: When given, the library audiobooks are placed in this collection.
    """
    author = Artist(
        item_id="0",
        provider="library",
        name="Test Author",
        artist_type=ArtistType.AUTHOR,
        provider_mappings={
            _mapping("local_inst", "author_local"),
            _mapping("streaming_inst", "author_streaming", in_library=False),
        },
    )
    db_author = await mass.music.artists.add_item_to_library(author)
    for sequence, name in enumerate(library_audiobooks, start=1):
        metadata = MediaItemMetadata()
        if collection_name:
            metadata.collections = UniqueList(
                [MediaItemCollection(title=collection_name, sequence=float(sequence))]
            )
        await mass.music.audiobooks.add_item_to_library(
            Audiobook(
                item_id="0",
                provider="library",
                name=name,
                provider_mappings={_mapping("local_inst", f"book_local_{sequence}")},
                authors=UniqueList([db_author]),
                metadata=metadata,
            )
        )
    return db_author


def _failing_provider_fetch(
    error: Exception,
) -> Callable[[str, str], Awaitable[list[Audiobook]]]:
    """Return a fake provider audiobook fetch that fails for the streaming provider only."""

    async def _fetch(_item_id: str, provider_instance_id_or_domain: str) -> list[Audiobook]:
        if provider_instance_id_or_domain == "streaming_inst":
            raise error
        return []

    return _fetch


async def test_author_audiobooks_filter_source_before_paging(mass: MusicAssistant) -> None:
    """A selected provider excludes other library books and provider requests before paging."""
    db_author = await _seed_author(mass, library_audiobooks=("Other Book",))
    books = [
        Audiobook(
            item_id=f"book_{index}",
            provider="streaming_inst",
            name=f"Selected Book {index}",
            provider_mappings={_mapping("streaming_inst", f"book_{index}")},
            authors=UniqueList([db_author]),
        )
        for index in range(2)
    ]
    fetch = AsyncMock(return_value=books)
    with (
        patch.object(mass.music, "get_unique_providers", return_value=_PROVIDERS),
        patch.object(mass.music.artists, "get_provider_author_audiobooks", fetch),
    ):
        items = await mass.music.artists.audiobooks(
            "author_streaming",
            "streaming_inst",
            provider_filter="streaming_inst",
            limit=1,
            offset=1,
        )

    assert [item.name for item in items] == ["Selected Book 1"]
    fetch.assert_awaited_once_with("author_streaming", "streaming_inst")


@pytest.mark.parametrize("collapse_collections", [False, True])
async def test_author_library_audiobooks_filter_source(
    mass: MusicAssistant, collapse_collections: Literal[False, True]
) -> None:
    """The source filter also excludes other providers' books and collapsed collections."""
    db_author = await _seed_author(
        mass, library_audiobooks=("Other Book",), collection_name="Other Series"
    )
    items = await mass.music.artists.audiobooks(
        db_author.item_id,
        "library",
        in_library_only=True,
        provider_filter="streaming_inst",
        collapse_collections=collapse_collections,
    )
    assert items == []

    items = await mass.music.artists.audiobooks(
        db_author.item_id,
        "library",
        in_library_only=True,
        provider_filter="local_inst",
        collapse_collections=collapse_collections,
    )
    assert [item.name for item in items] == (
        ["Other Series"] if collapse_collections else ["Other Book"]
    )


async def test_author_library_audiobooks_follow_the_users_music_sources(
    mass: MusicAssistant,
) -> None:
    """A restricted user does not see the author's library audiobooks on another user's source."""
    db_author = await _seed_author(mass, library_audiobooks=("Local Book",))
    await mass.music.audiobooks.add_item_to_library(
        Audiobook(
            item_id="0",
            provider="library",
            name="Streaming Book",
            provider_mappings={_mapping("streaming_inst", "book_streaming")},
            authors=UniqueList([db_author]),
        )
    )
    set_music_source_access(
        mass,
        {
            "local_inst": ProviderAccess(owner="user-a", sharing=ProviderSharing.PRIVATE),
            "streaming_inst": ProviderAccess(owner="user-b", sharing=ProviderSharing.PRIVATE),
        },
    )

    all_books = await mass.music.artists.audiobooks(
        db_author.item_id, "library", in_library_only=True
    )
    assert {item.name for item in all_books} == {"Local Book", "Streaming Book"}

    with patch(
        "music_assistant.controllers.music.media.base.get_current_user",
        return_value=User(user_id="user-a", username="user-a", role=UserRole.USER),
    ):
        own_books = await mass.music.artists.audiobooks(
            db_author.item_id, "library", in_library_only=True
        )
    assert [item.name for item in own_books] == ["Local Book"]


async def test_author_audiobooks_skip_failing_provider(
    mass: MusicAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """A failing secondary provider is skipped (and logged) so the library audiobooks still list."""
    db_author = await _seed_author(mass, library_audiobooks=("Local Book",))
    # both providers are loaded and available; the streaming one merely errors on the fetch
    await set_global_cache_values({"available_providers": set(_PROVIDERS)})
    with (
        patch.object(mass.music, "get_unique_providers", return_value=_PROVIDERS),
        patch.object(
            mass.music.artists,
            "get_provider_author_audiobooks",
            side_effect=_failing_provider_fetch(MediaNotFoundError("provider failed")),
        ),
    ):
        audiobooks = await mass.music.artists.audiobooks(db_author.item_id, "library")
    assert [item.name for item in audiobooks] == ["Local Book"]
    assert "Unable to fetch audiobooks for Test Author from provider streaming_inst" in caplog.text


async def test_author_audiobooks_keep_collapsed_collection_when_provider_fails(
    mass: MusicAssistant,
) -> None:
    """
    A collapsed collection of playable library books is kept when a secondary provider fails.

    A collapsed collection has no provider mappings of its own, so the "nothing playable" check
    must look at the books inside it rather than at the collection item.
    """
    db_author = await _seed_author(
        mass, library_audiobooks=("Book 1", "Book 2"), collection_name="Test Series"
    )
    await set_global_cache_values({"available_providers": set(_PROVIDERS)})
    with (
        patch.object(mass.music, "get_unique_providers", return_value=_PROVIDERS),
        patch.object(
            mass.music.artists,
            "get_provider_author_audiobooks",
            side_effect=_failing_provider_fetch(MediaNotFoundError("provider failed")),
        ),
    ):
        audiobooks = await mass.music.artists.audiobooks(
            db_author.item_id, "library", collapse_collections=True
        )
    assert len(audiobooks) == 1
    collection = audiobooks[0]
    assert isinstance(collection, MediaCollection)
    assert collection.name == "Test Series"
    assert [book.name for book in collection.items] == ["Book 1", "Book 2"]


async def test_author_audiobooks_raise_when_nothing_playable(mass: MusicAssistant) -> None:
    """Pin that the guard does not over-suppress: with nothing playable, the error still surfaces."""
    db_author = await _seed_author(mass)
    with (
        patch.object(mass.music, "get_unique_providers", return_value=_PROVIDERS),
        patch.object(
            mass.music.artists,
            "get_provider_author_audiobooks",
            side_effect=_failing_provider_fetch(MediaNotFoundError("provider failed")),
        ),
        pytest.raises(MediaNotFoundError),
    ):
        await mass.music.artists.audiobooks(db_author.item_id, "library")


async def test_author_audiobooks_raise_when_library_items_unavailable(
    mass: MusicAssistant,
) -> None:
    """Library audiobooks that are all unavailable do not count as playable: the error surfaces."""
    db_author = await _seed_author(mass, library_audiobooks=("Local Book",))
    # only the (failing) streaming provider is available, so the library audiobook is unplayable
    await set_global_cache_values({"available_providers": {"streaming_inst"}})
    with (
        patch.object(mass.music, "get_unique_providers", return_value=_PROVIDERS),
        patch.object(
            mass.music.artists,
            "get_provider_author_audiobooks",
            side_effect=_failing_provider_fetch(MediaNotFoundError("provider failed")),
        ),
        pytest.raises(MediaNotFoundError),
    ):
        await mass.music.artists.audiobooks(db_author.item_id, "library")


async def test_author_audiobooks_raise_when_collapsed_collection_unavailable(
    mass: MusicAssistant,
) -> None:
    """A collapsed collection whose books are all unavailable does not count as playable."""
    db_author = await _seed_author(
        mass, library_audiobooks=("Book 1", "Book 2"), collection_name="Test Series"
    )
    await set_global_cache_values({"available_providers": {"streaming_inst"}})
    with (
        patch.object(mass.music, "get_unique_providers", return_value=_PROVIDERS),
        patch.object(
            mass.music.artists,
            "get_provider_author_audiobooks",
            side_effect=_failing_provider_fetch(MediaNotFoundError("provider failed")),
        ),
        pytest.raises(MediaNotFoundError),
    ):
        await mass.music.artists.audiobooks(db_author.item_id, "library", collapse_collections=True)
