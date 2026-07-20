"""Tests for the opt-in simplified tool discovery (meta-tool) mode."""
# mypy: disable-error-code="arg-type, no-untyped-def, type-arg, assignment, operator, misc"

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from fastmcp import Client, FastMCP
from fastmcp.client.elicitation import ElicitResult
from fastmcp.exceptions import ToolError
from mcp.shared.exceptions import McpError

from music_assistant.providers.fastmcp_server.config import build_config_entries
from music_assistant.providers.fastmcp_server.constants import (
    CONF_META_TOOL_DISCOVERY,
    HOT_SWAPPABLE_KEYS,
)
from music_assistant.providers.fastmcp_server.meta_discovery import register_meta_discovery
from music_assistant.providers.fastmcp_server.middleware import TagFilterMiddleware
from music_assistant.providers.fastmcp_server.server import build_tag_lookup
from music_assistant.providers.fastmcp_server.tools import build_queue_server

_ALL_TAGS = {"query:queue", "edit:queue", "delete:queue", "control:playback"}
_META_NAMES = {"search_tools", "call_tool", "get_tool_schema"}


def _server(
    mock_mass: MagicMock,
    *,
    require_confirmation: bool = False,
) -> tuple[FastMCP, dict[str, Any]]:
    """
    Build a root FastMCP mirroring the runtime wiring for meta-discovery tests.

    Returns the server plus a mutable state dict: ``state["enabled"]`` gates
    the meta mode and ``state["allowed"]`` is the live allowed-tag set, both
    read through closures exactly like the runtime's hot-swap path.
    """
    state: dict[str, Any] = {"enabled": True, "allowed": set(_ALL_TAGS)}
    mcp: FastMCP = FastMCP(name="test")
    mcp.mount(
        build_queue_server(mock_mass, require_confirmation=require_confirmation),
        namespace="queue",
    )
    mcp.add_middleware(TagFilterMiddleware(lambda: state["allowed"], build_tag_lookup(mcp)))
    register_meta_discovery(
        mcp,
        enabled=lambda: bool(state["enabled"]),
        allowed_tags_provider=lambda: state["allowed"],
        lookup_component_tags=build_tag_lookup(mcp),
    )
    return mcp, state


async def test_meta_off_keeps_full_catalog(mock_mass: MagicMock) -> None:
    """With the toggle off the normal catalog is listed and no meta tool leaks."""
    mcp, state = _server(mock_mass)
    state["enabled"] = False
    async with Client(mcp) as client:
        names = {t.name for t in await client.list_tools()}
    assert "queue_get_active_queue" in names
    assert not (_META_NAMES & names)


async def test_meta_on_lists_exactly_three_tools(mock_mass: MagicMock) -> None:
    """With the toggle on the listing collapses to the three meta tools."""
    mcp, _state = _server(mock_mass)
    async with Client(mcp) as client:
        names = {t.name for t in await client.list_tools()}
    assert names == _META_NAMES


async def test_toggle_hot_swaps_listing_without_rebuild(mock_mass: MagicMock) -> None:
    """Flipping the flag changes the listing on the next request, same server."""
    mcp, state = _server(mock_mass)
    async with Client(mcp) as client:
        assert {t.name for t in await client.list_tools()} == _META_NAMES
        state["enabled"] = False
        names = {t.name for t in await client.list_tools()}
        assert "queue_set_shuffle" in names
        assert not (_META_NAMES & names)
        state["enabled"] = True
        assert {t.name for t in await client.list_tools()} == _META_NAMES


async def test_search_tools_returns_lightweight_results(mock_mass: MagicMock) -> None:
    """search_tools ranks the catalog and never inlines schemas."""
    mcp, _state = _server(mock_mass)
    async with Client(mcp) as client:
        result = await client.call_tool("search_tools", {"query": "shuffle queue"})
    entries = result.data
    names = [e["name"] for e in entries]
    assert "queue_set_shuffle" in names
    for entry in entries:
        assert set(entry) == {"name", "description"}, (
            f"search result must stay lightweight, got keys {set(entry)}"
        )


async def test_search_tools_respects_rbac(mock_mass: MagicMock) -> None:
    """A tag-disabled tool never surfaces in search results."""
    mcp, state = _server(mock_mass)
    state["allowed"] = {"query:queue"}
    async with Client(mcp) as client:
        result = await client.call_tool("search_tools", {"query": "shuffle queue"})
    names = [e["name"] for e in result.data]
    assert "queue_set_shuffle" not in names, "edit:queue is disabled — must not surface"


