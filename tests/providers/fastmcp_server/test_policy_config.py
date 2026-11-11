"""V2 policy configuration parsing and dynamic-entry tests."""

from __future__ import annotations

from collections.abc import Mapping
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.config_entries import ProviderConfig
from music_assistant_models.enums import ProviderType
from music_assistant_models.errors import AuthenticationRequired

from music_assistant.providers.fastmcp_server.capabilities import Capability
from music_assistant.providers.fastmcp_server.config import build_config_entries
from music_assistant.providers.fastmcp_server.constants import (
    CONF_DEFAULT_POLICY,
    CONF_MANUAL_TOKEN_IDS,
    CONF_POLICY_TOKEN_SUFFIXES,
    DEFAULT_MOUNT_PATH,
)
from music_assistant.providers.fastmcp_server.policy import PolicyMode, PolicyProfile
from music_assistant.providers.fastmcp_server.policy_config import (
    build_policy_resolver,
    current_user_mcp_tokens,
    policy_event_buffer_enabled,
    policy_mode_key,
    policy_token_suffix,
    raw_provider_config_value,
    token_policy_key,
)


def _config(values: Mapping[str, object]) -> MagicMock:
    config = MagicMock()
    config.get_value.side_effect = lambda key, default=None: values.get(key, default)
    return config


def test_missing_and_malformed_v2_defaults_fail_closed() -> None:
    """Absent or malformed profile values resolve Safe queries, never admin."""
    missing = build_policy_resolver(_config({}))
    malformed = build_policy_resolver(_config({CONF_DEFAULT_POLICY: "typo-admin"}))

    assert missing.resolve(None).profile.value == "Safe queries"
    assert malformed.resolve(None).profile.value == "Safe queries"
    assert malformed.resolve(None).mode(Capability.CONFIG_WRITE_CORE) is PolicyMode.DENY


def test_legacy_read_only_value_resolves_safe_queries() -> None:
    """Existing installations retain their restrictive policy after the rename."""
    resolver = build_policy_resolver(_config({CONF_DEFAULT_POLICY: "Read-only"}))

    assert resolver.resolve(None).profile.value == "Safe queries"
    assert resolver.resolve(None).mode(Capability.QUERY_LIBRARY) is PolicyMode.ALLOW
    assert resolver.resolve(None).mode(Capability.CONFIG_READ) is PolicyMode.DENY


def test_provider_config_accepts_legacy_read_only_value(mock_mass: MagicMock) -> None:
    """MA persisted config can load the renamed profile's legacy value."""
    values = {CONF_DEFAULT_POLICY: "Read-only"}
    raw = {
        "values": values,
        "type": ProviderType.PLUGIN.value,
        "domain": "mcp_server",
        "instance_id": "mcp_server--legacy",
        "enabled": True,
    }
    config = cast(
        "ProviderConfig",
        ProviderConfig.parse(build_config_entries(mock_mass, DEFAULT_MOUNT_PATH), raw),
    )

    resolver = build_policy_resolver(config, raw_value_provider=values.get)
    assert resolver.resolve(None).profile is PolicyProfile.SAFE_QUERIES


def test_default_named_and_custom_policy_parsing() -> None:
    """Named profiles and explicit Custom modes compile into complete snapshots."""
    named = build_policy_resolver(_config({CONF_DEFAULT_POLICY: "Home control"}))
    custom = build_policy_resolver(
        _config(
            {
                CONF_DEFAULT_POLICY: "Custom",
                policy_mode_key(Capability.QUERY_LIBRARY): "allow",
                policy_mode_key(Capability.DEBUG_EVENTS): "confirm",
                policy_mode_key(Capability.CONFIG_WRITE_CORE): "not-a-mode",
            }
        )
    )

    assert named.resolve(None).profile is PolicyProfile.HOME_CONTROL
    assert custom.resolve(None).mode(Capability.QUERY_LIBRARY) is PolicyMode.ALLOW
    assert custom.resolve(None).mode(Capability.DEBUG_EVENTS) is PolicyMode.CONFIRM
    assert custom.resolve(None).mode(Capability.CONFIG_WRITE_CORE) is PolicyMode.DENY
    assert custom.resolve(None).mode(Capability.CONTROL_PLAYBACK) is PolicyMode.DENY


def test_override_manual_unknown_and_replacement_resolution() -> None:
    """Only active/manual exact token IDs receive their own selection."""
    revoked_id = "revoked-id"
    replacement_id = "replacement-id"
    manual_id = "foreign-manual-id"
    values = {
        CONF_DEFAULT_POLICY: "Safe queries",
        CONF_MANUAL_TOKEN_IDS: [manual_id],
        token_policy_key(revoked_id): "Trusted",
        token_policy_key(replacement_id): "Inherit",
        token_policy_key(manual_id): "Custom",
        policy_mode_key(Capability.CONTROL_PLAYBACK, manual_id): "allow",
    }
    resolver = build_policy_resolver(_config(values), active_token_ids={replacement_id})

    assert resolver.resolve(revoked_id).profile is PolicyProfile.SAFE_QUERIES
    assert resolver.resolve(replacement_id).profile is PolicyProfile.SAFE_QUERIES
    assert resolver.resolve("unknown-id").profile is PolicyProfile.SAFE_QUERIES
    assert resolver.resolve(manual_id).profile is PolicyProfile.CUSTOM
    assert resolver.resolve(manual_id).mode(Capability.CONTROL_PLAYBACK) is PolicyMode.ALLOW


