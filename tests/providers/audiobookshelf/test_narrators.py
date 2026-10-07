"""Tests for the narrator lookup of the Audiobookshelf provider."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from music_assistant.providers.audiobookshelf import Audiobookshelf
from music_assistant.providers.audiobookshelf.helpers import NarratorHelper


def _book(*narrators: str) -> Mock:
    """Create a book of library lib1 which names the given narrators."""
    book = Mock()
    book.library_id = "lib1"
    book.media.metadata.narrators = list(narrators)
    return book


def _stub_library_narrators(provider: Audiobookshelf, name_to_id: dict[str, str]) -> AsyncMock:
    """Serve the library's narrators from a dict the test may extend afterwards."""

    async def _get_library_narrators(**_kwargs: str) -> list[SimpleNamespace]:
        return [SimpleNamespace(id_=id_, name=name) for name, id_ in name_to_id.items()]

    mock = AsyncMock(side_effect=_get_library_narrators)
    provider._client.get_library_narrators = mock  # type: ignore[method-assign]
    return mock


@pytest.mark.asyncio
async def test_book_without_narrators_makes_no_lookup(provider: Audiobookshelf) -> None:
    """A book naming no narrator never asks the server for the library's narrators."""
    lookup = _stub_library_narrators(provider, {"Ada Lovelace": "nar_ada"})

    assert await provider._get_audiobook_narrators(_book()) == set()
    lookup.assert_not_awaited()


@pytest.mark.asyncio
async def test_narrated_books_share_one_lookup(provider: Audiobookshelf) -> None:
    """Every book with known narrators reuses the ids fetched for the first one."""
    lookup = _stub_library_narrators(
        provider, {"Ada Lovelace": "nar_ada", "Alan Turing": "nar_alan"}
    )

    first = await provider._get_audiobook_narrators(_book("Ada Lovelace"))
    second = await provider._get_audiobook_narrators(_book("Alan Turing", "Ada Lovelace"))

    assert first == {NarratorHelper(id_="nar_ada", name="Ada Lovelace")}
    assert second == {
        NarratorHelper(id_="nar_ada", name="Ada Lovelace"),
        NarratorHelper(id_="nar_alan", name="Alan Turing"),
    }
    assert lookup.await_count == 1


@pytest.mark.asyncio
async def test_unknown_narrator_refreshes_the_ids(provider: Audiobookshelf) -> None:
    """A narrator added to the library since the last lookup, as a socket update brings."""
    narrators = {"Ada Lovelace": "nar_ada"}
    lookup = _stub_library_narrators(provider, narrators)
    await provider._get_audiobook_narrators(_book("Ada Lovelace"))

    narrators["Grace Hopper"] = "nar_grace"
    refreshed = await provider._get_audiobook_narrators(_book("Grace Hopper"))

    assert refreshed == {NarratorHelper(id_="nar_grace", name="Grace Hopper")}
    assert lookup.await_count == 2
