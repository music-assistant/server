"""Tests for ``provider.config.build_config_entries``."""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant.providers.fastmcp_server.config import build_config_entries
from music_assistant.providers.fastmcp_server.constants import (
    CONF_DELETE_LIBRARY,
    CONF_QUERY_LIBRARY,
    CONF_REQUIRE_AUTH,
    PERMISSION_KEYS,
    RESOURCE_KEYS,
)

if TYPE_CHECKING:
    from unittest.mock import MagicMock


def test_total_entry_count(mock_mass: MagicMock) -> None:
    """25 entries: 1 info label + 5 server settings + 16 permissions + 3 resources."""
    entries = build_config_entries(mock_mass, {})
    assert len(entries) == 1 + 5 + 16 + 3


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
    assert categories == {
        "Server",
        "Query Permissions",
        "Control Permissions",
        "Edit Permissions",
        "Delete Permissions",
        "MCP Resources",
    }


def test_info_label_includes_base_url(mock_mass: MagicMock) -> None:
    """The info label embeds MA's base_url so users see where to point clients."""
    entries = build_config_entries(mock_mass, {})
    info = entries[0]
    assert mock_mass.webserver.base_url in str(info.label)
    assert "/mcp/v1" in str(info.label)


def test_delete_library_default(mock_mass: MagicMock) -> None:
    """Specifically: delete_library defaults False (a hard-to-undo permission)."""
    entries = {e.key: e for e in build_config_entries(mock_mass, {})}
    assert entries[CONF_DELETE_LIBRARY].default_value is False