def test_malformed_token_profile_fails_closed() -> None:
    """A corrupt override cannot silently broaden into Interactive admin."""
    token_id = "configured-id"
    resolver = build_policy_resolver(
        _config(
            {
                CONF_DEFAULT_POLICY: "Trusted",
                token_policy_key(token_id): "Interactive administrator",
            }
        ),
        active_token_ids={token_id},
    )

    assert resolver.resolve(token_id).profile is PolicyProfile.SAFE_QUERIES
    assert resolver.resolve(token_id).mode(Capability.SYSTEM_ADMIN) is PolicyMode.DENY


@pytest.mark.asyncio
async def test_current_user_discovery_uses_ma_apis_and_exact_prefix(mock_mass: MagicMock) -> None:
    """Discovery includes only current-user tokens with the exact MCP em-dash prefix."""
    current = SimpleNamespace(user_id="current-user")
    mock_mass.webserver.auth.get_current_user_info = AsyncMock(return_value=current)
    mock_mass.webserver.auth.get_user_tokens = AsyncMock(
        return_value=[
            SimpleNamespace(token_id="a", user_id="current-user", name="MCP — Claude"),
            SimpleNamespace(token_id="b", user_id="current-user", name="MCP - wrong dash"),
            SimpleNamespace(token_id="c", user_id="foreign-user", name="MCP — Foreign"),
            SimpleNamespace(token_id="d", user_id="current-user", name="Other client"),
        ]
    )

    tokens = await current_user_mcp_tokens(mock_mass)

    assert [(token.token_id, token.name) for token in tokens] == [("a", "MCP — Claude")]
    mock_mass.webserver.auth.get_user_tokens.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_current_user_discovery_treats_startup_without_identity_as_empty(
    mock_mass: MagicMock,
) -> None:
    """MA may request config entries during startup without an authenticated user."""
    mock_mass.webserver.auth.get_current_user_info = AsyncMock(
        side_effect=AuthenticationRequired("Not authenticated")
    )
    mock_mass.webserver.auth.get_user_tokens = AsyncMock()

    assert await current_user_mcp_tokens(mock_mass) == ()
    mock_mass.webserver.auth.get_user_tokens.assert_not_awaited()


def test_dynamic_entries_have_conditional_matrices_and_hashed_token_keys(
    mock_mass: MagicMock,
) -> None:
    """Each selector controls exactly one 26-capability Custom matrix."""
    raw_id = "token-id-must-not-appear"
    selector_key = token_policy_key(raw_id)
    debug_key = policy_mode_key(Capability.DEBUG_EVENTS, raw_id)
    stored_values = {
        CONF_POLICY_TOKEN_SUFFIXES: [policy_token_suffix(raw_id)],
        selector_key: "Custom",
        debug_key: "confirm",
    }
    entries = build_config_entries(
        mock_mass,
        DEFAULT_MOUNT_PATH,
        tokens=(SimpleNamespace(token_id=raw_id, name="MCP — Claude"),),
        manual_token_ids=("manual-foreign-id",),
        stored_value_provider=stored_values.get,
    )
    by_key = {entry.key: entry for entry in entries}

    assert CONF_DEFAULT_POLICY in by_key
    assert CONF_MANUAL_TOKEN_IDS in by_key
    assert by_key[CONF_MANUAL_TOKEN_IDS].multi_value is True
    assert selector_key in by_key
    assert by_key[CONF_DEFAULT_POLICY].advanced is False
    assert by_key[selector_key].advanced is False
    assert by_key[selector_key].label is None
    assert by_key[selector_key].description is None
    assert by_key[selector_key].translation_key == "policy_token"
    assert by_key[selector_key].translation_params == ["MCP — Claude"]
    assert by_key[selector_key].value == "Custom"
    assert by_key[debug_key].value == "confirm"
    assert by_key[CONF_POLICY_TOKEN_SUFFIXES].value == [policy_token_suffix(raw_id)]
    assert [(option.value, option.title) for option in by_key[CONF_DEFAULT_POLICY].options] == [
        ("Safe queries", None),
        ("Home control", None),
        ("Interactive admin", None),
        ("Trusted", None),
        ("Custom", None),
    ]
    assert [option.value for option in by_key[selector_key].options] == [
        "Inherit",
        "Safe queries",
        "Home control",
        "Interactive admin",
        "Trusted",
        "Custom",
    ]
    assert all(option.title is None for option in by_key[selector_key].options)
    assert raw_id not in selector_key
    assert token_policy_key(raw_id) == token_policy_key(raw_id)
    assert token_policy_key(raw_id) != token_policy_key("replacement-id")
    assert all(raw_id not in entry.key for entry in entries)

    default_matrix = [
        entry
        for entry in entries
        if entry.depends_on == CONF_DEFAULT_POLICY and entry.depends_on_value == "Custom"
    ]
    token_matrix = [
        entry
        for entry in entries
        if entry.depends_on == selector_key and entry.depends_on_value == "Custom"
    ]
    assert len(default_matrix) == len(Capability) == 26
    assert len(token_matrix) == len(Capability) == 26
    assert all(entry.advanced is True for entry in default_matrix)
    assert all(entry.advanced is True for entry in token_matrix)
    for capability in Capability:
        for entry in (
            by_key[policy_mode_key(capability)],
            by_key[policy_mode_key(capability, raw_id)],
        ):
            assert entry.label is None
            assert entry.description is None
            assert entry.translation_key == "policy_capability"
            assert entry.translation_params == [str(capability)]
            assert [(option.value, option.title) for option in entry.options] == [
                ("deny", None),
                ("allow", None),
                ("confirm", None),
            ]


