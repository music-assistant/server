"""Focused behavior tests for the native safe queue-removal handler."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, call

import pytest
from music_assistant_models.errors import InvalidDataError

from music_assistant.providers.fastmcp_server.commands.queue import remove_items_safe


def _wire_queue(
    mass: MagicMock,
    ids: list[str],
    *,
    current_index: int | None = None,
    index_in_buffer: int | None = None,
) -> None:
    indices = {item_id: index for index, item_id in enumerate(ids)}
    mass.player_queues.get.return_value = SimpleNamespace(
        current_index=current_index,
        index_in_buffer=index_in_buffer,
    )
    mass.player_queues.index_by_id.side_effect = lambda _queue_id, item_id: indices.get(item_id)
    mass.player_queues.delete_item.side_effect = lambda _queue_id, item_id: indices.pop(
        item_id, None
    )


async def test_remove_items_safe_reports_every_requested_item(mock_mass: MagicMock) -> None:
    """Removed, played, buffered, and missing ids land in distinct buckets."""
    _wire_queue(mock_mass, ["played", "buffered", "remove"], current_index=0, index_in_buffer=1)

    result = await remove_items_safe(
        mock_mass,
        "q1",
        ["played", "buffered", "remove", "missing"],
    )

    assert result.skipped_played == ["played"]
    assert result.skipped_buffered == ["buffered"]
    assert result.removed == ["remove"]
    assert result.not_found == ["missing"]
    assert mock_mass.player_queues.delete_item.call_args_list == [call("q1", "remove")]


async def test_remove_items_safe_keeps_partial_batch_ack(mock_mass: MagicMock) -> None:
    """A stale id does not discard acknowledgements for successful removals."""
    _wire_queue(mock_mass, ["a", "b"])

    result = await remove_items_safe(mock_mass, "q1", ["a", "missing", "b"])

    assert result.removed == ["a", "b"]
    assert result.not_found == ["missing"]


async def test_remove_items_safe_detects_silently_ignored_delete(mock_mass: MagicMock) -> None:
    """A row still present after delete is reported as buffered, not removed."""
    _wire_queue(mock_mass, ["racy"])
    mock_mass.player_queues.delete_item.side_effect = None

    result = await remove_items_safe(mock_mass, "q1", ["racy"])

    assert result.removed == []
    assert result.skipped_buffered == ["racy"]


async def test_remove_items_safe_requires_ids(mock_mass: MagicMock) -> None:
    """An empty request is rejected before queue lookup."""
    with pytest.raises(InvalidDataError, match="item id"):
        await remove_items_safe(mock_mass, "q1", [])


async def test_remove_items_safe_rejects_unknown_queue(mock_mass: MagicMock) -> None:
    """Unknown queue ids fail before any deletion attempt."""
    mock_mass.player_queues.get.return_value = None
    with pytest.raises(InvalidDataError, match="q-missing"):
        await remove_items_safe(mock_mass, "q-missing", ["a"])
