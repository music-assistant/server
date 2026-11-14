"""Tests for Ynison logical queue ordering."""

import random
from copy import deepcopy
from types import SimpleNamespace
from typing import Any

import pytest
from music_assistant_models.enums import RepeatMode

from music_assistant.providers.yandex_ynison.queue_model import (
    QUEUE_WINDOW,
    YnisonQueueView,
    plan_navigation,
    radio_items,
    repeat_from_ynison,
    replace_items,
    set_index,
    set_repeat,
    set_shuffle,
)


def test_shuffle_mapping_controls_logical_navigation() -> None:
    """Next and previous follow the published shuffle permutation."""
    queue = {
        "current_playable_index": 2,
        "playable_list": [
            {"playable_id": "A"},
            {"playable_id": "B"},
            {"playable_id": "C"},
            {"playable_id": "D"},
        ],
        "shuffle_optional": {"playable_indices": [0, 2, 1, 3]},
    }

    view = YnisonQueueView(queue)

    assert view.current_index == 2
    assert view.next_index() == 1
    assert view.previous_index() == 0


def test_invalid_shuffle_mapping_falls_back_to_original_order() -> None:
    """A malformed permutation cannot produce missing or duplicate playables."""
    queue = {
        "current_playable_index": 1,
        "playable_list": [
            {"playable_id": "A"},
            {"playable_id": "B"},
            {"playable_id": "C"},
        ],
        "shuffle_optional": {"playable_indices": [0, 0, 8]},
    }

    view = YnisonQueueView(queue)

    assert view.order == (0, 1, 2)
    assert view.next_index() == 2
    assert view.shuffle_enabled is False


def test_valid_shuffle_mapping_reports_enabled() -> None:
    """Option reporting uses the same validation as logical navigation."""
    queue = {
        "current_playable_index": 1,
        "playable_list": [{"playable_id": "A"}, {"playable_id": "B"}],
        "shuffle_optional": {"playable_indices": [1, 0]},
    }

    assert YnisonQueueView(queue).shuffle_enabled is True


def test_wrap_is_explicit() -> None:
    """Logical navigation wraps only when requested by repeat-all semantics."""
    queue = {
        "current_playable_index": 2,
        "playable_list": [{"playable_id": "A"}, {"playable_id": "B"}, {"playable_id": "C"}],
    }
    view = YnisonQueueView(queue)

    assert view.next_index() is None
    assert view.next_index(wrap=True) == 0


# ------------------------------------------------------------------
# Queue mutations
# ------------------------------------------------------------------


def _player_state(
    count: int,
    index: int,
    *,
    repeat: str = "NONE",
    entity_type: str = "VARIOUS",
    shuffle: list[int] | None = None,
) -> dict[str, Any]:
    """Build a complete player state with ``count`` playables."""
    queue: dict[str, Any] = {
        "current_playable_index": index,
        "playable_list": [{"playable_id": f"t{i}"} for i in range(count)],
        "options": {"repeat_mode": repeat},
        "entity_type": entity_type,
    }
    if shuffle is not None:
        queue["shuffle_optional"] = {"playable_indices": shuffle}
    return {
        "player_queue": queue,
        "status": {"progress_ms": "5000", "duration_ms": "9000", "paused": False},
    }


@pytest.mark.parametrize(
    ("natural", "repeat", "count", "index", "outcome", "next_index"),
    [
        (True, "NONE", 3, 0, "change", 1),
        (True, "ONE", 3, 1, "restart", 1),
        (False, "ONE", 3, 1, "change", 2),
        (True, "ALL", 3, 2, "change", 0),
        (True, "ALL", 1, 0, "restart", 0),
        (True, "NONE", 3, 2, "stop", 2),
    ],
)
def test_plan_navigation_outcomes(
    natural: bool, repeat: str, count: int, index: int, outcome: str, next_index: int
) -> None:
    """Navigation follows repeat semantics and publishes a fresh status."""
    state = _player_state(count, index, repeat=repeat)

    assert plan_navigation(state, natural=natural, duration_ms=9000, device_id="me") == outcome
    assert state["player_queue"]["current_playable_index"] == next_index
    assert state["status"]["version"]["device_id"] == "me"
    if outcome == "stop":
        assert state["status"]["paused"] is True
        assert state["status"]["progress_ms"] == "9000"
    else:
        assert state["status"]["paused"] is False
        assert state["status"]["progress_ms"] == "0"


def test_plan_navigation_radio_end_requests_replenishment() -> None:
    """The end of a RADIO queue asks for more items without editing the state."""
    state = _player_state(3, 2, repeat="ALL", entity_type="RADIO")
    before = deepcopy(state)

    assert plan_navigation(state, natural=True, duration_ms=9000, device_id="me") == "radio"
    assert state == before


def test_set_index_resets_status() -> None:
    """Jumping to an index restarts the status at zero, authored by this device."""
    state = _player_state(3, 0)

    set_index(state, 2, device_id="me")

    assert state["player_queue"]["current_playable_index"] == 2
    assert state["status"]["progress_ms"] == "0"
    assert state["status"]["duration_ms"] == "0"
    assert state["status"]["version"]["device_id"] == "me"


