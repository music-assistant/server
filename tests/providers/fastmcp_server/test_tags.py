"""Tests for the tag enum and config-to-tag mapping."""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant.providers.fastmcp_server.constants import (
    CONF_CONTROL_PLAYBACK,
    CONF_DELETE_FAVORITES,
    CONF_QUERY_LIBRARY,
    PERMISSION_KEYS,
)
from music_assistant.providers.fastmcp_server.tags import CONFIG_TO_TAG, Tag, enabled_tags

if TYPE_CHECKING:
    from unittest.mock import MagicMock


def test_config_to_tag_is_total() -> None:
    """Every permission key has a unique tag, and counts match."""
    assert set(CONFIG_TO_TAG) == set(PERMISSION_KEYS)
    assert len(set(CONFIG_TO_TAG.values())) == len(PERMISSION_KEYS) == 16


def test_tag_enum_values_are_namespaced() -> None:
    """Tag values look like ``<verb>:<category>``."""
    for tag in Tag:
        assert ":" in tag.value
        verb, _, _ = tag.value.partition(":")
        assert verb in {"query", "control", "edit", "delete"}


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
