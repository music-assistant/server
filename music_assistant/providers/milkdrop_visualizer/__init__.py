"""
MilkDrop Visualizer Plugin for Music Assistant.

Reads the PCM Music Assistant already decodes for a playing player and relays
time-domain waveform frames over a WebSocket endpoint on the MA webserver. The
MA web frontend feeds these frames to the Butterchurn (MilkDrop) renderer in
the now-playing views.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from music_assistant.helpers.util import import_module_in_thread

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.enums import ProviderFeature
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType

SUPPORTED_FEATURES: set[ProviderFeature] = set()


async def setup(
    mass: MusicAssistant,
    manifest: ProviderManifest,
    config: ProviderConfig,
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    module = await import_module_in_thread(
        ".provider", "music_assistant.providers.milkdrop_visualizer"
    )
    return cast(
        "ProviderInstanceType",
        module.MilkdropVisualizerProvider(mass, manifest, config, SUPPORTED_FEATURES),
    )
