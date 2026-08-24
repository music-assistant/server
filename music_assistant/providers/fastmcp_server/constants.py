"""Configuration keys, defaults, and constants for the MCP Server provider."""

from __future__ import annotations

# Server and endpoint settings.
CONF_REQUIRE_AUTH = "require_auth"
CONF_MOUNT_PATH = "mount_path"
CONF_EXTRA_ALLOWED_ORIGINS = "extra_allowed_origins"
CONF_ENFORCE_AUDIENCE = "enforce_audience"
CONF_CONNECT_EXTERNAL_URL = "connect_external_url"
CONF_TRUST_FORWARDED_PROTO = "trust_forwarded_proto"
CONF_ENABLE_MCP_APP = "enable_mcp_app"

DEFAULT_MOUNT_PATH = "/mcp/v1"

# Permissions & Confirmations v2 policy settings.
CONF_DEFAULT_POLICY = "policy_default"
# Bandit B105: these constants are configuration keys, not credential values.
CONF_MANUAL_TOKEN_IDS = "policy_manual_token_ids"  # nosec B105
CONF_POLICY_TOKEN_SUFFIXES = "policy_token_suffixes"  # nosec B105
POLICY_MODE_KEY_PREFIX = "policy_mode_"
TOKEN_POLICY_KEY_PREFIX = "policy_token_"  # nosec B105

# MCP resource and prompt toggles.
CONF_RES_LIBRARY = "res_library"
CONF_RES_PLAYER = "res_player"
CONF_RES_PROMPTS = "res_prompts"

# Optional debug event retention.
CONF_DEBUG_EVENT_BUFFER_CAPACITY = "debug_event_buffer_capacity"

RESOURCE_KEYS: frozenset[str] = frozenset({CONF_RES_LIBRARY, CONF_RES_PLAYER, CONF_RES_PROMPTS})
POLICY_KEYS: frozenset[str] = frozenset(
    {CONF_DEFAULT_POLICY, CONF_MANUAL_TOKEN_IDS, CONF_POLICY_TOKEN_SUFFIXES}
)


def is_policy_key(key: str) -> bool:
    """Return whether a config key belongs to the v2 policy model."""
    return key in POLICY_KEYS or key.startswith((POLICY_MODE_KEY_PREFIX, TOKEN_POLICY_KEY_PREFIX))


def is_hot_swappable_key(key: str) -> bool:
    """Return whether a change can be applied without replacing the provider runtime."""
    return key in RESOURCE_KEYS or is_policy_key(key)
