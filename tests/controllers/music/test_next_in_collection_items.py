"""Tests for the next audiobook of the collections a user can continue."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from music_assistant_models.auth import User, UserRole

from music_assistant.mass import MusicAssistant
from tests.controllers.music.helpers import CollectionBookState, add_collection_audiobook

GET_CURRENT_USER = "music_assistant.controllers.music.controller.get_current_user"
USER_A = User(user_id="user-a", username="user-a", role=UserRole.USER)
USER_B = User(user_id="user-b", username="user-b", role=UserRole.USER)


@pytest.fixture(name="mass")
def mass_fixture(music_mass: MusicAssistant, monkeypatch: pytest.MonkeyPatch) -> MusicAssistant:
    """Run on a library-only instance with one music source."""
    monkeypatch.setattr(music_mass.music, "get_active_provider_instances", lambda: ["local_1"])
    return music_mass


@pytest.mark.parametrize(
    ("books", "expected"),
    [
        pytest.param([(1, "finished"), (2, "unplayed"), (3, "unplayed")], ["2"], id="next"),
        pytest.param(
            [(1, "finished"), (2, "unplayed"), (3, "finished"), (4, "unplayed")],
            ["2"],
            id="first_unfinished_not_after_last_finished",
        ),
        pytest.param([(1, "unplayed"), (2, "unplayed")], [], id="not_started"),
        pytest.param([(1, "finished"), (2, "finished")], [], id="all_finished"),
        pytest.param(
            [(1, "finished"), (2, "in_progress"), (3, "unplayed")], [], id="book_in_progress"
        ),
        pytest.param(
            [(1, "finished"), (2, "unavailable"), (3, "not_in_library"), (4, "unplayed")],
            ["4"],
            id="first_playable_in_library",
        ),
        pytest.param(
            [("10", "unplayed"), (9, "unplayed"), (1, "finished")], ["2"], id="numeric_order"
        ),
        pytest.param(
            [(None, "unplayed"), ("b", "unplayed"), (2.5, "unplayed"), (1, "finished")],
            ["3"],
            id="numbers_before_text_before_unsequenced",
        ),
        pytest.param(
            [(None, "unplayed"), ("b", "unplayed"), (1, "finished")],
            ["2"],
            id="text_before_unsequenced",
        ),
    ],
)
async def test_next_in_collection(
    mass: MusicAssistant,
    books: list[tuple[float | str | None, CollectionBookState]],
    expected: list[str],
) -> None:
    """The next audiobook is the first unfinished one of a started collection."""
    for item_id, (sequence, state) in enumerate(books, start=1):
        await add_collection_audiobook(mass, item_id, sequence, state)

    with patch(GET_CURRENT_USER, return_value=USER_A):
        result = await mass.music.next_in_collection_items()

    assert [item.item_id for item in result] == expected


async def test_next_in_collection_joins_in_progress_by_time(mass: MusicAssistant) -> None:
    """Next audiobooks sort into the in-progress items by when their collection was played."""
    await add_collection_audiobook(mass, 1, 1, "finished", collection="Old saga", played_at=100)
    await add_collection_audiobook(mass, 2, 2, collection="Old saga")
    await add_collection_audiobook(
        mass, 3, 1, "in_progress", collection="Standalone", played_at=200
    )
    await add_collection_audiobook(mass, 4, 1, "finished", collection="New saga", played_at=300)
    await add_collection_audiobook(mass, 5, 2, collection="New saga")

    with patch(GET_CURRENT_USER, return_value=USER_A):
        in_progress = await mass.music.in_progress_items()
        merged = await mass.music.in_progress_items(include_next_in_collection=True)
        own_row = await mass.music.next_in_collection_items()
        other_source = await mass.music.next_in_collection_items(providers=["audible_1"])
    # the played state of user-a is not the one of user-b
    with patch(GET_CURRENT_USER, return_value=USER_B):
        other_user = await mass.music.in_progress_items(include_next_in_collection=True)

    assert [item.item_id for item in in_progress] == ["3"]
    assert [item.item_id for item in merged] == ["5", "3", "2"]
    assert [item.item_id for item in own_row] == ["5", "2"]
    assert own_row[0].name == "Book 5"
    assert own_row[0].image is not None
    assert own_row[0].image.path == "cover_5"
    assert other_source == []
    assert other_user == []


async def test_next_in_collection_reads_collections_from_index(mass: MusicAssistant) -> None:
    """The books of all collections come from the index, not from the metadata of each book."""
    params: dict[str, object] = {}
    query = mass.music._next_in_collection_query(USER_A, None, params)

    plan = await mass.music.database.get_rows_from_query(
        f"EXPLAIN QUERY PLAN {query}", params, limit=0
    )

    details = [row["detail"] for row in plan]
    assert "SCAN a USING COVERING INDEX audiobooks_collections_idx" in details
    assert not any(detail in ("SCAN a", "SCAN m", "SCAN p") for detail in details)
