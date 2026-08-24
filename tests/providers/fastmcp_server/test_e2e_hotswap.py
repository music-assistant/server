"""End-to-end permission hot-swap tests for the retained resource surface."""
# mypy: disable-error-code="arg-type, no-untyped-def, type-arg, assignment, operator, misc, attr-defined"

from __future__ import annotations

import logging
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastmcp import Client, FastMCP
from mcp.shared.exceptions import McpError

from music_assistant.providers.fastmcp_server.capabilities import Capability
from music_assistant.providers.fastmcp_server.constants import (
    CONF_DEFAULT_POLICY,
    CONF_REQUIRE_AUTH,
)
from music_assistant.providers.fastmcp_server.policy_config import policy_mode_key
from music_assistant.providers.fastmcp_server.resources import register_resources
from music_assistant.providers.fastmcp_server.server import MCPServerRuntime


def _build_runtime_with_resources(
    mock_mass: MagicMock, mock_config: MagicMock
) -> tuple[MCPServerRuntime, FastMCP]:
    """Build the production resource/tag shape without mounting an HTTP route."""
    runtime = MCPServerRuntime(mock_mass, mock_config, logging.getLogger("t"))
    mcp = FastMCP(name="hotswap-test")
    register_resources(mcp, mock_mass, mock_config)
    runtime._mcp = mcp
    runtime._apply_tag_filter(mcp)
    return runtime, mcp


def _set_config_values(config: MagicMock, **overrides: Any) -> None:
    """Mutate the test provider config in place, matching MA update semantics."""
    config._values.update(overrides)


async def test_hot_swap_makes_disabled_resource_visible_without_restart(
    mock_mass: MagicMock, mock_config: MagicMock
) -> None:
    """Enabling a permission exposes its already-registered resource templates."""
    _set_config_values(
        mock_config,
        **{
            CONF_DEFAULT_POLICY: "Custom",
            policy_mode_key(Capability.QUERY_LIBRARY): "allow",
        },
    )
    runtime, mcp = _build_runtime_with_resources(mock_mass, mock_config)

    async with Client(mcp) as client:
        before = {template.uriTemplate for template in await client.list_resource_templates()}
    assert "player://{player_id}" not in before

    _set_config_values(mock_config, **{policy_mode_key(Capability.QUERY_PLAYERS): "allow"})
    await runtime.apply_config_change(
        mock_config,
        changed_keys={policy_mode_key(Capability.QUERY_PLAYERS)},
    )

    async with Client(mcp) as client:
        after = {template.uriTemplate for template in await client.list_resource_templates()}
    assert "player://{player_id}" in after


async def test_hot_swap_hides_previously_visible_resource(
    mock_mass: MagicMock, mock_config: MagicMock
) -> None:
    """Disabling library query permission hides resources on the same FastMCP root."""
    runtime, mcp = _build_runtime_with_resources(mock_mass, mock_config)

    async with Client(mcp) as client:
        before = {template.uriTemplate for template in await client.list_resource_templates()}
    assert "library://track/{track_id}" in before

    _set_config_values(
        mock_config,
        **{
            CONF_DEFAULT_POLICY: "Custom",
            policy_mode_key(Capability.QUERY_LIBRARY): "deny",
        },
    )
    await runtime.apply_config_change(
        mock_config,
        changed_keys={policy_mode_key(Capability.QUERY_LIBRARY)},
    )

    async with Client(mcp) as client:
        after = {template.uriTemplate for template in await client.list_resource_templates()}
    assert "library://track/{track_id}" not in after


async def test_auth_off_runtime_resources_use_global_default_policy(
    mock_mass: MagicMock,
    mock_config: MagicMock,
) -> None:
    """Production runtime wiring uses auth-off default policy for resource reads."""
    _set_config_values(
        mock_config,
        **{
            CONF_REQUIRE_AUTH: False,
            CONF_DEFAULT_POLICY: "Custom",
            policy_mode_key(Capability.QUERY_LIBRARY): "allow",
        },
    )
    runtime = MCPServerRuntime(mock_mass, mock_config, logging.getLogger("t"))
    mcp = FastMCP(name="auth-off-resource-policy")
    register_resources(mcp, mock_mass, mock_config)
    runtime._mcp = mcp
    runtime._apply_tag_filter(mcp)

    async with Client(mcp) as client:
        await client.read_resource("library://track/17")

        for mode in ("deny", "confirm"):
            _set_config_values(
                mock_config,
                **{policy_mode_key(Capability.QUERY_LIBRARY): mode},
            )
            runtime._refresh_policy_resolver()
            templates = {
                str(template.uriTemplate) for template in await client.list_resource_templates()
            }
            assert "library://track/{track_id}" not in templates
            with pytest.raises(McpError):
                await client.read_resource("library://track/17")
