"""Tests for TagFilterMiddleware enforcement on direct invocation (C3)."""
# mypy: disable-error-code="arg-type, no-untyped-def, type-arg, assignment, operator, misc"

from __future__ import annotations

import pytest
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError
from mcp.shared.exceptions import McpError

from music_assistant.providers.fastmcp_server.middleware import TagFilterMiddleware
from music_assistant.providers.fastmcp_server.server import build_tag_lookup


def _build_server(allowed: set[str]) -> FastMCP:
    """Construct a FastMCP root with one tagged tool and the tag-filter middleware."""
    mcp: FastMCP = FastMCP(name="test-server")

    @mcp.tool(tags={"query"})  # type: ignore[untyped-decorator, unused-ignore]
    async def reads() -> str:
        """Return a read-only result."""
        return "ok"

    @mcp.tool(tags={"delete"})  # type: ignore[untyped-decorator, unused-ignore]
    async def deletes() -> str:
        """Pretend to perform a destructive action."""
        return "deleted"

    @mcp.tool  # type: ignore[untyped-decorator, unused-ignore]
    async def untagged() -> str:
        """Return a value from an untagged tool — always exposed."""
        return "untagged"

    @mcp.resource("data://thing/{thing_id}", tags={"query"})  # type: ignore[untyped-decorator, unused-ignore]
    async def thing(thing_id: str) -> str:
        """Return a read-only resource value for the given id."""
        return f"thing:{thing_id}"

    @mcp.prompt(name="suggest", tags={"query"})  # type: ignore[untyped-decorator, unused-ignore]
    def suggest() -> str:
        """Return a sample prompt template."""
        return "Pick something."

    mcp.add_middleware(TagFilterMiddleware(lambda: allowed, build_tag_lookup(mcp)))
    return mcp


async def test_listing_filters_disabled_tools() -> None:
    """A tool whose tags are all disabled doesn't appear in tools/list."""
    mcp = _build_server(allowed={"query"})
    async with Client(mcp) as client:
        names = {t.name for t in await client.list_tools()}
    assert "reads" in names
    assert "untagged" in names
    assert "deletes" not in names


async def test_call_disabled_tool_blocked() -> None:
    """
    A client cannot bypass the listing filter by calling the disabled tool by name.

    FastMCP's ``call_tool`` raises ``ToolError`` for any server-side rejection
    (the middleware re-raises ``NotFoundError`` as a tool-call failure).
    Pinning that type (rather than ``Exception``) keeps a future bug that
    raises e.g. ``TypeError`` from a wrong call signature from being
    silently masked.
    """
    mcp = _build_server(allowed={"query"})
    async with Client(mcp) as client:
        with pytest.raises(ToolError):
            await client.call_tool("deletes", {})


async def test_call_enabled_tool_works() -> None:
    """An enabled tool runs normally with the middleware in place."""
    mcp = _build_server(allowed={"query"})
    async with Client(mcp) as client:
        result = await client.call_tool("reads", {})
    text_blocks = [c for c in result.content if hasattr(c, "text")]
    assert any("ok" in c.text for c in text_blocks)


async def test_untagged_tool_always_callable() -> None:
    """Tools without tags are infrastructure and remain callable regardless of permissions."""
    mcp = _build_server(allowed=set())
    async with Client(mcp) as client:
        result = await client.call_tool("untagged", {})
    text_blocks = [c for c in result.content if hasattr(c, "text")]
    assert any("untagged" in c.text for c in text_blocks)


async def test_disabled_resource_blocked_on_read() -> None:
    """
    Reading a disabled resource by URI raises rather than silently succeeding.

    ``read_resource`` lifts server errors to ``McpError`` (the MCP SDK's own
    JSON-RPC error envelope class), not to ``ToolError`` — different transport
    path from ``call_tool``.
    """
    mcp = _build_server(allowed=set())
    async with Client(mcp) as client:
        with pytest.raises(McpError):
            await client.read_resource("data://thing/42")


async def test_template_resource_read_via_concrete_uri() -> None:
    """
    A concrete URI matched by a template resource is readable when its tag is enabled.

    The middleware lookup must fall back from ``get_resource`` (statically
    registered URIs only) to ``get_resource_template`` (URI-template matching);
    otherwise every ``@mcp.resource("scheme://{var}")``-backed URI gets blocked
    as not-found even though the tag is enabled.
    """
    mcp = _build_server(allowed={"query"})
    async with Client(mcp) as client:
        contents = await client.read_resource("data://thing/42")
    text_blocks = [c for c in contents if hasattr(c, "text")]
    assert any("thing:42" in c.text for c in text_blocks)


async def test_disabled_prompt_blocked_on_get() -> None:
    """Getting a disabled prompt by name raises ``McpError`` (RPC envelope)."""
    mcp = _build_server(allowed=set())
    async with Client(mcp) as client:
        with pytest.raises(McpError):
            await client.get_prompt("suggest", {})
