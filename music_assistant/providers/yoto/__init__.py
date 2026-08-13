"""Yoto music provider support for Music Assistant."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .provider import SUPPORTED_FEATURES, YotoProvider
from .setup_flow import CONF_CLIENT_ID, CONF_REFRESH_TOKEN

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType


async def setup(
    mass: MusicAssistant,
    manifest: ProviderManifest,
    config: ProviderConfig,
) -> ProviderInstanceType:
    """
    Initialize the Yoto provider.

    :param mass: Music Assistant instance.
    :param manifest: Yoto provider manifest.
    :param config: Stored provider configuration.
    :return: Initialized provider instance.
    """
    return YotoProvider(mass, manifest, config)


__all__ = [
    "CONF_CLIENT_ID",
    "CONF_REFRESH_TOKEN",
    "SUPPORTED_FEATURES",
    "YotoProvider",
    "setup",
]
