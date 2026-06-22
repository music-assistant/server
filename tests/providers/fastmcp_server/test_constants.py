"""Sanity tests for invariants in ``provider.constants``."""

from __future__ import annotations

from music_assistant.providers.fastmcp_server.constants import (
    HOT_SWAPPABLE_KEYS,
    META_KEYS,
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
    """Hot-swappable set is exactly the union of permission, resource, and meta keys — anything else triggers a runtime restart."""
    assert HOT_SWAPPABLE_KEYS == PERMISSION_KEYS | RESOURCE_KEYS | META_KEYS


def test_meta_keys_count() -> None:
    """1 meta-tool discovery config key."""
    assert len(META_KEYS) == 1


def test_no_overlap_perm_resource_meta() -> None:
    """Permission, resource, and meta key sets don't overlap (cleanly partitioned)."""
    assert PERMISSION_KEYS.isdisjoint(RESOURCE_KEYS)
    assert PERMISSION_KEYS.isdisjoint(META_KEYS)
    assert RESOURCE_KEYS.isdisjoint(META_KEYS)