def test_replace_items_extends_shuffle_order() -> None:
    """Appended items join the shuffle permutation."""
    state = _player_state(3, 1, shuffle=[1, 2, 0])
    items = [*state["player_queue"]["playable_list"], {"playable_id": "n0"}]

    replace_items(state, items)

    assert state["player_queue"]["shuffle_optional"]["playable_indices"] == [1, 2, 0, 3]


def test_replace_items_keeps_window_around_current_item() -> None:
    """Appends never publish more than the window; the current item and its tail survive."""
    state = _player_state(QUEUE_WINDOW, 150)
    items = [
        *state["player_queue"]["playable_list"],
        *({"playable_id": f"n{i}"} for i in range(10)),
    ]

    dropped = replace_items(state, items)

    queue = state["player_queue"]
    assert dropped == 10
    assert len(queue["playable_list"]) == QUEUE_WINDOW
    assert queue["playable_list"][queue["current_playable_index"]] == {"playable_id": "t150"}
    assert queue["playable_list"][-1] == {"playable_id": "n9"}


def test_replace_items_never_drops_the_current_item() -> None:
    """When the current item is near the start, the window grows rather than lose it."""
    state = _player_state(QUEUE_WINDOW, 3)
    items = [
        *state["player_queue"]["playable_list"],
        *({"playable_id": f"n{i}"} for i in range(10)),
    ]

    assert replace_items(state, items) == 3
    assert state["player_queue"]["current_playable_index"] == 0


def test_replace_items_does_not_trim_shuffled_queue() -> None:
    """A shuffle permutation is not re-indexed by trimming."""
    order = list(range(QUEUE_WINDOW))
    state = _player_state(QUEUE_WINDOW, 150, shuffle=order)
    items = [*state["player_queue"]["playable_list"], {"playable_id": "n0"}]

    assert replace_items(state, items) == 0
    assert len(state["player_queue"]["playable_list"]) == QUEUE_WINDOW + 1


def test_set_index_with_items_shifts_target_by_trimmed_prefix() -> None:
    """A target index into the appended list follows the trimmed window."""
    state = _player_state(QUEUE_WINDOW, QUEUE_WINDOW - 1)
    items = [*state["player_queue"]["playable_list"], *({"playable_id": f"n{i}"} for i in range(5))]

    set_index(state, QUEUE_WINDOW, device_id="me", items=items)

    queue = state["player_queue"]
    assert queue["playable_list"][queue["current_playable_index"]] == {"playable_id": "n0"}


@pytest.mark.parametrize(
    ("mode", "wire"), [(RepeatMode.OFF, "NONE"), (RepeatMode.ONE, "ONE"), (RepeatMode.ALL, "ALL")]
)
def test_repeat_mapping_round_trips(mode: RepeatMode, wire: str) -> None:
    """Repeat modes map to the Ynison wire value and back."""
    state = _player_state(2, 0)

    set_repeat(state, mode)

    assert state["player_queue"]["options"]["repeat_mode"] == wire
    assert repeat_from_ynison(state["player_queue"]) == mode


def test_unknown_wire_repeat_is_unknown() -> None:
    """An unknown repeat value maps to UNKNOWN; a missing one is OFF."""
    assert repeat_from_ynison({"options": {"repeat_mode": "SOMETIMES"}}) == RepeatMode.UNKNOWN
    assert repeat_from_ynison({}) == RepeatMode.OFF


def test_set_shuffle_keeps_current_item_first() -> None:
    """Enabling shuffle starts the permutation at the current item; disabling removes it."""
    state = _player_state(5, 3)

    set_shuffle(state, enabled=True, rng=random.Random(1))  # noqa: S311 - deterministic order
    order = state["player_queue"]["shuffle_optional"]["playable_indices"]
    assert order[0] == 3
    assert sorted(order) == [0, 1, 2, 3, 4]

    set_shuffle(state, enabled=False)
    assert "shuffle_optional" not in state["player_queue"]


def test_radio_items_map_tracks_to_playables() -> None:
    """Rotor tracks become Ynison playables with the queue's origin."""
    album = SimpleNamespace(id=7)
    tracks = [
        SimpleNamespace(id=1, title="One", albums=[album], cover_uri="cover/%%"),
        SimpleNamespace(id=2, title=None, albums=[], cover_uri=None),
    ]

    assert radio_items(tracks, "radio-origin") == [
        {
            "playable_id": "1",
            "album_id_optional": "7",
            "playable_type": "TRACK",
            "from": "radio-origin",
            "title": "One",
            "cover_url_optional": "cover/%%",
        },
        {
            "playable_id": "2",
            "album_id_optional": "",
            "playable_type": "TRACK",
            "from": "radio-origin",
            "title": "",
            "cover_url_optional": "",
        },
    ]
