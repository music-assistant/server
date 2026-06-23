"""Tests for queue move / reorder MCP tools."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError
from music_assistant_models.errors import InvalidDataError

from music_assistant.providers.fastmcp_server.tools.queue import build_queue_server


@pytest.fixture
def mounted_queue(mock_mass: Any) -> FastMCP:
    """Build a root FastMCP with the queue sub-server mounted."""
    mcp: FastMCP = FastMCP(name="test")
    mcp.mount(build_queue_server(mock_mass), namespace="queue")
    return mcp


async def test_move_item_forwards_pos_shift(mounted_queue: FastMCP, mock_mass: MagicMock) -> None:
    """pos_shift is forwarded to player_queues.move_item."""
    mock_mass.player_queues.move_item = MagicMock()
    async with Client(mounted_queue) as client:
        await client.call_tool(
            "queue_move_item",
            {"queue_id": "q1", "item_id": "item-1", "pos_shift": -1},
        )
    mock_mass.player_queues.move_item.assert_called_once_with("q1", "item-1", -1)


async def test_move_item_defaults_pos_shift(mounted_queue: FastMCP, mock_mass: MagicMock) -> None:
    """pos_shift defaults to +1 (move down one slot)."""
    mock_mass.player_queues.move_item = MagicMock()
    async with Client(mounted_queue) as client:
        await client.call_tool(
            "queue_move_item",
            {"queue_id": "q1", "item_id": "item-1"},
        )
    mock_mass.player_queues.move_item.assert_called_once_with("q1", "item-1", 1)


async def test_move_item_raises_tool_error_on_buffered_item(
    mounted_queue: FastMCP, mock_mass: MagicMock
) -> None:
    """Buffered/played items surface as ToolError for the agent."""
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
    mock_mass.player_queues.move_item = MagicMock(
        side_effect=InvalidDataError("Item missing not found in queue")
    )
    async with Client(mounted_queue) as client:
        with pytest.raises(ToolError, match="not found"):
            await client.call_tool(
                "queue_move_item",
                {"queue_id": "q1", "item_id": "missing", "pos_shift": 1},
            )


async def test_move_item_to_end_forwards_call(mounted_queue: FastMCP, mock_mass: MagicMock) -> None:
    """move_item_to_end delegates to player_queues.move_item_end."""
    mock_mass.player_queues.move_item_end = MagicMock()
    async with Client(mounted_queue) as client:
        await client.call_tool(
            "queue_move_item_to_end",
            {"queue_id": "q1", "item_id": "item-1"},
        )
    mock_mass.player_queues.move_item_end.assert_called_once_with("q1", "item-1")
