"""
Spotify Connect plugin for Music Assistant.

We tie a single player to a single Spotify Connect daemon.
The provider has multi instance support, so multiple players can be linked to
multiple Spotify Connect daemons.

The MA-facing logic lives in ``provider.py``; everything specific to one
Spotify Connect implementation (currently go-librespot) lives behind the
``SpotifyConnectBackend`` contract in ``backends/``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .provider import (
    CONF_MASS_PLAYER_ID,
    CONF_PUBLISH_NAME,
    DEFAULT_PUBLISH_NAME,
    PLAYER_ID_AUTO,
    SpotifyConnectProvider,
)

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType

__all__ = [
    "CONF_MASS_PLAYER_ID",
    "CONF_PUBLISH_NAME",
    "DEFAULT_PUBLISH_NAME",
    "PLAYER_ID_AUTO",
    "SpotifyConnectProvider",
    "setup",
]


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return SpotifyConnectProvider(mass, manifest, config)
