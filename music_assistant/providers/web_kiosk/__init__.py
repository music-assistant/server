"""
Web Kiosk Player Provider for Music Assistant.

Turns any web browser into a fullscreen kiosk player. Runs an embedded HTTP
server that serves the kiosk screen and a per-player push WebSocket. Music is
chosen in Music Assistant. The screen follows that player. Cover, title,
energy bars, lyrics, and the party code do not need an API token.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant_models.enums import ProviderFeature

from .provider import WebKioskProvider

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    features: set[ProviderFeature] = {ProviderFeature.REMOVE_PLAYER}
    return WebKioskProvider(mass, manifest, config, features)
