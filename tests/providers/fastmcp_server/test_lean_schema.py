"""
Tests for the lean-output-schema toggle on the config/debug namespaces.

Spec: ``specs/done/0009-tool-schema-context-budget-policy.md``.

When the lean toggle is on, the gated admin namespaces (config, debug) register
their tools without an ``outputSchema`` so the per-turn tool-schema footprint
shrinks for MCP hosts that lack tool-search deferred loading. The typed return
value must still reach the client as JSON text, and annotations must be intact.
"""
# mypy: disable-error-code="arg-type, no-untyped-def, type-arg, assignment, misc, union-attr"

from __future__ import annotations

import json
from typing import Any

from fastmcp import Client, FastMCP

from music_assistant.providers.fastmcp_server.tools import build_config_server, build_debug_server


def _mount(mock_mass: Any, *, lean_schema: bool) -> FastMCP:
    """Mount config + debug exactly as MCPServerRuntime does, with the lean flag."""
    mcp: FastMCP = FastMCP(name="test")
    mcp.mount(build_config_server(mock_mass, lean_schema=lean_schema), namespace="config")
    mcp.mount(build_debug_server(mock_mass, lean_schema=lean_schema), namespace="debug")
    return mcp


async def _tools(server: FastMCP) -> dict[str, Any]:
    async with Client(server) as client:
        return {t.name: t for t in await client.list_tools()}


async def test_default_keeps_output_schema(mock_mass: Any) -> None:
    """With the lean flag off (default), config/debug tools keep their outputSchema."""
    tools = await _tools(_mount(mock_mass, lean_schema=False))
    with_schema = [n for n, t in tools.items() if t.outputSchema]
    assert with_schema, "expected config/debug tools to carry an outputSchema by default"


async def test_lean_schema_omits_output_schema(mock_mass: Any) -> None:
    """With the lean flag on, every config/debug tool omits its outputSchema."""
    tools = await _tools(_mount(mock_mass, lean_schema=True))
    assert tools, "expected config/debug tools to be mounted"
    offenders = [n for n, t in tools.items() if t.outputSchema]
    assert not offenders, f"these tools still carry an outputSchema in lean mode: {offenders}"


async def test_lean_schema_preserves_annotations(mock_mass: Any) -> None:
    """Dropping outputSchema must not strip the required title/hint annotations."""
    tools = await _tools(_mount(mock_mass, lean_schema=True))
    for name, tool in tools.items():
        assert tool.annotations is not None, f"{name}: lost annotations in lean mode"
        assert tool.annotations.title, f"{name}: lost title in lean mode"


async def test_lean_schema_reduces_total_schema_bytes(mock_mass: Any) -> None:
    """
    Lean mode is a real context saving: serialized tool schemas shrink ≥25%.

    This doubles as the budget guard — it fails loudly if a future refactor
    inlines the output shape somewhere the toggle does not reach.
    """

    async def total_bytes(*, lean_schema: bool) -> int:
        tools = await _tools(_mount(mock_mass, lean_schema=lean_schema))
        return sum(
            len(json.dumps(t.model_dump(exclude_none=True, by_alias=True), default=list))
            for t in tools.values()
        )

    default_bytes = await total_bytes(lean_schema=False)
    lean_bytes = await total_bytes(lean_schema=True)
    assert lean_bytes < default_bytes * 0.75, (
        f"expected ≥25% schema shrink, got {default_bytes} → {lean_bytes} "
        f"({100 * lean_bytes // default_bytes}% of original)"
    )


async def test_lean_schema_tool_still_returns_data(mock_mass: Any) -> None:
    """A lean tool still delivers its dataclass payload as JSON text content."""
    server = _mount(mock_mass, lean_schema=True)
    async with Client(server) as client:
        result = await client.call_tool("debug_list_package_versions", {})
    text = "".join(block.text for block in result.content if hasattr(block, "text"))
    assert "fastmcp" in text, f"expected package data in text content, got: {text!r}"
