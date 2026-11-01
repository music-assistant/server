"""Tests for MusicController.in_progress_items playlog queries."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from music_assistant_models.auth import User, UserRole
from music_assistant_models.config_entries import ProviderAccess
from music_assistant_models.enums import MediaType, ProviderSharing
from music_assistant_models.media_items import (
    Audiobook,
    MediaItemCollection,
    MediaItemMetadata,
    ProviderMapping,
    UniqueList,
)

from music_assistant.constants import (
    DB_TABLE_AUDIOBOOK_COLLECTIONS,
    DB_TABLE_MEDIA_PROGRESS,
    DB_TABLE_PROVIDER_MAPPINGS,
)
from music_assistant.helpers.datetime import utc_timestamp
from music_assistant.mass import MusicAssistant
from tests.common import set_music_source_access

GET_CURRENT_USER = "music_assistant.controllers.music.controller.get_current_user"
USER_A = User(user_id="user-a", username="user-a", role=UserRole.USER)
# the provider sequences as reported, "10" sorts before "2" when compared as text
SAGA: dict[str, int | str] = {"book_1": 1, "book_2": "2", "book_3": "10"}


@pytest.fixture(name="mass")
def mass_fixture(music_mass: MusicAssistant) -> MusicAssistant:
    """Run on a library-only instance: these tests only touch the library."""
    return music_mass


async def _add_in_progress_row(
    mass: MusicAssistant,
    item_id: str,
    *,
    provider: str = "library",
    media_type: MediaType = MediaType.AUDIOBOOK,
    userid: str = "user-a",
    fully_played: bool = False,
) -> None:
    """Insert a single in-progress (partially played) or finished playlog row."""
    await mass.music.database.insert(
        DB_TABLE_MEDIA_PROGRESS,
        {
            "item_id": item_id,
            "provider": provider,
            "media_type": media_type.value,
            "name": f"Item {item_id}",
            "timestamp": int(utc_timestamp()),
            "fully_played": fully_played,
            "seconds_played": 60,
            "userid": userid,
            "user_initiated": True,
        },
    )


async def _add_provider_mapping(
    mass: MusicAssistant,
    item_id: int,
    provider_instance: str,
    media_type: MediaType = MediaType.AUDIOBOOK,
    *,
    available: bool = True,
) -> None:
    """Insert a provider mapping row for a library item."""
    await mass.music.database.insert(
        DB_TABLE_PROVIDER_MAPPINGS,
        {
            "media_type": media_type.value,
            "item_id": item_id,
            "provider_domain": provider_instance.rstrip("0123456789_"),
            "provider_instance": provider_instance,
            "provider_item_id": f"{provider_instance}-{item_id}",
            "available": available,
            "in_library": True,
        },
    )


async def test_in_progress_items_omitted_providers_unchanged(
    mass: MusicAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Existing callers (no providers filter) keep seeing every in-progress entry."""
    monkeypatch.setattr(mass.music, "get_active_provider_instances", lambda: ["local_1"])
    await _add_provider_mapping(mass, item_id=1, provider_instance="local_1")
    await _add_in_progress_row(mass, "1", provider="library")

    result = await mass.music.in_progress_items(limit=10)

    assert {item.item_id for item in result} == {"1"}


