"""Tests for the queue_remove_item MCP tool."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, call

import pytest
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError


def _queue_item(*, item_id: str, name: str = "Track") -> SimpleNamespace:
    return SimpleNamespace(queue_item_id=item_id, name=name, uri=f"library://track/{item_id}")


def _mock_removable_queue(
    mock_mass: MagicMock,
    *,
    queue_id: str,
    items: list[SimpleNamespace],
    index_in_buffer: int | None = None,
    current_index: int | None = None,
) -> None:
    """Wire get, index_by_id, and optional buffer/played guards for remove_item tests."""
    id_to_index = {it.queue_item_id: idx for idx, it in enumerate(items)}
    queue = SimpleNamespace(
        queue_id=queue_id,
        current_index=current_index,
        index_in_buffer=index_in_buffer,
        items=len(items),
    )
    mock_mass.player_queues.get = MagicMock(return_value=queue)
    mock_mass.player_queues.index_by_id = MagicMock(
        side_effect=lambda _qid, item_id: id_to_index.get(item_id)
    )


async def test_remove_item_deletes_each_id(mounted_queue: FastMCP, mock_mass: MagicMock) -> None:
    """Each removable item_id is forwarded to player_queues.delete_item."""
    _mock_removable_queue(
        mock_mass,
        queue_id="q1",
        items=[_queue_item(item_id="abc"), _queue_item(item_id="def")],
    )
    mock_mass.player_queues.delete_item = MagicMock()
    async with Client(mounted_queue) as client:
        result = await client.call_tool(
            "queue_remove_item",
            {"queue_id": "q1", "item_ids": ["abc", "def"]},
        )
    assert result.data.removed == ["abc", "def"]
    assert result.data.skipped_buffered == []
    assert result.data.skipped_played == []
    assert mock_mass.player_queues.delete_item.call_args_list == [
        call("q1", "abc"),
        call("q1", "def"),
    ]


async def test_remove_item_returns_ack(mounted_queue: FastMCP, mock_mass: MagicMock) -> None:
    """A single successful remove returns RemoveFromQueueResult."""
    _mock_removable_queue(
        mock_mass,
        queue_id="q1",
        items=[_queue_item(item_id="abc")],
    )
    mock_mass.player_queues.delete_item = MagicMock()
    async with Client(mounted_queue) as client:
        result = await client.call_tool(
            "queue_remove_item",
            {"queue_id": "q1", "item_ids": ["abc"]},
        )
    assert result.data.removed == ["abc"]
    assert result.data.skipped_buffered == []
    assert result.data.skipped_played == []


async def test_remove_item_skips_buffered_row(mounted_queue: FastMCP, mock_mass: MagicMock) -> None:
    """Rows at or before index_in_buffer are skipped without calling delete_item."""
    _mock_removable_queue(
        mock_mass,
        queue_id="q1",
        items=[_queue_item(item_id="g0"), _queue_item(item_id="g1")],
        index_in_buffer=0,
    )
    mock_mass.player_queues.delete_item = MagicMock()
    async with Client(mounted_queue) as client:
        result = await client.call_tool(
            "queue_remove_item",
            {"queue_id": "q1", "item_ids": ["g0"]},
        )
    assert result.data.removed == []
    assert result.data.skipped_buffered == ["g0"]
    assert result.data.skipped_played == []
    mock_mass.player_queues.delete_item.assert_not_called()


async def test_remove_item_skips_played_row(mounted_queue: FastMCP, mock_mass: MagicMock) -> None:
    """Rows at or before current_index are skipped without calling delete_item."""
    _mock_removable_queue(
        mock_mass,
        queue_id="q1",
        items=[
            _queue_item(item_id="played"),
            _queue_item(item_id="upnext"),
        ],
        current_index=0,
    )
    mock_mass.player_queues.delete_item = MagicMock()
    async with Client(mounted_queue) as client:
        result = await client.call_tool(
            "queue_remove_item",
            {"queue_id": "q1", "item_ids": ["played"]},
        )
    assert result.data.removed == []
    assert result.data.skipped_buffered == []
    assert result.data.skipped_played == ["played"]
    mock_mass.player_queues.delete_item.assert_not_called()


async def test_remove_item_mixed_played_and_upnext(
    mounted_queue: FastMCP, mock_mass: MagicMock
) -> None:
    """Played and up-next ids in one call land in separate ack buckets."""
    _mock_removable_queue(
        mock_mass,
        queue_id="q1",
        items=[
            _queue_item(item_id="played"),
            _queue_item(item_id="upnext"),
        ],
        current_index=0,
    )
    mock_mass.player_queues.delete_item = MagicMock()
    async with Client(mounted_queue) as client:
        result = await client.call_tool(
            "queue_remove_item",
            {"queue_id": "q1", "item_ids": ["played", "upnext"]},
        )
    assert result.data.removed == ["upnext"]
    assert result.data.skipped_buffered == []
    assert result.data.skipped_played == ["played"]
    mock_mass.player_queues.delete_item.assert_called_once_with("q1", "upnext")


async def test_remove_item_mixed_batch(mounted_queue: FastMCP, mock_mass: MagicMock) -> None:
    """Buffered and removable ids in one call land in separate ack buckets."""
    _mock_removable_queue(
        mock_mass,
        queue_id="q1",
        items=[_queue_item(item_id="g0"), _queue_item(item_id="g1")],
        index_in_buffer=0,
    )
    mock_mass.player_queues.delete_item = MagicMock()
    async with Client(mounted_queue) as client:
        result = await client.call_tool(
            "queue_remove_item",
            {"queue_id": "q1", "item_ids": ["g0", "g1"]},
        )
    assert result.data.removed == ["g1"]
    assert result.data.skipped_buffered == ["g0"]
    assert result.data.skipped_played == []
    mock_mass.player_queues.delete_item.assert_called_once_with("q1", "g1")


async def test_remove_item_requires_ids(mounted_queue: FastMCP) -> None:
    """An empty item_ids list raises ToolError."""
    async with Client(mounted_queue) as client:
        with pytest.raises(ToolError, match="item_id"):
            await client.call_tool("queue_remove_item", {"queue_id": "q1", "item_ids": []})


async def test_remove_item_raises_tool_error_on_missing_item(
    mounted_queue: FastMCP, mock_mass: MagicMock
) -> None:
    """Unknown item_id surfaces as ToolError."""
    _mock_removable_queue(mock_mass, queue_id="q1", items=[])
    mock_mass.player_queues.delete_item = MagicMock()
    async with Client(mounted_queue) as client:
        with pytest.raises(ToolError, match="not found"):
            await client.call_tool(
                "queue_remove_item",
                {"queue_id": "q1", "item_ids": ["missing"]},
            )
    mock_mass.player_queues.delete_item.assert_not_called()


async def test_remove_item_raises_tool_error_on_unknown_queue(
    mounted_queue: FastMCP, mock_mass: MagicMock
) -> None:
    """Unknown queue_id surfaces as ToolError before any delete."""
    mock_mass.player_queues.get = MagicMock(return_value=None)
    async with Client(mounted_queue) as client:
        with pytest.raises(ToolError, match="Queue 'q-missing' not found"):
            await client.call_tool(
                "queue_remove_item",
                {"queue_id": "q-missing", "item_ids": ["abc"]},
            )
