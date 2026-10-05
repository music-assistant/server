"""
Spotify Connect plugin for Music Assistant.

The provider runs as a single instance that advertises one Spotify Connect
device (one backend daemon and one AudioSource) per connected Music Assistant
player.

The MA-facing logic lives in ``provider.py``; everything specific to one
Spotify Connect implementation (Spotify Soloist or go-librespot) lives behind the
``SpotifyConnectBackend`` contract in ``base.py`` (one implementation per subdirectory).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .provider import (
    BACKEND_GO_LIBRESPOT,
    BACKEND_SOLOIST,
    CONF_API_KEY,
    CONF_BACKEND,
    CONF_SOLOIST_CONSENT,
    CONF_VOLUME_MODE,
    SUPPORTED_FEATURES,
    VOLUME_MODE_OPTIONS,
    SpotifyConnectProvider,
)

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType

__all__ = [
    "BACKEND_GO_LIBRESPOT",
    "BACKEND_SOLOIST",
    "CONF_API_KEY",
    "CONF_BACKEND",
    "CONF_SOLOIST_CONSENT",
    "CONF_VOLUME_MODE",
    "SUPPORTED_FEATURES",
    "VOLUME_MODE_OPTIONS",
    "SpotifyConnectProvider",
    "setup",
]


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return SpotifyConnectProvider(mass, manifest, config)
