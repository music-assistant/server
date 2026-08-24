"""Read provider configuration and compile immutable policy resolvers."""

from __future__ import annotations

import hashlib
import logging
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from music_assistant_models.errors import AuthenticationRequired

from .capabilities import Capability
from .constants import (
    CONF_DEFAULT_POLICY,
    CONF_MANUAL_TOKEN_IDS,
    CONF_POLICY_TOKEN_SUFFIXES,
    POLICY_MODE_KEY_PREFIX,
    TOKEN_POLICY_KEY_PREFIX,
)
from .policy import (
    PolicyMode,
    PolicyProfile,
    PolicyResolver,
    PolicySelection,
    policy_snapshot,
)

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig

    from music_assistant.mass import MusicAssistant

LOGGER = logging.getLogger(__name__)

INHERIT_POLICY = "Inherit"
LEGACY_READ_ONLY_POLICY = "Read-only"
# Bandit B105: this is a UI display-name prefix, not credential material.
MCP_TOKEN_NAME_PREFIX = "MCP — "  # nosec B105


@dataclass(frozen=True, slots=True)
class PolicyToken:
    """Non-secret token metadata used to render one override group."""

    token_id: str
    name: str


def policy_token_suffix(token_id: str) -> str:
    """Return a deterministic non-reversible suffix for one MA token ID."""
    return hashlib.sha256(token_id.encode()).hexdigest()


def valid_policy_token_suffix(value: object) -> bool:
    """Return whether a stored policy suffix has the exact SHA-256 hex shape."""
    return isinstance(value, str) and bool(re.fullmatch(r"[0-9a-f]{64}", value))


def token_policy_key(token_id: str) -> str:
    """Return the selector key for one token ID without embedding that ID."""
    return f"{TOKEN_POLICY_KEY_PREFIX}{policy_token_suffix(token_id)}"


def policy_mode_key(capability: str | Capability, token_id: str | None = None) -> str:
    """Return one default or token-specific Custom capability key."""
    capability_fragment = str(capability).replace(":", "_")
    if token_id is None:
        return f"{POLICY_MODE_KEY_PREFIX}{capability_fragment}"
    return f"{TOKEN_POLICY_KEY_PREFIX}{capability_fragment}_{policy_token_suffix(token_id)}"


async def current_user_mcp_tokens(mass: MusicAssistant) -> tuple[PolicyToken, ...]:
    """Discover exact-prefix MCP tokens belonging to MA's current settings user."""
    try:
        current_user = await mass.webserver.auth.get_current_user_info()
        tokens = await mass.webserver.auth.get_user_tokens()
    except AuthenticationRequired, AttributeError, RuntimeError, TypeError:
        LOGGER.warning("Unable to discover current-user MCP tokens")
        return ()
    user_id = str(getattr(current_user, "user_id", ""))
    discovered = {
        str(token.token_id): PolicyToken(str(token.token_id), str(token.name))
        for token in tokens
        if str(getattr(token, "user_id", "")) == user_id
        and str(getattr(token, "name", "")).startswith(MCP_TOKEN_NAME_PREFIX)
        and str(getattr(token, "token_id", ""))
    }
    return tuple(sorted(discovered.values(), key=lambda token: (token.name, token.token_id)))


def build_policy_resolver(
    config: ProviderConfig,
    *,
    active_token_ids: Iterable[str] = (),
    raw_value_provider: Callable[[str], Any] | None = None,
) -> PolicyResolver:
    """Compile raw provider values into an immutable fail-closed policy resolver."""
    default = _parse_selection(
        config,
        token_id=None,
        allow_inherit=False,
        raw_value_provider=raw_value_provider,
    )
    token_ids = set(manual_token_ids(config.get_value(CONF_MANUAL_TOKEN_IDS)))
    token_ids.update(str(token_id) for token_id in active_token_ids if str(token_id))
    overrides = {
        token_id: _parse_selection(
            config,
            token_id=token_id,
            allow_inherit=True,
            raw_value_provider=raw_value_provider,
        )
        for token_id in sorted(token_ids)
    }
    return PolicyResolver(default=default, overrides=overrides)


