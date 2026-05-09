"""Tests for elicitation on destructive operations (C8)."""
# mypy: disable-error-code="arg-type, no-untyped-def, type-arg, assignment, operator, misc"

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastmcp import Client, FastMCP

from music_assistant.providers.fastmcp_server.tools import build_media_server, build_queue_server


def _server(mass: MagicMock, *, require_confirmation: bool) -> FastMCP:
    """Build a small root server mounting only queue + media for elicitation tests."""
    mcp: FastMCP = FastMCP(name="t")
    mcp.mount(
        build_queue_server(mass, require_confirmation=require_confirmation),
        namespace="queue",
    )
    mcp.mount(
        build_media_server(mass, require_confirmation=require_confirmation),
        namespace="media",
    )
    return mcp


def _accepter() -> object:
    """Build an elicitation handler that always accepts with True."""

    async def handler(message, response_type, params, context):  # noqa: ARG001
        return True

    return handler


def _decliner() -> object:
    """Build an elicitation handler that always declines."""
    from fastmcp.client.elicitation import ElicitResult  # noqa: PLC0415

    async def handler(message, response_type, params, context):  # noqa: ARG001
        return ElicitResult(action="decline", content=None)

    return handler


async def test_clear_queue_runs_when_user_accepts(mock_mass: MagicMock) -> None:
    """User accepts the elicitation prompt → clear_queue dispatches to MA."""
    mock_mass.player_queues.clear = AsyncMock()
    mcp = _server(mock_mass, require_confirmation=True)

    async with Client(mcp, elicitation_handler=_accepter()) as client:
        await client.call_tool("queue_clear_queue", {"queue_id": "q1"})
    mock_mass.player_queues.clear.assert_awaited_once_with("q1")


async def test_clear_queue_blocked_when_user_declines(mock_mass: MagicMock) -> None:
    """User declines → tool raises ToolError, no MA call is made."""
    mock_mass.player_queues.clear = AsyncMock()
    mcp = _server(mock_mass, require_confirmation=True)

    async with Client(mcp, elicitation_handler=_decliner()) as client:
        with pytest.raises(Exception):  # noqa: B017,PT011
            await client.call_tool("queue_clear_queue", {"queue_id": "q1"})
    mock_mass.player_queues.clear.assert_not_awaited()


async def test_no_confirmation_when_disabled(mock_mass: MagicMock) -> None:
    """With require_confirmation=False, elicitation is skipped entirely."""
    mock_mass.player_queues.clear = AsyncMock()
    mcp = _server(mock_mass, require_confirmation=False)

    elicit_called = False

    async def handler(message, response_type, params, context):  # noqa: ARG001
        nonlocal elicit_called
        elicit_called = True
        return True

    async with Client(mcp, elicitation_handler=handler) as client:
        await client.call_tool("queue_clear_queue", {"queue_id": "q1"})
    assert elicit_called is False
    mock_mass.player_queues.clear.assert_awaited_once_with("q1")


async def test_remove_from_library_confirms(mock_mass: MagicMock) -> None:
    """media.remove_from_library also triggers elicitation."""
    # MA's MusicController takes (media_type, library_item_id), not a URI —
    # the tool resolves the URI via get_item_by_uri first.
    resolved = MagicMock(media_type=MagicMock(), item_id=42)
    mock_mass.music.get_item_by_uri = AsyncMock(return_value=resolved)
    mock_mass.music.remove_item_from_library = AsyncMock()
    mcp = _server(mock_mass, require_confirmation=True)

    async with Client(mcp, elicitation_handler=_accepter()) as client:
        await client.call_tool("media_remove_from_library", {"uri": "lib://t/42"})
    mock_mass.music.get_item_by_uri.assert_awaited_once_with("lib://t/42")
    mock_mass.music.remove_item_from_library.assert_awaited_once_with(resolved.media_type, 42)
