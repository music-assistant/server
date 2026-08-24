"""Compose Music Assistant ConfigEntry values and surface settings."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import TYPE_CHECKING, Any

from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption
from music_assistant_models.enums import ConfigEntryType

from .capabilities import Capability
from .constants import (
    CONF_CONNECT_EXTERNAL_URL,
    CONF_DEBUG_EVENT_BUFFER_CAPACITY,
    CONF_DEFAULT_POLICY,
    CONF_ENABLE_MCP_APP,
    CONF_ENFORCE_AUDIENCE,
    CONF_EXTRA_ALLOWED_ORIGINS,
    CONF_MANUAL_TOKEN_IDS,
    CONF_MOUNT_PATH,
    CONF_POLICY_TOKEN_SUFFIXES,
    CONF_REQUIRE_AUTH,
    CONF_RES_LIBRARY,
    CONF_RES_PLAYER,
    CONF_RES_PROMPTS,
    CONF_TRUST_FORWARDED_PROTO,
    DEFAULT_MOUNT_PATH,
)
from .policy import PolicyMode, PolicyProfile
from .policy_config import (
    INHERIT_POLICY,
    PolicyToken,
    policy_mode_key,
    token_policy_key,
)
from .policy_config import (
    manual_token_ids as normalize_manual_token_ids,
)

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant


def build_config_entries(
    mass: MusicAssistant,
    mount_path: str,
    *,
    tokens: Iterable[Any] = (),
    manual_token_ids: Iterable[str] = (),
    stored_value_provider: Callable[[str], Any] | None = None,
) -> tuple[ConfigEntry, ...]:
    """Return endpoint, resource, prompt, and dynamic v2 policy entries."""
    base_url = mass.webserver.base_url.rstrip("/")
    mount_path = "/" + mount_path.strip("/")
    info_label = (
        f"MCP endpoint: {base_url}{mount_path}\n"
        "Create tokens in Profile → Long-lived access tokens."
    )
    entries: list[ConfigEntry] = [
        ConfigEntry(
            key="info_label",
            type=ConfigEntryType.LABEL,
            label=info_label,
            category="server",
            required=False,
        ),
        ConfigEntry(
            key="open_connect",
            type=ConfigEntryType.ACTION,
            action="open_connect",
            required=False,
        ),
        ConfigEntry(
            key=CONF_REQUIRE_AUTH,
            type=ConfigEntryType.BOOLEAN,
            default_value=True,
            category="server",
            required=False,
        ),
        ConfigEntry(
            key=CONF_MOUNT_PATH,
            type=ConfigEntryType.STRING,
            default_value=DEFAULT_MOUNT_PATH,
            category="server",
            advanced=True,
            required=False,
        ),
        ConfigEntry(
            key=CONF_ENFORCE_AUDIENCE,
            type=ConfigEntryType.BOOLEAN,
            default_value=False,
            category="server",
            advanced=True,
            required=False,
        ),
        ConfigEntry(
            key=CONF_EXTRA_ALLOWED_ORIGINS,
            type=ConfigEntryType.STRING,
            default_value="",
            category="server",
            advanced=True,
            required=False,
        ),
        ConfigEntry(
            key=CONF_CONNECT_EXTERNAL_URL,
            type=ConfigEntryType.STRING,
            default_value="",
            category="server",
            advanced=True,
            required=False,
        ),
        ConfigEntry(
            key=CONF_TRUST_FORWARDED_PROTO,
            type=ConfigEntryType.BOOLEAN,
            default_value=False,
            category="server",
            advanced=True,
            required=False,
        ),
        _policy_selector(CONF_DEFAULT_POLICY, None, allow_inherit=False),
        ConfigEntry(
            key=CONF_POLICY_TOKEN_SUFFIXES,
            type=ConfigEntryType.STRING,
            default_value=[],
            multi_value=True,
            hidden=True,
            category="policy",
            required=False,
            value=(
                stored_value_provider(CONF_POLICY_TOKEN_SUFFIXES)
                if stored_value_provider is not None
                else None
            ),
        ),
    ]
    entries.extend(_custom_matrix(CONF_DEFAULT_POLICY))
    entries.append(
        ConfigEntry(
            key=CONF_MANUAL_TOKEN_IDS,
            type=ConfigEntryType.STRING,
            default_value=[],
            multi_value=True,
            category="policy",
            required=False,
            advanced=True,
        )
    )

    rendered: dict[str, PolicyToken] = {}
    for token in tokens:
        token_id = str(getattr(token, "token_id", "")).strip()
        if token_id:
            rendered[token_id] = PolicyToken(token_id, str(getattr(token, "name", token_id)))
    for token_id in normalize_manual_token_ids(manual_token_ids):
        rendered.setdefault(token_id, PolicyToken(token_id, f"Manual MCP token ·{token_id[-8:]}"))
    for token in sorted(rendered.values(), key=lambda value: (value.name, value.token_id)):
        selector = token_policy_key(token.token_id)
        selector_entry = _policy_selector(selector, token.name, allow_inherit=True)
        if stored_value_provider is not None:
            selector_entry.value = stored_value_provider(selector)
        entries.append(selector_entry)
        matrix = _custom_matrix(selector, token.token_id)
        if stored_value_provider is not None:
            for entry in matrix:
                entry.value = stored_value_provider(entry.key)
        entries.extend(matrix)

    entries.extend(
        (
            _bool(CONF_RES_LIBRARY, True, "mcp_resources"),
            _bool(CONF_RES_PLAYER, True, "mcp_resources"),
            _bool(CONF_RES_PROMPTS, True, "mcp_resources"),
            _bool(CONF_ENABLE_MCP_APP, False, "mcp_app"),
            ConfigEntry(
                key=CONF_DEBUG_EVENT_BUFFER_CAPACITY,
                type=ConfigEntryType.INTEGER,
                default_value=500,
                range=(50, 5000),
                category="debug",
                required=False,
            ),
        )
    )
    return tuple(entries)


def _bool(key: str, default: bool, category: str) -> ConfigEntry:
    """Build one optional boolean provider entry."""
    return ConfigEntry(
        key=key,
        type=ConfigEntryType.BOOLEAN,
        default_value=default,
        category=category,
        required=False,
    )


def _policy_selector(key: str, label: str | None, *, allow_inherit: bool) -> ConfigEntry:
    """Build one profile selector."""
    values = ([INHERIT_POLICY] if allow_inherit else []) + [
        profile.value for profile in PolicyProfile
    ]
    return ConfigEntry(
        key=key,
        type=ConfigEntryType.STRING,
        default_value=INHERIT_POLICY if allow_inherit else PolicyProfile.SAFE_QUERIES.value,
        options=[ConfigValueOption(value=value) for value in values],
        translation_key="policy_token" if label is not None else None,
        translation_params=[label] if label is not None else None,
        category="policy",
        required=False,
    )


def _custom_matrix(selector_key: str, token_id: str | None = None) -> list[ConfigEntry]:
    """Build the conditional 26-capability Custom matrix for one selector."""
    options = [ConfigValueOption(value=mode.value) for mode in PolicyMode]
    return [
        ConfigEntry(
            key=policy_mode_key(capability, token_id),
            type=ConfigEntryType.STRING,
            default_value=PolicyMode.DENY.value,
            options=options,
            advanced=True,
            depends_on=selector_key,
            depends_on_value=PolicyProfile.CUSTOM.value,
            translation_key="policy_capability",
            translation_params=[str(capability)],
            category="policy",
            required=False,
        )
        for capability in Capability
    ]
