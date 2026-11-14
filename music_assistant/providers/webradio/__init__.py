"""
Web Radio Broadcast player provider for Music Assistant.

Each configured station is exposed as a virtual MA player whose queue is
served on a stable HTTP URL using the Shoutcast/ICY protocol. Dumb internet-
radio devices, VLC, browsers and similar clients can tune in by URL.

Audio is broadcast only while the queue is playing; stop ends the broadcast
and the queue running out also ends it. Pause is not surfaced on the queue
source - it has no meaningful behaviour on a live broadcast. The producer
ffmpeg pipeline starts on the first listener and shuts down once the last
one disconnects.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant_models.enums import ProviderFeature

from .provider import WebRadioProvider

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType


SUPPORTED_FEATURES: set[ProviderFeature] = {ProviderFeature.REMOVE_PLAYER}


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize the provider instance."""
    return WebRadioProvider(mass, manifest, config, SUPPORTED_FEATURES)
