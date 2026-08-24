"""Tests for retained non-policy provider configuration entries."""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant.providers.fastmcp_server.config import build_config_entries
from music_assistant.providers.fastmcp_server.constants import (
    CONF_DEBUG_EVENT_BUFFER_CAPACITY,
    CONF_ENABLE_MCP_APP,
    CONF_REQUIRE_AUTH,
    CONF_RES_LIBRARY,
    CONF_RES_PLAYER,
    CONF_RES_PROMPTS,
    DEFAULT_MOUNT_PATH,
    RESOURCE_KEYS,
)

if TYPE_CHECKING:
    from unittest.mock import MagicMock


def test_retained_endpoint_resource_and_prompt_entries(mock_mass: MagicMock) -> None:
    """V2 keeps endpoint/auth/Origin/resource/prompt controls and event capacity."""
    entries = {entry.key: entry for entry in build_config_entries(mock_mass, DEFAULT_MOUNT_PATH)}

    assert CONF_REQUIRE_AUTH in entries
    assert entries.keys() >= RESOURCE_KEYS
    assert entries[CONF_RES_LIBRARY].default_value is True
    assert entries[CONF_RES_PLAYER].default_value is True
    assert entries[CONF_RES_PROMPTS].default_value is True
    assert entries[CONF_ENABLE_MCP_APP].default_value is False
    assert entries[CONF_ENABLE_MCP_APP].advanced is False
    capacity = entries[CONF_DEBUG_EVENT_BUFFER_CAPACITY]
    assert capacity.default_value == 500
    assert capacity.range == (50, 5000)


def test_info_label_includes_normalized_endpoint(mock_mass: MagicMock) -> None:
    """The runtime info label renders endpoint guidance instead of its structural key."""
    entries = build_config_entries(mock_mass, "mcp/v1")
    info = entries[0]

    assert info.key == "info_label"
    assert info.label == (
        f"MCP endpoint: {mock_mass.webserver.base_url}/mcp/v1\n"
        "Create tokens in Profile → Long-lived access tokens."
    )
    assert info.translation_params is None
