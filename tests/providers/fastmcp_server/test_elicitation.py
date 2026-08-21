"""Tests for elicitation on destructive operations (C8)."""
# mypy: disable-error-code="arg-type, no-untyped-def, type-arg, assignment, operator, misc"

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError
from mcp.shared.exceptions import McpError
from mcp.types import INTERNAL_ERROR, INVALID_REQUEST, ErrorData

from music_assistant.providers.fastmcp_server.tools import build_media_server, build_queue_server
from music_assistant.providers.fastmcp_server.tools._common import confirm_or_raise


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
    mock_mass.player_queues.clear.assert_called_once_with("q1")


async def test_clear_queue_blocked_when_user_declines(mock_mass: MagicMock) -> None:
    """User declines → tool raises ToolError, no MA call is made."""
    mock_mass.player_queues.clear = AsyncMock()
    mcp = _server(mock_mass, require_confirmation=True)

    async with Client(mcp, elicitation_handler=_decliner()) as client:
        with pytest.raises(ToolError):
            await client.call_tool("queue_clear_queue", {"queue_id": "q1"})
    mock_mass.player_queues.clear.assert_not_called()


async def test_remove_item_runs_when_user_accepts(mock_mass: MagicMock) -> None:
    """User accepts the elicitation prompt → remove_item dispatches to MA."""
    queue = MagicMock(queue_id="q1", current_index=0, index_in_buffer=0)
    mock_mass.player_queues.get = MagicMock(return_value=queue)
    mock_mass.player_queues.index_by_id = MagicMock(side_effect=[2, None])
    mcp = _server(mock_mass, require_confirmation=True)

    async with Client(mcp, elicitation_handler=_accepter()) as client:
        result = await client.call_tool(
            "queue_remove_item",
            {"queue_id": "q1", "item_ids": ["item-1"]},
        )
    mock_mass.player_queues.delete_item.assert_called_once_with("q1", "item-1")
    assert result.data.removed == ["item-1"]


async def test_remove_item_blocked_when_user_declines(mock_mass: MagicMock) -> None:
    """User declines → tool raises ToolError, no MA call is made."""
    mock_mass.player_queues.get = MagicMock(return_value=MagicMock(queue_id="q1"))
    mcp = _server(mock_mass, require_confirmation=True)

    async with Client(mcp, elicitation_handler=_decliner()) as client:
        with pytest.raises(ToolError):
            await client.call_tool(
                "queue_remove_item",
                {"queue_id": "q1", "item_ids": ["item-1"]},
            )
    mock_mass.player_queues.delete_item.assert_not_called()


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
    mock_mass.player_queues.clear.assert_called_once_with("q1")


async def test_get_active_queue_clamps_include_items(mock_mass: MagicMock) -> None:
    """
    A client-supplied ``include_items`` is clamped to 500 to bound memory.

    Without the clamp a hostile or sloppy caller could pass ``include_items=10**6``
    and force MA to materialise the entire queue per request.
    """
    queue = MagicMock(queue_id="q1")
    mock_mass.player_queues.get_active_queue = MagicMock(return_value=queue)
    mock_mass.player_queues.items = MagicMock(return_value=[])
    mcp = _server(mock_mass, require_confirmation=False)

    async with Client(mcp) as client:
        await client.call_tool(
            "queue_get_active_queue",
            {"player_id": "p1", "include_items": 10_000},
        )
    mock_mass.player_queues.items.assert_called_once_with("q1", limit=500, offset=0)


async def test_get_active_queue_passes_small_limit_through(mock_mass: MagicMock) -> None:
    """A reasonable ``include_items`` is forwarded verbatim — no over-cap."""
    queue = MagicMock(queue_id="q1")
    mock_mass.player_queues.get_active_queue = MagicMock(return_value=queue)
    mock_mass.player_queues.items = MagicMock(return_value=[])
    mcp = _server(mock_mass, require_confirmation=False)

    async with Client(mcp) as client:
        await client.call_tool(
            "queue_get_active_queue",
            {"player_id": "p1", "include_items": 10},
        )
    mock_mass.player_queues.items.assert_called_once_with("q1", limit=10, offset=0)


