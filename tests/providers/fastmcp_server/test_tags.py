"""Tests for the tag enum and config-to-tag mapping."""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant.providers.fastmcp_server.constants import (
    CONF_CONFIG_READ,
    CONF_CONFIG_WRITE_CORE,
    CONF_CONFIG_WRITE_PLAYER,
    CONF_CONFIG_WRITE_PROVIDER,
    CONF_CONFIG_WRITE_SECRET,
    CONF_CONTROL_PLAYBACK,
    CONF_DEBUG_EVENTS,
    CONF_DEBUG_INSPECT,
    CONF_DEBUG_LOGS,
    CONF_DEBUG_PROVIDERS,
    CONF_DEBUG_RELOAD,
    CONF_DELETE_FAVORITES,
    CONF_QUERY_LIBRARY,
    PERMISSION_KEYS,
)
from music_assistant.providers.fastmcp_server.tags import CONFIG_TO_TAG, Tag, enabled_tags

if TYPE_CHECKING:
    from unittest.mock import MagicMock


def test_config_to_tag_is_total() -> None:
    """Core permission keys have unique tags in CONFIG_TO_TAG (16 core + 5 debug + 5 config)."""
    assert set(PERMISSION_KEYS).issubset(set(CONFIG_TO_TAG))
    assert (
        len({v for k, v in CONFIG_TO_TAG.items() if k in PERMISSION_KEYS})
        == len(PERMISSION_KEYS)
        == 26
    )


def test_tag_enum_values_are_namespaced() -> None:
    """Tag values look like ``<verb>:<category>``."""
    for tag in Tag:
        assert ":" in tag.value
        verb, _, _ = tag.value.partition(":")
        assert verb in {"query", "control", "edit", "delete", "debug", "config"}


def test_enabled_tags_defaults(mock_config: MagicMock) -> None:
    """With default config (4 reads on, all mutations off), only 4 query tags surface."""
    tags = enabled_tags(mock_config)
    assert tags == {
        Tag.QUERY_LIBRARY,
        Tag.QUERY_QUEUE,
        Tag.QUERY_PLAYERS,
        Tag.QUERY_METADATA,
    }


def test_enabled_tags_toggle(mock_config: MagicMock) -> None:
    """Flipping a single bool flips exactly one tag."""
    base = enabled_tags(mock_config)
    mock_config._values[CONF_CONTROL_PLAYBACK] = True
    after = enabled_tags(mock_config)
    assert after - base == {Tag.CONTROL_PLAYBACK}
    assert base - after == set()


def test_enabled_tags_all_off(mock_config: MagicMock) -> None:
    """When every permission is off, the result is empty."""
    for key in PERMISSION_KEYS:
        mock_config._values[key] = False
    assert enabled_tags(mock_config) == set()


def test_enabled_tags_query_library_off_drops_only_one(mock_config: MagicMock) -> None:
    """Disabling one specific permission only drops that tag."""
    mock_config._values[CONF_QUERY_LIBRARY] = False
    tags = enabled_tags(mock_config)
    assert Tag.QUERY_LIBRARY not in tags
    assert Tag.QUERY_QUEUE in tags
    assert Tag.QUERY_PLAYERS in tags
    assert Tag.QUERY_METADATA in tags


def test_delete_tags_namespaced() -> None:
    """Sanity: delete-family tags use the ``delete:`` prefix."""
    assert CONFIG_TO_TAG[CONF_DELETE_FAVORITES].value.startswith("delete:")


def test_debug_tags_present_in_enum() -> None:
    """Debug tags exist in the Tag enum with correct string values."""
    assert Tag.DEBUG_INSPECT.value == "debug:inspect"
    assert Tag.DEBUG_LOGS.value == "debug:logs"
    assert Tag.DEBUG_EVENTS.value == "debug:events"
    assert Tag.DEBUG_PROVIDERS.value == "debug:providers"
    assert Tag.DEBUG_RELOAD.value == "debug:reload"


def test_debug_config_keys_map_to_tags() -> None:
    """Debug config keys map to their corresponding Tag enum members."""
    assert CONFIG_TO_TAG[CONF_DEBUG_INSPECT] is Tag.DEBUG_INSPECT
    assert CONFIG_TO_TAG[CONF_DEBUG_LOGS] is Tag.DEBUG_LOGS
    assert CONFIG_TO_TAG[CONF_DEBUG_EVENTS] is Tag.DEBUG_EVENTS
    assert CONFIG_TO_TAG[CONF_DEBUG_PROVIDERS] is Tag.DEBUG_PROVIDERS
    assert CONFIG_TO_TAG[CONF_DEBUG_RELOAD] is Tag.DEBUG_RELOAD


def test_enabled_tags_excludes_debug_when_off(mock_config: MagicMock) -> None:
    """Debug tags are excluded from enabled tags when debug permissions are off."""
    tags = enabled_tags(mock_config)
    assert Tag.DEBUG_INSPECT not in tags
    assert Tag.DEBUG_LOGS not in tags
    assert Tag.DEBUG_EVENTS not in tags
    assert Tag.DEBUG_PROVIDERS not in tags
    assert Tag.DEBUG_RELOAD not in tags


def test_config_tags_present_in_enum() -> None:
    """Config tags exist in the Tag enum with correct string values."""
    assert Tag.CONFIG_READ.value == "config:read"
    assert Tag.CONFIG_WRITE_PROVIDER.value == "config:write:provider"
    assert Tag.CONFIG_WRITE_CORE.value == "config:write:core"
    assert Tag.CONFIG_WRITE_PLAYER.value == "config:write:player"
    assert Tag.CONFIG_WRITE_SECRET.value == "config:write:secret"


def test_config_keys_map_to_tags() -> None:
    """Config config keys map to their corresponding Tag enum members."""
    assert CONFIG_TO_TAG[CONF_CONFIG_READ] is Tag.CONFIG_READ
    assert CONFIG_TO_TAG[CONF_CONFIG_WRITE_PROVIDER] is Tag.CONFIG_WRITE_PROVIDER
    assert CONFIG_TO_TAG[CONF_CONFIG_WRITE_CORE] is Tag.CONFIG_WRITE_CORE
    assert CONFIG_TO_TAG[CONF_CONFIG_WRITE_PLAYER] is Tag.CONFIG_WRITE_PLAYER
    assert CONFIG_TO_TAG[CONF_CONFIG_WRITE_SECRET] is Tag.CONFIG_WRITE_SECRET
