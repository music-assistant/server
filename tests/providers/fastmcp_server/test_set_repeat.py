"""
Tests for the set_repeat MCP tool.

Validates that:
1. Valid repeat_mode values (off/one/all) are accepted and forwarded to MA.
2. Invalid repeat_mode values raise a clean ToolError.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError
from music_assistant_models.enums import RepeatMode

from music_assistant.providers.fastmcp_server.tools.queue import build_queue_server


@pytest.fixture
def mounted_queue(mock_mass: Any) -> FastMCP:
    """Build a root FastMCP with the queue sub-server mounted."""
    mcp: FastMCP = FastMCP(name="test")
    mcp.mount(build_queue_server(mock_mass), namespace="queue")
    return mcp


async def test_set_repeat_accepts_valid_modes(mounted_queue: FastMCP, mock_mass: MagicMock) -> None:
    """Each valid repeat_mode value is accepted and forwarded to MA."""
    for mode in ("off", "one", "all"):
        mock_mass.player_queues.set_repeat.reset_mock()
        async with Client(mounted_queue) as client:
            await client.call_tool("queue_set_repeat", {"queue_id": "q1", "repeat_mode": mode})
        mock_mass.player_queues.set_repeat.assert_called_once_with("q1", RepeatMode(mode))


async def test_set_repeat_accepts_mixed_case(mounted_queue: FastMCP, mock_mass: MagicMock) -> None:
    """repeat_mode is normalized to lowercase before validation."""
    mock_mass.player_queues.set_repeat.reset_mock()
    async with Client(mounted_queue) as client:
        await client.call_tool("queue_set_repeat", {"queue_id": "q1", "repeat_mode": "ALL"})
    mock_mass.player_queues.set_repeat.assert_called_once_with("q1", RepeatMode.ALL)


async def test_set_repeat_rejects_invalid_mode(mounted_queue: FastMCP) -> None:
    """Invalid repeat_mode raises ToolError with the list of valid options."""
    async with Client(mounted_queue) as client:
        with pytest.raises(ToolError, match="bogus"):
            await client.call_tool("queue_set_repeat", {"queue_id": "q1", "repeat_mode": "bogus"})


async def test_set_repeat_defaults_to_off(mounted_queue: FastMCP, mock_mass: MagicMock) -> None:
    """Calling set_repeat without repeat_mode defaults to 'off'."""
    mock_mass.player_queues.set_repeat.reset_mock()
    async with Client(mounted_queue) as client:
        await client.call_tool("queue_set_repeat", {"queue_id": "q1"})
    mock_mass.player_queues.set_repeat.assert_called_once_with("q1", RepeatMode.OFF)