def policy_event_buffer_enabled(
    config: ProviderConfig,
    *,
    active_token_ids: Iterable[str] = (),
    raw_value_provider: Callable[[str], Any] | None = None,
) -> bool:
    """Return whether any configured policy can expose debug events."""
    resolver = build_policy_resolver(
        config,
        active_token_ids=active_token_ids,
        raw_value_provider=raw_value_provider,
    )
    snapshots = [resolver.resolve(None)]
    snapshots.extend(resolver.resolve(token_id) for token_id in resolver.overrides)
    if any(snapshot.mode(Capability.DEBUG_EVENTS) is not PolicyMode.DENY for snapshot in snapshots):
        return True

    suffixes = manual_token_suffixes(
        policy_value(config, CONF_POLICY_TOKEN_SUFFIXES, raw_value_provider)
    )
    for suffix in suffixes:
        key = f"{TOKEN_POLICY_KEY_PREFIX}{suffix}"
        value = policy_value(config, key, raw_value_provider)
        if value == INHERIT_POLICY:
            continue
        try:
            profile = PolicyProfile(str(value))
        except ValueError:
            continue
        if profile is PolicyProfile.CUSTOM:
            mode_key = f"{TOKEN_POLICY_KEY_PREFIX}debug_events_{suffix}"
            try:
                mode = PolicyMode(
                    str(policy_value(config, mode_key, raw_value_provider) or PolicyMode.DENY)
                )
            except ValueError:
                mode = PolicyMode.DENY
        else:
            mode = policy_snapshot(profile).mode(Capability.DEBUG_EVENTS)
        if mode is not PolicyMode.DENY:
            return True
    return False


def policy_value(
    config: ProviderConfig,
    key: str,
    raw_value_provider: Callable[[str], Any] | None,
) -> Any:
    """Read a parsed value, falling back to MA's sanctioned raw config store."""
    value = config.get_value(key)
    return raw_value_provider(key) if value is None and raw_value_provider is not None else value


def manual_token_suffixes(raw: object) -> tuple[str, ...]:
    """Parse the permanent non-reversible token-policy index."""
    if not isinstance(raw, list | tuple | set | frozenset):
        return ()
    return tuple(sorted({str(value) for value in raw if valid_policy_token_suffix(value)}))


def parse_selection(
    config: ProviderConfig,
    *,
    token_id: str | None,
    allow_inherit: bool,
    raw_value_provider: Callable[[str], Any] | None = None,
) -> PolicySelection:
    """Public parser used by configuration composition tests and migrations."""
    return _parse_selection(
        config,
        token_id=token_id,
        allow_inherit=allow_inherit,
        raw_value_provider=raw_value_provider,
    )


def _parse_selection(
    config: ProviderConfig,
    *,
    token_id: str | None,
    allow_inherit: bool,
    raw_value_provider: Callable[[str], Any] | None = None,
) -> PolicySelection:
    """Parse one selector and fail closed on every malformed raw value."""
    key = CONF_DEFAULT_POLICY if token_id is None else token_policy_key(token_id)
    raw = policy_value(config, key, raw_value_provider)
    if raw is None:
        return (
            PolicySelection.inherit()
            if allow_inherit
            else PolicySelection.profile(PolicyProfile.SAFE_QUERIES)
        )
    if allow_inherit and raw == INHERIT_POLICY:
        return PolicySelection.inherit()
    if not isinstance(raw, str):
        return PolicySelection.profile(PolicyProfile.SAFE_QUERIES)
    if raw == LEGACY_READ_ONLY_POLICY:
        return PolicySelection.profile(PolicyProfile.SAFE_QUERIES)
    try:
        profile = PolicyProfile(raw)
    except TypeError, ValueError:
        return PolicySelection.profile(PolicyProfile.SAFE_QUERIES)
    if profile is not PolicyProfile.CUSTOM:
        return PolicySelection.profile(profile)
    modes = {
        str(capability): _parse_mode(
            policy_value(config, policy_mode_key(capability, token_id), raw_value_provider)
        )
        for capability in Capability
    }
    return PolicySelection.custom(modes)


def _parse_mode(raw: object) -> PolicyMode:
    """Parse one raw mode, defaulting invalid and missing values to deny."""
    if not isinstance(raw, str):
        return PolicyMode.DENY
    try:
        return PolicyMode(raw)
    except TypeError, ValueError:
        return PolicyMode.DENY


def manual_token_ids(raw: object) -> tuple[str, ...]:
    """Normalize a multi-value config input into ordered unique token IDs."""
    if isinstance(raw, str):
        values: Iterable[object] = (raw,)
    elif isinstance(raw, Iterable):
        values = raw
    else:
        values = ()
    return tuple(dict.fromkeys(value for item in values if (value := str(item).strip())))
