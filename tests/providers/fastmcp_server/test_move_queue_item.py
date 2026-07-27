"""Tests for queue move / reorder MCP tools."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError
from music_assistant_models.errors import InvalidDataError


def _queue_item(*, item_id: str, name: str) -> SimpleNamespace:
    return SimpleNamespace(queue_item_id=item_id, name=name, duration=180, media_item=None)


def _mock_queue(mock_mass: MagicMock, *, queue_id: str, items: list[SimpleNamespace]) -> None:
    queue = SimpleNamespace(
        queue_id=queue_id,
        current_index=0,
        items=len(items),
        shuffle_enabled=False,
        repeat_mode=None,
        available=True,
    )
    mock_mass.player_queues.get = MagicMock(return_value=queue)
    mock_mass.player_queues.items = MagicMock(return_value=items)


async def test_move_item_forwards_pos_shift(mounted_queue: FastMCP, mock_mass: MagicMock) -> None:
    """pos_shift is forwarded to player_queues.move_item."""
    _mock_queue(mock_mass, queue_id="q1", items=[_queue_item(item_id="item-1", name="One")])
    async with Client(mounted_queue) as client:
        result = await client.call_tool(
            "queue_move_item",
            {"queue_id": "q1", "item_id": "item-1", "pos_shift": -1},
        )
    mock_mass.player_queues.move_item.assert_called_once_with("q1", "item-1", -1)
    assert result.data.queue_id == "q1"
    assert [i.item_id for i in result.data.items] == ["item-1"]


async def test_move_item_defaults_pos_shift(mounted_queue: FastMCP, mock_mass: MagicMock) -> None:
    """pos_shift defaults to +1 (move down one slot)."""
    _mock_queue(mock_mass, queue_id="q1", items=[_queue_item(item_id="item-1", name="One")])
    async with Client(mounted_queue) as client:
        await client.call_tool(
            "queue_move_item",
            {"queue_id": "q1", "item_id": "item-1"},
        )
    mock_mass.player_queues.move_item.assert_called_once_with("q1", "item-1", 1)


async def test_move_item_returns_queue_brief(mounted_queue: FastMCP, mock_mass: MagicMock) -> None:
    """A successful move returns the reordered queue order inline."""
    _mock_queue(
        mock_mass,
        queue_id="q1",
        items=[
            _queue_item(item_id="item-2", name="Two"),
            _queue_item(item_id="item-1", name="One"),
        ],
    )
    async with Client(mounted_queue) as client:
        result = await client.call_tool(
            "queue_move_item",
            {"queue_id": "q1", "item_id": "item-2", "pos_shift": -1},
        )
    assert [i.item_id for i in result.data.items] == ["item-2", "item-1"]
    assert [i.name for i in result.data.items] == ["Two", "One"]


async def test_move_item_raises_tool_error_on_buffered_item(
    mounted_queue: FastMCP, mock_mass: MagicMock
) -> None:
    """Buffered/played items surface as ToolError for the agent."""
    mock_mass.player_queues.get = MagicMock(return_value=SimpleNamespace(queue_id="q1"))
    mock_mass.player_queues.move_item = MagicMock(
        side_effect=IndexError("0 is already played/buffered")
    )
    async with Client(mounted_queue) as client:
        with pytest.raises(ToolError, match="buffered"):
            await client.call_tool(
                "queue_move_item",
                {"queue_id": "q1", "item_id": "item-1", "pos_shift": 0},
            )


async def test_move_item_raises_tool_error_on_missing_item(
    mounted_queue: FastMCP, mock_mass: MagicMock
) -> None:
    """Unknown item_id surfaces as ToolError."""
    mock_mass.player_queues.get = MagicMock(return_value=SimpleNamespace(queue_id="q1"))
    mock_mass.player_queues.move_item = MagicMock(
        side_effect=InvalidDataError("Item missing not found in queue")
    )
    async with Client(mounted_queue) as client:
        with pytest.raises(ToolError, match="not found"):
            await client.call_tool(
                "queue_move_item",
                {"queue_id": "q1", "item_id": "missing", "pos_shift": 1},
            )


async def test_move_item_raises_tool_error_on_unknown_queue(
    mounted_queue: FastMCP, mock_mass: MagicMock
) -> None:
    """Unknown queue_id surfaces as ToolError before the move call."""
    mock_mass.player_queues.get = MagicMock(return_value=None)
    async with Client(mounted_queue) as client:
        with pytest.raises(ToolError, match="Queue 'q-missing' not found"):
            await client.call_tool(
                "queue_move_item",
                {"queue_id": "q-missing", "item_id": "item-1", "pos_shift": 1},
            )
    mock_mass.player_queues.move_item.assert_not_called()


async def test_move_item_raises_when_queue_missing_after_move(
    mounted_queue: FastMCP, mock_mass: MagicMock
) -> None:
    """A vanished queue after a successful move surfaces as ToolError."""
    queue = SimpleNamespace(queue_id="q1")
    mock_mass.player_queues.get = MagicMock(side_effect=[queue, None])
    async with Client(mounted_queue) as client:
        with pytest.raises(ToolError, match="not found after move"):
            await client.call_tool(
                "queue_move_item",
                {"queue_id": "q1", "item_id": "item-1", "pos_shift": 1},
            )


async def test_move_item_to_end_forwards_call(mounted_queue: FastMCP, mock_mass: MagicMock) -> None:
    """move_item_to_end delegates to player_queues.move_item_end and returns the brief."""
    _mock_queue(mock_mass, queue_id="q1", items=[_queue_item(item_id="item-1", name="One")])
    async with Client(mounted_queue) as client:
        result = await client.call_tool(
            "queue_move_item_to_end",
            {"queue_id": "q1", "item_id": "item-1"},
        )
    mock_mass.player_queues.move_item_end.assert_called_once_with("q1", "item-1")
    assert result.data.queue_id == "q1"
    assert [i.item_id for i in result.data.items] == ["item-1"]


async def test_move_item_to_end_raises_tool_error_on_unknown_queue(
    mounted_queue: FastMCP, mock_mass: MagicMock
) -> None:
    """Unknown queue_id surfaces as ToolError before move_item_end."""
    mock_mass.player_queues.get = MagicMock(return_value=None)
    async with Client(mounted_queue) as client:
        with pytest.raises(ToolError, match="Queue 'q-missing' not found"):
            await client.call_tool(
                "queue_move_item_to_end",
                {"queue_id": "q-missing", "item_id": "item-1"},
            )
    mock_mass.player_queues.move_item_end.assert_not_called()
