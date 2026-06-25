"""Tests for the queue_remove_item MCP tool."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, call

import pytest
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError
from music_assistant_models.errors import InvalidDataError


async def test_remove_item_deletes_each_id(mounted_queue: FastMCP, mock_mass: MagicMock) -> None:
    """Each item_id is forwarded to player_queues.delete_item."""
    mock_mass.player_queues.get = MagicMock(return_value=SimpleNamespace(queue_id="q1"))
    mock_mass.player_queues.delete_item = MagicMock()
    async with Client(mounted_queue) as client:
        await client.call_tool(
            "queue_remove_item",
            {"queue_id": "q1", "item_ids": ["abc", "def"]},
        )
    assert mock_mass.player_queues.delete_item.call_args_list == [
        call("q1", "abc"),
        call("q1", "def"),
    ]


async def test_remove_item_requires_ids(mounted_queue: FastMCP) -> None:
    """An empty item_ids list raises ToolError."""
    async with Client(mounted_queue) as client:
        with pytest.raises(ToolError, match="item_id"):
            await client.call_tool("queue_remove_item", {"queue_id": "q1", "item_ids": []})


async def test_remove_item_raises_tool_error_on_missing_item(
    mounted_queue: FastMCP, mock_mass: MagicMock
) -> None:
    """Unknown item_id surfaces as ToolError."""
    mock_mass.player_queues.get = MagicMock(return_value=SimpleNamespace(queue_id="q1"))
    mock_mass.player_queues.delete_item = MagicMock(
        side_effect=InvalidDataError("Item missing not found in queue")
    )
    async with Client(mounted_queue) as client:
        with pytest.raises(ToolError, match="not found"):
            await client.call_tool(
                "queue_remove_item",
                {"queue_id": "q1", "item_ids": ["missing"]},
            )


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
