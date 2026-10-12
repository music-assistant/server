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
from unittest.mock import AsyncMock

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
async def test_runtime_has_three_tools_and_preserves_resources_and_prompts(
    mock_mass: MagicMock, mock_config: MagicMock
) -> None:
    """The runtime retains non-tool surfaces beside exactly three meta-tools."""
    from fastmcp import Client  # noqa: PLC0415

    from music_assistant.providers.fastmcp_server.server import MCPServerRuntime  # noqa: PLC0415

    # ``register_dynamic_route`` must return a callable; the smoke test does not
    # need real HTTP transport — Client(mcp) talks to the in-memory FastMCP root.
    mock_config._values["require_auth"] = False
    runtime = MCPServerRuntime(mock_mass, mock_config, _stub_logger())
    # Pretend the bridge mounted; we exercise the FastMCP root directly via in-memory Client.
    await runtime.start()
    try:
        async with Client(runtime._mcp) as client:
            assert {tool.name for tool in await client.list_tools()} == {
                "search_tools",
                "get_tool_schema",
                "call_tool",
            }
            assert {str(item.uriTemplate) for item in await client.list_resource_templates()} >= {
                "catalog://commands{?cursor,limit}",
                "player://{player_id}",
                "queue://{queue_id}",
            }
            assert await client.list_prompts()
    finally:
        await runtime.stop()


async def test_runtime_metadata_advertises_all_stable_capabilities(
    mock_mass: MagicMock,
    mock_config: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Protected-resource metadata is complete and never request-policy filtered."""
    from music_assistant.providers.fastmcp_server import http_bridge  # noqa: PLC0415
    from music_assistant.providers.fastmcp_server.capabilities import Capability  # noqa: PLC0415
    from music_assistant.providers.fastmcp_server.server import MCPServerRuntime  # noqa: PLC0415

    async def unmount() -> None:
        return None

    mount = AsyncMock(return_value=unmount)
    well_known = AsyncMock(return_value=lambda: None)
    monkeypatch.setattr(http_bridge, "mount_into_mass", mount)
    monkeypatch.setattr(http_bridge, "mount_well_known", well_known)
    runtime = MCPServerRuntime(mock_mass, mock_config, _stub_logger())

    await runtime.start()
    try:
        await_args = well_known.await_args
        assert await_args is not None
        kwargs = await_args.kwargs
        assert set(kwargs["scopes_supported"]()) == {str(capability) for capability in Capability}
        assert len(kwargs["scopes_supported"]()) == 26
    finally:
        await runtime.stop()


def _stub_logger() -> object:
    import logging  # noqa: PLC0415

    return logging.getLogger("fastmcp_server.smoke")