def test_v1_entries_are_removed_even_if_stored_values_exist(mock_mass: MagicMock) -> None:
    """The breaking v2 UI ignores all legacy permission/risk/confirmation keys."""
    entries = build_config_entries(mock_mass, DEFAULT_MOUNT_PATH)
    keys = {entry.key for entry in entries}

    assert {
        "query_library",
        "debug_events",
        "config_write_core",
        "dynamic_api_read",
        "require_confirmation",
    }.isdisjoint(keys)


def test_actual_provider_config_roundtrip_preserves_exact_cold_token_policies(
    mock_mass: MagicMock,
) -> None:
    """A permanent suffix index makes undeclared hashed overrides cold-start durable."""
    readonly_id = "auto-readonly-token"
    debug_id = "auto-debug-token"
    replacement_id = "replacement-token"
    readonly_suffix = policy_token_suffix(readonly_id)
    debug_suffix = policy_token_suffix(debug_id)
    raw_values = {
        CONF_DEFAULT_POLICY: "Trusted",
        CONF_POLICY_TOKEN_SUFFIXES: [readonly_suffix, debug_suffix],
        token_policy_key(readonly_id): "Safe queries",
        token_policy_key(debug_id): "Custom",
        policy_mode_key(Capability.DEBUG_EVENTS, debug_id): "allow",
    }
    raw = {
        "values": raw_values,
        "type": ProviderType.PLUGIN.value,
        "domain": "mcp_server",
        "instance_id": "mcp_server--1",
        "enabled": True,
    }
    first_entries = build_config_entries(
        mock_mass,
        DEFAULT_MOUNT_PATH,
        tokens=(
            SimpleNamespace(token_id=readonly_id, name="MCP — restricted"),
            SimpleNamespace(token_id=debug_id, name="MCP — debug"),
        ),
    )
    first = cast("ProviderConfig", ProviderConfig.parse(first_entries, raw))
    persisted = first.to_raw()

    # A context-free cold parse cannot render either user's token rows. Only
    # the permanent hidden index survives on the ProviderConfig itself; exact
    # hashed values remain in MA's sanctioned raw store.
    cold = cast(
        "ProviderConfig",
        ProviderConfig.parse(
            build_config_entries(mock_mass, DEFAULT_MOUNT_PATH),
            persisted,
        ),
    )
    stored = dict(raw_values)
    stored.update(persisted["values"])

    def raw_value(key: str) -> object:
        return stored.get(key)

    resolver = build_policy_resolver(
        cold,
        active_token_ids={readonly_id, replacement_id},
        raw_value_provider=raw_value,
    )

    assert cold.get_value(CONF_POLICY_TOKEN_SUFFIXES) == [readonly_suffix, debug_suffix]
    assert resolver.resolve(readonly_id).profile is PolicyProfile.SAFE_QUERIES
    assert resolver.resolve(replacement_id).profile is PolicyProfile.TRUSTED
    assert policy_event_buffer_enabled(cold, raw_value_provider=raw_value) is True
    assert readonly_id not in repr(persisted)
    assert debug_id not in repr(persisted)


def test_raw_provider_config_value_uses_ma_store_when_present() -> None:
    """Runtime and provider share one reader for preserved policy keys."""
    mass = SimpleNamespace(
        config=SimpleNamespace(
            get_raw_provider_config_value=lambda _id, key, _default: f"raw:{key}"
        )
    )

    assert raw_provider_config_value(mass, "mcp--1", "policy_default") == "raw:policy_default"
    assert (
        raw_provider_config_value(SimpleNamespace(config=None), "mcp--1", "policy_default") is None
    )
    assert raw_provider_config_value(mass, "", "policy_default") is None
