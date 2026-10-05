"""
End-to-end test for the permission-tag hot-swap on a live FastMCP root.

``MCPServerRuntime.apply_permission_change`` has unit-level routing tests
(in ``test_apply_permission_change.py``) and a tag-filter middleware test
(in ``test_middleware.py``), but **no integration test** that mounts a real
FastMCP root, flips a permission via the runtime, and asserts the visible
tool surface actually changed. An inverted ``permission_only`` predicate or
a stale closure capture would slip through every existing unit test.

This file builds a runtime whose tag-set is mutated by
``apply_permission_change`` and verifies that the in-memory ``Client``'s
``list_tools`` reflects the new set **without** a stop/start cycle.
"""
# mypy: disable-error-code="arg-type, no-untyped-def, type-arg, assignment, operator, misc, attr-defined"

from __future__ import annotations

import logging
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastmcp import Client, FastMCP

from music_assistant.providers.fastmcp_server.middleware import TagFilterMiddleware
from music_assistant.providers.fastmcp_server.server import MCPServerRuntime, build_tag_lookup


def _build_runtime_with_mounted_server(
    mock_mass: MagicMock, mock_config: MagicMock
) -> tuple[MCPServerRuntime, FastMCP]:
    """
    Construct a real ``MCPServerRuntime`` with a small FastMCP mounted.

    We skip the full ``start()`` (which would mount into MA's webserver) and
    build the FastMCP root by hand — same shape as the production start path,
    minus the ASGI / route registration that has nothing to do with the
    hot-swap behaviour under test.
    """
    from music_assistant.providers.fastmcp_server.tags import enabled_tags  # noqa: PLC0415
    from music_assistant.providers.fastmcp_server.tools import (  # noqa: PLC0415
        build_library_server,
        build_volume_server,
    )

    runtime = MCPServerRuntime(mock_mass, mock_config, logging.getLogger("t"))

    mcp: FastMCP = FastMCP(name="hotswap-test")
    mcp.mount(build_library_server(mock_mass), namespace="library")
    mcp.mount(build_volume_server(mock_mass), namespace="volume")
    runtime._mcp = mcp

    # Hand-wire the same middleware closure that ``MCPServerRuntime.start``
    # would install — mutating ``runtime._allowed_tags`` after this point
    # should change the visible tool set on the next ``list_tools`` call.
    runtime._allowed_tags = {str(t) for t in enabled_tags(mock_config)}
    mcp.add_middleware(TagFilterMiddleware(lambda: runtime._allowed_tags, build_tag_lookup(mcp)))
    return runtime, mcp


def _set_config_values(cfg: MagicMock, **overrides: Any) -> None:
    """Mutate the ``_values`` dict on a ``mock_config`` fixture in place."""
    cfg._values.update(overrides)


@pytest.mark.asyncio
async def test_hot_swap_makes_disabled_tool_visible_without_restart(
    mock_mass: MagicMock, mock_config: MagicMock
) -> None:
    """
    Enabling a permission via ``apply_permission_change`` exposes its tools.

    With ``control_volume=False`` (the default in ``mock_config``), the
    ``volume_*`` tools are invisible to clients. Flipping the bit to True
    via ``apply_permission_change(changed_keys={"control_volume"})`` must
    make them visible on the next ``list_tools`` — without any ``stop()``
    or ``start()`` call.
    """
    runtime, mcp = _build_runtime_with_mounted_server(mock_mass, mock_config)

    async with Client(mcp) as client:
        names_before = {t.name for t in await client.list_tools()}
        assert not any(n.startswith("volume_") for n in names_before), (
            f"volume tools should be hidden by default; got names={names_before!r}"
        )
        # library tools are enabled by default, so at least one should show up.
        assert any(n.startswith("library_") for n in names_before), names_before

    # Flip the permission ON and dispatch the hot-swap.
    _set_config_values(mock_config, control_volume=True)
    await runtime.apply_permission_change(mock_config, changed_keys={"control_volume"})

    async with Client(mcp) as client:
        names_after = {t.name for t in await client.list_tools()}
    assert any(n.startswith("volume_") for n in names_after), (
        f"control_volume hot-swap did not expose volume_* tools; got names={names_after!r}"
    )


@pytest.mark.asyncio
async def test_hot_swap_hides_previously_visible_tool(
    mock_mass: MagicMock, mock_config: MagicMock
) -> None:
    """
    The reverse direction also works: disabling a permission hides its tools.

    Start with ``query_library=True`` (default), confirm library_* is visible,
    flip to ``False``, confirm it's hidden on the next list — same FastMCP
    root, no restart.
    """
    runtime, mcp = _build_runtime_with_mounted_server(mock_mass, mock_config)

    async with Client(mcp) as client:
        names_before = {t.name for t in await client.list_tools()}
        assert any(n.startswith("library_") for n in names_before), names_before

    _set_config_values(mock_config, query_library=False)
    await runtime.apply_permission_change(mock_config, changed_keys={"query_library"})

    async with Client(mcp) as client:
        names_after = {t.name for t in await client.list_tools()}
    assert not any(n.startswith("library_") for n in names_after), (
        f"hot-swap to query_library=False did not hide library_* tools; got {names_after!r}"
    )
