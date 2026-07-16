"""
Yandex Station Player Provider for Music Assistant.

Play music on Yandex Station smart speakers via local Glagol WebSocket protocol.
Adapted from AlexxIT/YandexStation (MIT license).
"""

from __future__ import annotations

import logging
from dataclasses import replace as dc_replace
from typing import TYPE_CHECKING

from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption
from music_assistant_models.enums import ConfigEntryType, ProviderFeature
from music_assistant_models.errors import LoginFailed
from ya_passport_auth.ma import (
    ACTION_AUTH_COOKIES,
    ACTION_AUTH_DEVICE,
    ACTION_AUTH_QR,
    BORROW_SOURCE_OWN,
    AuthConfigSpec,
    KeySpec,
    build_auth_config_entries,
    handle_auth_action,
    is_authenticated,
    list_yandex_music_instances,
)

from .auth import PAGE_CONFIG
from .constants import (
    CONF_INTERCEPT_FEATURE_ENABLED,
    CONF_MUSIC_TOKEN,
    CONF_REFRESH_TOKEN,
    CONF_REMEMBER_SESSION,
    CONF_X_TOKEN,
    CONF_YM_INSTANCE,
)
from .provider import YandexStationProvider

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigValueType, ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType

_LOGGER = logging.getLogger(__name__)

SUPPORTED_FEATURES: set[ProviderFeature] = set()

# The station keeps the "remember session" toggle visible after login so
# users can drop long-lived tokens without resetting authentication first.
AUTH_SPEC = AuthConfigSpec(
    keys=KeySpec(
        x_token=CONF_X_TOKEN,
        music_token=CONF_MUSIC_TOKEN,
        refresh_token=CONF_REFRESH_TOKEN,
        remember_session=CONF_REMEMBER_SESSION,
    ),
    flows=frozenset({"device", "qr", "cookies"}),
    remember_visible_after_auth=True,
)


async def get_config_entries(
    mass: MusicAssistant,
    instance_id: str | None = None,  # noqa: ARG001
    action: str | None = None,
    values: dict[str, ConfigValueType] | None = None,
) -> tuple[ConfigEntry, ...]:
    """Return Config entries to setup this provider."""
    if values is None:
        values = {}

    # Yandex account source: borrow from a linked yandex_music instance or
    # use the provider's own login. A stale selection (instance removed)
    # normalizes back to own so the login block reappears.
    ym_instances = list_yandex_music_instances(mass)
    selected = str(values.get(CONF_YM_INSTANCE) or BORROW_SOURCE_OWN)
    if selected != BORROW_SOURCE_OWN and selected not in {i for i, _ in ym_instances}:
        selected = BORROW_SOURCE_OWN
        values[CONF_YM_INSTANCE] = BORROW_SOURCE_OWN
    borrowing = selected != BORROW_SOURCE_OWN

    # Standard auth ACTION dispatch (device/QR/cookies login, reset,
    # remember-session-off token drop) from the shared layer. Skipped while
    # borrowing — the linked instance owns authentication.
    if not borrowing:
        await handle_auth_action(mass, AUTH_SPEC, PAGE_CONFIG, action, values)

    # Dynamic label text
    if borrowing:
        label_text = (
            "Using the Yandex account of the linked Yandex Music provider. "
            "Authentication is managed there."
        )
    elif not is_authenticated(AUTH_SPEC, values):
        label_text = (
            "Open a verification URL on any device and enter the short code, "
            "or scan a QR code with the Yandex app on your phone.\n\n"
            "Alternatively, you can paste browser cookies in the advanced settings."
        )
    elif action in (ACTION_AUTH_DEVICE, ACTION_AUTH_QR, ACTION_AUTH_COOKIES):
        label_text = "Authenticated to Yandex. Don't forget to save to complete setup."
    else:
        label_text = "Authenticated to Yandex."

    # Titles of the source options are data-driven (instance display names).
    source_options = [
        *(ConfigValueOption(inst_id, f"Yandex Music: {name}") for inst_id, name in ym_instances),
        ConfigValueOption(BORROW_SOURCE_OWN, "Use own credentials (default)"),
    ]

    auth_entries = build_auth_config_entries(AUTH_SPEC, values, status_label=label_text)
    if borrowing:
        # Hide the whole login block; the status label stays as explanation.
        auth_entries = tuple(
            entry if entry.key == "label_text" else dc_replace(entry, hidden=True)
            for entry in auth_entries
        )

    return (
        ConfigEntry(
            key=CONF_YM_INSTANCE,
            type=ConfigEntryType.STRING,
            options=source_options,
            default_value=BORROW_SOURCE_OWN,
            required=False,
        ),
        *auth_entries,
        # Experimental intercept feature — provider-level master switch.
        # When OFF, no per-player intercept setting takes effect.
        ConfigEntry(
            key=CONF_INTERCEPT_FEATURE_ENABLED,
            type=ConfigEntryType.BOOLEAN,
            default_value=False,
            required=False,
        ),
    )


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    ym_instance = config.get_value(CONF_YM_INSTANCE)
    borrowing = bool(ym_instance) and ym_instance != BORROW_SOURCE_OWN
    music_token = config.get_value(CONF_MUSIC_TOKEN)
    x_token = config.get_value(CONF_X_TOKEN)
    if not borrowing and not music_token and not x_token:
        msg = "Authentication required. Please login with your Yandex credentials."
        raise LoginFailed(msg)
    return YandexStationProvider(mass, manifest, config, SUPPORTED_FEATURES)
