"""Sanity tests for invariants in ``provider.constants``."""

from __future__ import annotations

from music_assistant.providers.fastmcp_server.constants import (
    CONF_META_TOOL_DISCOVERY,
    HOT_SWAPPABLE_KEYS,
    PERMISSION_KEYS,
    RESOURCE_KEYS,
)


def test_permission_keys_count() -> None:
    """26 permission keys: 4 verbs x 4 categories + 5 debug tags + 5 config tags."""
    assert len(PERMISSION_KEYS) == 26


def test_resource_keys_count() -> None:
    """3 resource toggles."""
    assert len(RESOURCE_KEYS) == 3


def test_hot_swappable_includes_perm_resource_and_meta_keys() -> None:
    """Hot-swappable set is exactly this union — anything else triggers a runtime restart."""
    assert PERMISSION_KEYS | RESOURCE_KEYS | {CONF_META_TOOL_DISCOVERY} == HOT_SWAPPABLE_KEYS


def test_no_overlap_perm_resource() -> None:
    """Permission and resource key sets don't overlap (cleanly partitioned)."""
    assert PERMISSION_KEYS.isdisjoint(RESOURCE_KEYS)
