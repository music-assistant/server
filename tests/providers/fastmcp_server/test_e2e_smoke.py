"""
End-to-end smoke test: build the runtime in-memory and exercise it via FastMCP Client.

This test is the only one that depends on the ``fastmcp`` package being
installed and on Music Assistant model imports working — the rest of the
suite uses mocks. Skipped automatically if either is unavailable.
"""
# mypy: disable-error-code="arg-type, no-untyped-def, type-arg, assignment, operator, misc"

from __future__ import annotations

import importlib.util
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from unittest.mock import MagicMock


_HAVE_FASTMCP = importlib.util.find_spec("fastmcp") is not None
_HAVE_MA = importlib.util.find_spec("music_assistant") is not None
_HAVE_MA_MODELS = importlib.util.find_spec("music_assistant_models") is not None


@pytest.mark.skipif(
    not (_HAVE_FASTMCP and _HAVE_MA and _HAVE_MA_MODELS),
    reason="needs fastmcp + music_assistant + music_assistant_models installed",
)
@pytest.mark.asyncio
async def test_runtime_lists_namespaced_tools(mock_mass: MagicMock, mock_config: MagicMock) -> None:
    """``MCPServerRuntime`` builds without errors and exposes namespaced tools."""
    from fastmcp import Client  # noqa: PLC0415

    from music_assistant.providers.fastmcp_server.server import MCPServerRuntime  # noqa: PLC0415

    # ``register_dynamic_route`` must return a callable; the smoke test does not
    # need real HTTP transport — Client(mcp) talks to the in-memory FastMCP root.
    runtime = MCPServerRuntime(mock_mass, mock_config, _stub_logger())
    # Pretend the bridge mounted; we exercise the FastMCP root directly via in-memory Client.
    await runtime.start()
    try:
        async with Client(runtime._mcp) as client:
            tools = await client.list_tools()
            names = {t.name for t in tools}
            # 4 query tags enabled by default → tools from library + queue + players + metadata
            assert any(name.startswith("library_") for name in names), names
            assert any(name.startswith("queue_") for name in names), names
            # Mutation-only namespaces should not appear under default config
            assert not any(name.startswith("volume_") for name in names), names
    finally:
        await runtime.stop()


def _stub_logger() -> object:
    import logging  # noqa: PLC0415

    return logging.getLogger("fastmcp_server.smoke")
