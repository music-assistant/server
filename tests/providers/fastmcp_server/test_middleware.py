"""Tests for TagFilterMiddleware enforcement on direct invocation (C3)."""

from __future__ import annotations

import pytest

fastmcp = pytest.importorskip("fastmcp")

from fastmcp import Client, FastMCP  # noqa: E402

from music_assistant.providers.fastmcp_server.middleware import TagFilterMiddleware  # noqa: E402


def _build_server(allowed: set[str]) -> FastMCP:
    """Construct a FastMCP root with one tagged tool and the tag-filter middleware."""
    mcp: FastMCP = FastMCP(name="test-server")

    @mcp.tool(tags={"query"})
    async def reads() -> str:
        """Return a read-only result."""
        return "ok"

    @mcp.tool(tags={"delete"})
    async def deletes() -> str:
        """Pretend to perform a destructive action."""
        return "deleted"

    @mcp.tool
    async def untagged() -> str:
        """Return a value from an untagged tool — always exposed."""
        return "untagged"

    @mcp.resource("data://thing/{thing_id}", tags={"query"})
    async def thing(thing_id: str) -> str:
        """Return a read-only resource value for the given id."""
        return f"thing:{thing_id}"

    @mcp.prompt(name="suggest", tags={"query"})
    def suggest() -> str:
        """Return a sample prompt template."""
        return "Pick something."

    async def lookup(kind: str, key: str) -> set[str] | None:
        try:
            if kind == "tool":
                obj = await mcp.get_tool(key)
            elif kind == "resource":
                obj = await mcp.get_resource(key)
            elif kind == "prompt":
                obj = await mcp.get_prompt(key)
            else:
                return None
        except Exception:
            return None
        if obj is None:
            return None
        return {str(t) for t in (getattr(obj, "tags", None) or set())}

    mcp.add_middleware(TagFilterMiddleware(lambda: allowed, lookup))
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
    """A client cannot bypass the listing filter by calling the disabled tool by name."""
    mcp = _build_server(allowed={"query"})
    async with Client(mcp) as client:
        with pytest.raises(Exception):  # noqa: B017,PT011 - SDK wraps as ToolError or RPC error
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
    """Reading a disabled resource by URI raises rather than silently succeeding."""
    mcp = _build_server(allowed=set())
    async with Client(mcp) as client:
        with pytest.raises(Exception):  # noqa: B017,PT011
            await client.read_resource("data://thing/42")


async def test_disabled_prompt_blocked_on_get() -> None:
    """Getting a disabled prompt by name raises."""
    mcp = _build_server(allowed=set())
    async with Client(mcp) as client:
        with pytest.raises(Exception):  # noqa: B017,PT011
            await client.get_prompt("suggest", {})
