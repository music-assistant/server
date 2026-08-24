"""Direct tests for the native safe queue-removal command."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from music_assistant_models.errors import InvalidDataError

from music_assistant.providers.fastmcp_server.commands.queue import remove_items_safe


async def test_safe_remove_never_deletes_played_or_buffered_rows() -> None:
    """Every requested id is classified once and only a future row is deleted."""
    mass = MagicMock()
    mass.player_queues.get.return_value = SimpleNamespace(current_index=1, index_in_buffer=2)
    positions: dict[str, list[int | None]] = {
        "played": [1],
        "buffered": [2],
        "future": [3, None],
        "stale": [None],
    }
    mass.player_queues.index_by_id.side_effect = lambda _queue_id, item_id: positions[item_id].pop(
        0
    )

    result = await remove_items_safe(mass, "q1", ["played", "buffered", "future", "stale"])

    assert result.skipped_played == ["played"]
    assert result.skipped_buffered == ["buffered"]
    assert result.removed == ["future"]
    assert result.not_found == ["stale"]
    mass.player_queues.delete_item.assert_called_once_with("q1", "future")


async def test_safe_remove_reclassifies_silently_ignored_delete() -> None:
    """A row MA leaves behind is reported as buffered, never falsely removed."""
    mass = MagicMock()
    mass.player_queues.get.return_value = SimpleNamespace(current_index=0, index_in_buffer=0)
    mass.player_queues.index_by_id.side_effect = [4, 4]

    result = await remove_items_safe(mass, "q1", ["racing"])

    assert result.removed == []
    assert result.skipped_buffered == ["racing"]


async def test_safe_remove_requires_at_least_one_id() -> None:
    """An empty batch fails before touching MA's queue controller."""
    mass = MagicMock()

    with pytest.raises(InvalidDataError, match="at least one queue item id"):
        await remove_items_safe(mass, "q1", [])

    mass.player_queues.get.assert_not_called()


async def test_safe_remove_rejects_unknown_queue() -> None:
    """A missing queue is distinguished from stale item ids."""
    mass = MagicMock()
    mass.player_queues.get.return_value = None

    with pytest.raises(KeyError, match="'q1'"):
        await remove_items_safe(mass, "q1", ["item"])
