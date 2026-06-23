"""Tests for the queue_remove_item MCP tool."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError

from music_assistant.providers.fastmcp_server.tools.queue import build_queue_server


@pytest.fixture
def mounted_queue(mock_mass: Any) -> FastMCP:
    """Build a root FastMCP with the queue sub-server mounted."""
    mcp: FastMCP = FastMCP(name="test")
    mcp.mount(build_queue_server(mock_mass), namespace="queue")
    return mcp


async def test_remove_item_deletes_each_id(mounted_queue: FastMCP, mock_mass: MagicMock) -> None:
    """Each item_id is forwarded to player_queues.delete_item."""
    mock_mass.player_queues.delete_item = MagicMock()
    async with Client(mounted_queue) as client:
        await client.call_tool(
            "queue_remove_item",
            {"queue_id": "q1", "item_ids": ["abc", "def"]},
        )
    assert mock_mass.player_queues.delete_item.call_args_list == [
        (("q1", "abc"),),
        (("q1", "def"),),
    ]


async def test_remove_item_requires_ids(mounted_queue: FastMCP) -> None:
    """An empty item_ids list raises ToolError."""
    async with Client(mounted_queue) as client:
        with pytest.raises(ToolError, match="item_id"):
            await client.call_tool("queue_remove_item", {"queue_id": "q1", "item_ids": []})
