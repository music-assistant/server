"""
Yandex Station Player Provider for Music Assistant.

Play music on Yandex Station smart speakers via local Glagol WebSocket protocol.
Adapted from AlexxIT/YandexStation (MIT license).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import ConfigEntryType, ProviderFeature
from music_assistant_models.errors import LoginFailed
from ya_passport_auth.ma import BORROW_SOURCE_OWN

from .constants import (
    CONF_INTERCEPT_FEATURE_ENABLED,
    CONF_MUSIC_TOKEN,
    CONF_X_TOKEN,
    CONF_YM_INSTANCE,
)
from .provider import YandexStationProvider

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigValueType, ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType

SUPPORTED_FEATURES: set[ProviderFeature] = set()


async def get_config_entries(
    mass: MusicAssistant,  # noqa: ARG001
    instance_id: str | None = None,  # noqa: ARG001
    action: str | None = None,  # noqa: ARG001
    values: dict[str, ConfigValueType] | None = None,  # noqa: ARG001
) -> tuple[ConfigEntry, ...]:
    """
    Return Config entries to configure this provider.

    Authentication and the Yandex account source are collected by the interactive
    setup flow (see setup_flow.py); only the genuine options live on this surface.
    """
    return (
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
    provider = YandexStationProvider(mass, manifest, config, SUPPORTED_FEATURES)
    # Credentials come from the setup flow (borrow a linked Yandex Music instance or
    # this provider's own login). Fail fast when neither is present.
    ym_instance = provider.get_setup_value(CONF_YM_INSTANCE)
    borrowing = bool(ym_instance) and ym_instance != BORROW_SOURCE_OWN
    if (
        not borrowing
        and not provider.get_setup_value(CONF_MUSIC_TOKEN)
        and not provider.get_setup_value(CONF_X_TOKEN)
    ):
        msg = "Authentication required. Please login with your Yandex credentials."
        raise LoginFailed(msg)
    return provider