async def test_in_progress_items_filters_direct_provider_entries(
    mass: MusicAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Direct (non-library) playlog entries are matched against their own provider."""
    monkeypatch.setattr(
        mass.music, "get_active_provider_instances", lambda: ["audible_1", "local_1"]
    )
    await _add_in_progress_row(mass, "audible-book", provider="audible_1")
    await _add_in_progress_row(mass, "local-book", provider="local_1")

    result = await mass.music.in_progress_items(limit=10, providers=["local_1"])

    assert {item.item_id for item in result} == {"local-book"}


async def test_in_progress_items_library_item_or_matches_any_mapping(
    mass: MusicAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A library item mapped to two providers remains when only one is requested."""
    monkeypatch.setattr(
        mass.music, "get_active_provider_instances", lambda: ["audible_1", "local_1"]
    )
    await _add_provider_mapping(mass, item_id=1, provider_instance="audible_1")
    await _add_provider_mapping(mass, item_id=1, provider_instance="local_1")
    await _add_in_progress_row(mass, "1", provider="library")

    result = await mass.music.in_progress_items(limit=10, providers=["local_1"])

    assert {item.item_id for item in result} == {"1"}


async def test_in_progress_items_explicit_empty_providers_returns_no_items(
    mass: MusicAssistant,
) -> None:
    """An explicit empty provider list returns no items, distinct from omitting the filter."""
    await _add_in_progress_row(mass, "book-1")

    result = await mass.music.in_progress_items(limit=10, providers=[])

    assert result == []


async def test_in_progress_items_combines_explicit_and_user_music_sources(
    mass: MusicAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A user's music sources narrow an explicit filter that would otherwise pass."""
    monkeypatch.setattr(
        mass.music, "get_active_provider_instances", lambda: ["audible_1", "local_1"]
    )
    # audible_1 is another member's private source, so user-a may not use it
    set_music_source_access(
        mass,
        {
            "local_1": ProviderAccess(owner="user-a", sharing=ProviderSharing.PRIVATE),
            "audible_1": ProviderAccess(owner="user-b", sharing=ProviderSharing.PRIVATE),
        },
    )
    user_a = User(user_id="user-a", username="user-a", role=UserRole.USER)
    await _add_provider_mapping(mass, item_id=1, provider_instance="audible_1")
    await _add_provider_mapping(mass, item_id=1, provider_instance="local_1")
    await _add_in_progress_row(mass, "1", provider="library")

    with patch(GET_CURRENT_USER, return_value=user_a):
        result = await mass.music.in_progress_items(limit=10, providers=["local_1"])
    assert {item.item_id for item in result} == {"1"}

    # requesting a music source the user may not use must not leak the item back
    # in, even though the item does have a (restricted) mapping to it.
    with patch(GET_CURRENT_USER, return_value=user_a):
        result = await mass.music.in_progress_items(limit=10, providers=["audible_1"])
    assert result == []


async def test_in_progress_items_excludes_unavailable_mapping(
    mass: MusicAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A mapping that is no longer available on the requested provider cannot match."""
    monkeypatch.setattr(
        mass.music, "get_active_provider_instances", lambda: ["local_1", "audible_1"]
    )
    await _add_provider_mapping(mass, item_id=1, provider_instance="local_1")
    await _add_provider_mapping(mass, item_id=1, provider_instance="audible_1", available=False)
    await _add_in_progress_row(mass, "1", provider="library")

    result = await mass.music.in_progress_items(limit=10, providers=["audible_1"])

    assert result == []


def _book(name: str, collections: dict[str, int | str]) -> Audiobook:
    """Return an audiobook as a provider reports it, part of the given collections."""
    return Audiobook(
        item_id=name,
        provider="local_1",
        name=name,
        provider_mappings={
            ProviderMapping(item_id=name, provider_domain="local", provider_instance="local_1")
        },
        metadata=MediaItemMetadata(
            collections=UniqueList(
                MediaItemCollection(title=title, sequence=sequence)
                for title, sequence in collections.items()
            )
        ),
    )


async def _add_saga(mass: MusicAssistant, monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """Add the saga books to the library, return their library ids by name."""
    monkeypatch.setattr(mass.music, "get_active_provider_instances", lambda: ["local_1"])
    return {
        name: (await mass.music.audiobooks.add_item_to_library(_book(name, {"Saga": seq}))).item_id
        for name, seq in SAGA.items()
    }


async def _collections(mass: MusicAssistant) -> set[tuple[int, str, int | str]]:
    rows = await mass.music.database.get_rows(DB_TABLE_AUDIOBOOK_COLLECTIONS, limit=0)
    return {(row["audiobook_id"], row["title"], row["sequence"]) for row in rows}


@pytest.mark.parametrize(
    ("finished", "partial", "include_next", "expected"),
    [
        (["book_1"], [], True, ["book_2"]),
        (["book_2"], [], True, ["book_3"]),
        (["book_1", "book_3"], [], True, []),
        (["book_1"], ["book_2"], True, ["book_2"]),
        ([], [], True, []),
        (["book_1"], [], False, []),
    ],
)
async def test_in_progress_items_next_in_collection(
    mass: MusicAssistant,
    monkeypatch: pytest.MonkeyPatch,
    finished: list[str],
    partial: list[str],
    include_next: bool,
    expected: list[str],
) -> None:
    """The book after the highest finished one joins the in-progress books, at most once."""
    ids = await _add_saga(mass, monkeypatch)
    for name in finished:
        await _add_in_progress_row(mass, ids[name], fully_played=True)
    for name in partial:
        await _add_in_progress_row(mass, ids[name])

    with patch(GET_CURRENT_USER, return_value=USER_A):
        result = await mass.music.in_progress_items(
            limit=10, include_next_in_collection=include_next
        )

    assert [item.item_id for item in result] == [ids[name] for name in expected]


@pytest.mark.parametrize(("partial", "expected"), [([], ["book_2"]), (["book_2"], [])])
async def test_next_in_collection_items_skips_collections_in_progress(
    mass: MusicAssistant,
    monkeypatch: pytest.MonkeyPatch,
    partial: list[str],
    expected: list[str],
) -> None:
    """A collection with a book in progress is left to the in-progress row."""
    ids = await _add_saga(mass, monkeypatch)
    await _add_in_progress_row(mass, ids["book_1"], fully_played=True)
    for name in partial:
        await _add_in_progress_row(mass, ids[name])

    with patch(GET_CURRENT_USER, return_value=USER_A):
        result = await mass.music.next_in_collection_items(limit=10)

    assert [item.item_id for item in result] == [ids[name] for name in expected]


@pytest.mark.parametrize("excluded_by", ["other_user", "unavailable", "no_user"])
async def test_next_in_collection_items_excluded(
    mass: MusicAssistant, monkeypatch: pytest.MonkeyPatch, excluded_by: str
) -> None:
    """Another user's progress, an unavailable next book or no session user show nothing."""
    ids = await _add_saga(mass, monkeypatch)
    await _add_in_progress_row(
        mass,
        ids["book_1"],
        fully_played=True,
        userid="user-b" if excluded_by == "other_user" else "user-a",
    )
    if excluded_by == "unavailable":
        await mass.music.database.update(
            DB_TABLE_PROVIDER_MAPPINGS,
            {"media_type": MediaType.AUDIOBOOK.value, "item_id": int(ids["book_2"])},
            {"available": False},
        )

    with patch(GET_CURRENT_USER, return_value=None if excluded_by == "no_user" else USER_A):
        result = await mass.music.next_in_collection_items(limit=10)

    assert result == []


async def test_audiobook_collections_follow_the_library_item(
    mass: MusicAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The collections table is rewritten on update and cleared on removal."""
    ids = await _add_saga(mass, monkeypatch)
    book_1 = int(ids["book_1"])
    assert (book_1, "Saga", 1) in await _collections(mass)
    # the text sequence is stored as a number
    assert (int(ids["book_2"]), "Saga", 2) in await _collections(mass)

    await mass.music.audiobooks.update_item_in_library(
        book_1, _book("book_1", {"Other Saga": "1.5"})
    )
    assert {x for x in await _collections(mass) if x[0] == book_1} == {(book_1, "Other Saga", 1.5)}

    # remove_item_from_library routes audio analysis cleanup through the AA controller
    streams = MagicMock()
    streams.audio_analysis.delete_audio_analysis = AsyncMock()
    monkeypatch.setattr(mass, "streams", streams, raising=False)
    await mass.music.audiobooks.remove_item_from_library(book_1)
    assert {x for x in await _collections(mass) if x[0] == book_1} == set()


async def test_next_in_collection_items_after_merge(
    mass: MusicAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A book merged into its duplicate takes its collection rows along."""
    ids = await _add_saga(mass, monkeypatch)
    duplicate = await mass.music.audiobooks.add_item_to_library(_book("book_2_copy", {"Saga": "2"}))
    await _add_in_progress_row(mass, ids["book_1"], fully_played=True)

    await mass.music.audiobooks.merge_library_items(duplicate.item_id, ids["book_2"])
    with patch(GET_CURRENT_USER, return_value=USER_A):
        result = await mass.music.next_in_collection_items(limit=10)

    assert [item.item_id for item in result] == [duplicate.item_id]