async def test_call_tool_proxies_execution(mock_mass: MagicMock) -> None:
    """call_tool executes a permitted catalogued tool with the given arguments."""
    mcp, _state = _server(mock_mass)
    async with Client(mcp) as client:
        await client.call_tool(
            "call_tool",
            {"name": "queue_set_shuffle", "arguments": {"queue_id": "q1", "enabled": True}},
        )
    mock_mass.player_queues.set_shuffle.assert_awaited_once_with("q1", True)


async def test_call_tool_blocked_for_disabled_tag(mock_mass: MagicMock) -> None:
    """The proxy re-enters the middleware chain, so RBAC still blocks the call."""
    mcp, state = _server(mock_mass)
    state["allowed"] = {"query:queue"}
    async with Client(mcp) as client:
        with pytest.raises((ToolError, McpError), match=r"disabled|not found"):
            await client.call_tool(
                "call_tool",
                {"name": "queue_set_shuffle", "arguments": {"queue_id": "q1", "enabled": True}},
            )
    mock_mass.player_queues.set_shuffle.assert_not_awaited()


async def test_get_tool_schema_returns_full_schema(mock_mass: MagicMock) -> None:
    """get_tool_schema returns the input schema for one permitted tool."""
    mcp, _state = _server(mock_mass)
    async with Client(mcp) as client:
        result = await client.call_tool("get_tool_schema", {"tool_name": "queue_set_shuffle"})
    schema = result.data
    assert schema["name"] == "queue_set_shuffle"
    assert "queue_id" in schema["inputSchema"]["properties"]
    assert "enabled" in schema["inputSchema"]["properties"]


async def test_get_tool_schema_hides_disabled_tool(mock_mass: MagicMock) -> None:
    """A tag-disabled tool's schema is not disclosed."""
    mcp, state = _server(mock_mass)
    state["allowed"] = {"query:queue"}
    async with Client(mcp) as client:
        with pytest.raises((ToolError, McpError), match="not found"):
            await client.call_tool("get_tool_schema", {"tool_name": "queue_set_shuffle"})


async def test_get_tool_schema_unknown_tool(mock_mass: MagicMock) -> None:
    """An unknown tool name reports not-found."""
    mcp, _state = _server(mock_mass)
    async with Client(mcp) as client:
        with pytest.raises((ToolError, McpError), match="not found"):
            await client.call_tool("get_tool_schema", {"tool_name": "no_such_tool"})


async def test_elicitation_fires_through_call_tool_proxy(mock_mass: MagicMock) -> None:
    """Destructive-op confirmation still gates tools invoked via the proxy."""
    prompts: list[str] = []

    async def handler(message, response_type, params, context):  # noqa: ARG001
        prompts.append(message)
        return True

    mcp, _state = _server(mock_mass, require_confirmation=True)
    async with Client(mcp, elicitation_handler=handler) as client:
        await client.call_tool(
            "call_tool",
            {"name": "queue_clear_queue", "arguments": {"queue_id": "q1"}},
        )
    assert prompts, "elicitation prompt must reach the client through the proxy"
    mock_mass.player_queues.clear.assert_called_once_with("q1")


async def test_elicitation_decline_blocks_through_proxy(mock_mass: MagicMock) -> None:
    """Declining the confirmation through the proxy leaves the queue untouched."""

    async def decliner(message, response_type, params, context):  # noqa: ARG001
        return ElicitResult(action="decline", content=None)

    mcp, _state = _server(mock_mass, require_confirmation=True)
    async with Client(mcp, elicitation_handler=decliner) as client:
        with pytest.raises((ToolError, McpError)):
            await client.call_tool(
                "call_tool",
                {"name": "queue_clear_queue", "arguments": {"queue_id": "q1"}},
            )
    mock_mass.player_queues.clear.assert_not_called()


async def test_direct_call_still_works_in_meta_mode(mock_mass: MagicMock) -> None:
    """
    Catalogued tools stay callable by name in meta mode (hidden, not blocked).

    FastMCP's search transforms hide tools from the listing but keep
    ``get_tool`` delegating, so a stale client that cached tool names keeps
    working; RBAC still applies via the middleware.
    """
    mcp, _state = _server(mock_mass)
    async with Client(mcp) as client:
        await client.call_tool("queue_set_shuffle", {"queue_id": "q1", "enabled": False})
    mock_mass.player_queues.set_shuffle.assert_awaited_once_with("q1", False)


async def test_config_entry_registered_default_off(mock_mass: MagicMock) -> None:
    """The meta toggle ships as a Server-category boolean, default off."""
    entries = {e.key: e for e in build_config_entries(mock_mass, {})}
    entry = entries[CONF_META_TOOL_DISCOVERY]
    assert entry.default_value is False
    assert entry.category == "server"
    assert CONF_META_TOOL_DISCOVERY in HOT_SWAPPABLE_KEYS
