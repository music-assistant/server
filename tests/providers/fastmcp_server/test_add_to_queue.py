"""
Tests for the add_to_queue MCP tool.

Validates that:
1. Valid option values (add/next/play/replace/replace_next) are accepted and forwarded to MA.
2. Invalid option values raise a clean ToolError.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError
from music_assistant_models.enums import QueueOption

from music_assistant.providers.fastmcp_server.tools.queue import build_queue_server


@pytest.fixture
def mounted_queue(mock_mass: Any) -> FastMCP:
    """Build a root FastMCP with the queue sub-server mounted."""
    mcp: FastMCP = FastMCP(name="test")
    mcp.mount(build_queue_server(mock_mass), namespace="queue")
    return mcp


async def test_add_to_queue_accepts_valid_options(
    mounted_queue: FastMCP, mock_mass: MagicMock
) -> None:
    """Each valid option value is accepted and forwarded to MA."""
    for opt in ("add", "next", "play", "replace", "replace_next"):
        mock_mass.player_queues.play_media.reset_mock()
        async with Client(mounted_queue) as client:
            await client.call_tool(
                "queue_add_to_queue", {"queue_id": "q1", "uri": "spotify://track/1", "option": opt}
            )
        mock_mass.player_queues.play_media.assert_called_once_with(
            "q1", "spotify://track/1", option=QueueOption(opt)
        )


async def test_add_to_queue_rejects_invalid_option(mounted_queue: FastMCP) -> None:
    """Invalid option raises ToolError with the list of valid options."""
    async with Client(mounted_queue) as client:
        with pytest.raises(ToolError, match="bogus"):
            await client.call_tool(
                "queue_add_to_queue",
                {"queue_id": "q1", "uri": "spotify://track/1", "option": "bogus"},
            )


async def test_add_to_queue_defaults_to_add(mounted_queue: FastMCP, mock_mass: MagicMock) -> None:
    """Calling add_to_queue without option defaults to 'add'."""
    mock_mass.player_queues.play_media.reset_mock()
    async with Client(mounted_queue) as client:
        await client.call_tool("queue_add_to_queue", {"queue_id": "q1", "uri": "spotify://track/1"})
    mock_mass.player_queues.play_media.assert_called_once_with(
        "q1", "spotify://track/1", option=QueueOption.ADD
    )
