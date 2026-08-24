"""Sanity tests for v2 configuration key routing."""

from __future__ import annotations

from music_assistant.providers.fastmcp_server.constants import (
    CONF_DEFAULT_POLICY,
    RESOURCE_KEYS,
    is_hot_swappable_key,
    is_policy_key,
)


def test_v2_policy_and_resource_changes_are_hot_swappable() -> None:
    """Static and hashed dynamic policy keys hot-swap; endpoint keys restart."""
    assert is_policy_key(CONF_DEFAULT_POLICY)
    assert is_policy_key("policy_mode_debug_events")
    assert is_policy_key("policy_token_deadbeef")
    assert all(is_hot_swappable_key(key) for key in RESOURCE_KEYS)
    assert is_hot_swappable_key("policy_token_query_library_deadbeef")
    assert not is_hot_swappable_key("mount_path")


def test_v1_keys_are_not_recognized_as_policy_or_hot_swappable() -> None:
    """Stored legacy keys are inert under the breaking v2 contract."""
    for key in ("query_library", "dynamic_api_read", "require_confirmation"):
        assert not is_policy_key(key)
        assert not is_hot_swappable_key(key)
