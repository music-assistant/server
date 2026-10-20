"""
Yandex Station Player Provider for Music Assistant.

Play music on Yandex Station smart speakers via local Glagol WebSocket protocol.
Adapted from AlexxIT/YandexStation (MIT license).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant_models.errors import LoginFailed
from ya_passport_auth.ma import BORROW_SOURCE_OWN

from .constants import (
    CONF_MUSIC_TOKEN,
    CONF_X_TOKEN,
    CONF_YM_INSTANCE,
)
from .provider import YandexStationProvider

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.enums import ProviderFeature
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType

SUPPORTED_FEATURES: set[ProviderFeature] = set()


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
