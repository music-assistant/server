"""Tests for ``provider.config.build_config_entries``."""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant.providers.fastmcp_server.config import build_config_entries
from music_assistant.providers.fastmcp_server.constants import (
    CONF_CONFIG_READ,
    CONF_CONFIG_WRITE_CORE,
    CONF_CONFIG_WRITE_PLAYER,
    CONF_CONFIG_WRITE_PROVIDER,
    CONF_CONFIG_WRITE_SECRET,
    CONF_DEBUG_EVENT_BUFFER_CAPACITY,
    CONF_DEBUG_EVENTS,
    CONF_DEBUG_INSPECT,
    CONF_DEBUG_LOGS,
    CONF_DEBUG_PROVIDERS,
    CONF_DEBUG_RELOAD,
    CONF_DELETE_LIBRARY,
    CONF_MOUNT_PATH,
    CONF_QUERY_LIBRARY,
    CONF_REQUIRE_AUTH,
    PERMISSION_KEYS,
    RESOURCE_KEYS,
)

if TYPE_CHECKING:
    from unittest.mock import MagicMock


def test_total_entry_count(mock_mass: MagicMock) -> None:
    """40 entries: 1 info label + 1 connect-wizard action + 8 server + 16 perms + 3 resources + 6 debug + 5 config."""
    entries = build_config_entries(mock_mass, {})
    assert len(entries) == 1 + 1 + 8 + 16 + 3 + 6 + 5


def test_all_permission_keys_present(mock_mass: MagicMock) -> None:
    """Every permission key from PERMISSION_KEYS has a matching ConfigEntry."""
    entries = build_config_entries(mock_mass, {})
    keys = {e.key for e in entries}
    assert PERMISSION_KEYS.issubset(keys)
    assert RESOURCE_KEYS.issubset(keys)
    assert CONF_REQUIRE_AUTH in keys


def test_delete_keys_default_false(mock_mass: MagicMock) -> None:
    """All delete-family permissions default to False (least-privilege)."""
    entries = {e.key: e for e in build_config_entries(mock_mass, {})}
    mutation_prefixes = ("delete_", "control_", "edit_")
    for key in PERMISSION_KEYS:
        if key.startswith(mutation_prefixes):
            assert entries[key].default_value is False, f"{key} should default False"


def test_query_keys_default_true(mock_mass: MagicMock) -> None:
    """All query-family permissions default to True."""
    entries = {e.key: e for e in build_config_entries(mock_mass, {})}
    assert entries[CONF_QUERY_LIBRARY].default_value is True


def test_categories_match_pr2889_ux(mock_mass: MagicMock) -> None:
    """Categories mirror upstream PR #2889 grouping for familiarity at review time."""
    entries = build_config_entries(mock_mass, {})
    categories = {getattr(e, "category", None) for e in entries if getattr(e, "category", None)}
    # ``Generic`` comes from the Connect Wizard ACTION entry, which mirrors the
    # Spotify provider's ``CONF_ACTION_AUTH`` button (no explicit category).
    assert categories == {
        "server",
        "query_permissions",
        "control_permissions",
        "edit_permissions",
        "delete_permissions",
        "mcp_resources",
        "debug",
        "mcp_config",
        "generic",
    }


def test_info_label_includes_base_url(mock_mass: MagicMock) -> None:
    """The info label embeds MA's base_url so users see where to point clients."""
    entries = build_config_entries(mock_mass, {})
    info = entries[0]
    assert mock_mass.webserver.base_url in str(info.label)
    assert "/mcp/v1" in str(info.label)


def test_info_label_normalises_mount_path_without_leading_slash(
    mock_mass: MagicMock,
) -> None:
    """
    A user-typed ``mcp/v1`` (no leading slash) must still render a valid URL.

    Regression for upstream PR #3858 Copilot comment: the runtime normalises
    the mount path, but the info label did not — so the displayed endpoint
    glued the host to the path with no separator (``…:8095mcp/v1``).
    """
    entries = build_config_entries(mock_mass, {CONF_MOUNT_PATH: "mcp/v1"})
    label = str(entries[0].label)
    base_url = mock_mass.webserver.base_url
    assert f"{base_url}/mcp/v1" in label
    assert f"{base_url}mcp" not in label


def test_delete_library_default(mock_mass: MagicMock) -> None:
    """Specifically: delete_library defaults False (a hard-to-undo permission)."""
    entries = {e.key: e for e in build_config_entries(mock_mass, {})}
    assert entries[CONF_DELETE_LIBRARY].default_value is False


def test_debug_entries_present_with_off_defaults(mock_mass: MagicMock) -> None:
    """Debug ConfigEntries are present and default to off (least privilege)."""
    entries = {e.key: e for e in build_config_entries(mock_mass, {})}
    for key in (
        CONF_DEBUG_INSPECT,
        CONF_DEBUG_LOGS,
        CONF_DEBUG_EVENTS,
        CONF_DEBUG_PROVIDERS,
        CONF_DEBUG_RELOAD,
    ):
        assert key in entries, f"missing {key}"
        assert entries[key].default_value is False, f"{key} must be off by default"
        assert entries[key].category == "debug"

    cap = entries[CONF_DEBUG_EVENT_BUFFER_CAPACITY]
    assert cap.default_value == 500
    assert cap.range == (50, 5000)


def test_config_entries_present_with_off_defaults(mock_mass: MagicMock) -> None:
    """Config ConfigEntries are present and default to off (least privilege)."""
    entries = {e.key: e for e in build_config_entries(mock_mass, {})}
    for key in (
        CONF_CONFIG_READ,
        CONF_CONFIG_WRITE_PROVIDER,
        CONF_CONFIG_WRITE_CORE,
        CONF_CONFIG_WRITE_PLAYER,
        CONF_CONFIG_WRITE_SECRET,
    ):
        assert key in entries, f"missing {key}"
        assert entries[key].default_value is False, f"{key} must be off by default"
        assert entries[key].category == "mcp_config"