async def test_remove_from_library_confirms(mock_mass: MagicMock) -> None:
    """media.remove_from_library also triggers elicitation."""
    # MA's MusicController takes (media_type, library_item_id), not a URI —
    # the tool resolves the URI via get_item_by_uri first.
    resolved = MagicMock(media_type=MagicMock(), item_id="42", provider="library")
    mock_mass.music.get_item_by_uri = AsyncMock(return_value=resolved)
    mock_mass.music.remove_item_from_library = AsyncMock()
    mcp = _server(mock_mass, require_confirmation=True)

    async with Client(mcp, elicitation_handler=_accepter()) as client:
        await client.call_tool("media_remove_from_library", {"uri": "lib://t/42"})
    mock_mass.music.get_item_by_uri.assert_awaited_once_with("lib://t/42")
    mock_mass.music.remove_item_from_library.assert_awaited_once_with(resolved.media_type, "42")


async def test_remove_from_favorites_resolves_provider_uri_to_library(
    mock_mass: MagicMock,
) -> None:
    """
    A provider URI is resolved to the matching library item before removal.

    ``MusicController.remove_item_from_*`` expects a library item id; passing the
    provider's native item id silently targets the wrong item (or raises on a
    non-numeric ``int()`` cast). The tool now looks up the library counterpart via
    ``get_library_item_by_prov_id``.
    """
    provider_item = MagicMock(media_type=MagicMock(), item_id="prov-abc", provider="yandex_music")
    library_item = MagicMock(media_type=provider_item.media_type, item_id="99")
    mock_mass.music.get_item_by_uri = AsyncMock(return_value=provider_item)
    mock_mass.music.get_library_item_by_prov_id = AsyncMock(return_value=library_item)
    mock_mass.music.remove_item_from_favorites = AsyncMock()
    mcp = _server(mock_mass, require_confirmation=False)

    async with Client(mcp) as client:
        await client.call_tool(
            "media_remove_from_favorites", {"uri": "yandex_music://track/prov-abc"}
        )
    mock_mass.music.get_library_item_by_prov_id.assert_awaited_once_with(
        provider_item.media_type, "prov-abc", "yandex_music"
    )
    mock_mass.music.remove_item_from_favorites.assert_awaited_once_with(
        library_item.media_type, "99"
    )


async def test_remove_from_library_raises_when_not_in_library(
    mock_mass: MagicMock,
) -> None:
    """
    When the URI's library counterpart cannot be resolved, the tool raises.

    Without this, the tool would silently call ``remove_item_from_library`` with
    a provider-native item id, which either fails on ``int()`` cast or targets
    the wrong item.
    """
    provider_item = MagicMock(media_type=MagicMock(), item_id="prov-abc", provider="yandex_music")
    mock_mass.music.get_item_by_uri = AsyncMock(return_value=provider_item)
    mock_mass.music.get_library_item_by_prov_id = AsyncMock(return_value=None)
    mock_mass.music.remove_item_from_library = AsyncMock()
    mcp = _server(mock_mass, require_confirmation=False)

    async with Client(mcp) as client:
        with pytest.raises(ToolError):
            await client.call_tool(
                "media_remove_from_library", {"uri": "yandex_music://track/prov-abc"}
            )
    mock_mass.music.remove_item_from_library.assert_not_awaited()


async def test_confirm_passes_through_when_elicitation_unsupported() -> None:
    """McpError(INVALID_REQUEST) = no capability → pass through (no raise)."""
    ctx = MagicMock()
    ctx.elicit = AsyncMock(
        side_effect=McpError(ErrorData(code=INVALID_REQUEST, message="Elicitation not supported"))
    )
    # Must NOT raise — pass-through to permission flag.
    await confirm_or_raise(ctx, "confirm?", enabled=True)


async def test_confirm_fails_closed_on_unexpected_mcp_error() -> None:
    """A non-capability McpError must re-raise (fail closed), not bypass confirmation."""
    ctx = MagicMock()
    ctx.elicit = AsyncMock(
        side_effect=McpError(ErrorData(code=INTERNAL_ERROR, message="handler blew up"))
    )
    with pytest.raises(McpError):
        await confirm_or_raise(ctx, "confirm?", enabled=True)
