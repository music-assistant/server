"""Tests for Ynison logical queue ordering."""

from music_assistant.providers.yandex_ynison.queue_model import YnisonQueueView


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
