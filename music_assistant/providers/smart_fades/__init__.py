"""Smart Fades audio analysis provider."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from music_assistant_models.config_entries import ConfigEntry, ConfigValueType
from music_assistant_models.enums import ConfigEntryType

from .provider import SmartFadesProvider

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.enums import ProviderFeature
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant

SUPPORTED_FEATURES: set[ProviderFeature] = set()


async def setup(
    mass: MusicAssistant,
    manifest: ProviderManifest,
    config: ProviderConfig,
) -> SmartFadesProvider:
    """Set up the Smart Fades provider."""
    return SmartFadesProvider(mass, manifest, config, SUPPORTED_FEATURES)


async def get_config_entries(
    mass: MusicAssistant,
    instance_id: str | None = None,
    action: str | None = None,
    values: dict[str, ConfigValueType] | None = None,
) -> tuple[ConfigEntry, ...]:
    """Return config entries for this provider."""
    # ruff: noqa: ARG001
    return (
        ConfigEntry(
            key="single_cpu_warning",
            type=ConfigEntryType.ALERT,
            label=(
                "Only 1 CPU core detected on this system. "
                "Smart Fades analysis typically takes some CPU time during model inference. "
                "Enabling it on single-CPU hosts may cause performance issues "
                "and block normal playback. Enable at your own risk."
            ),
            required=False,
            hidden=(os.cpu_count() or 1) > 1,
        ),
    )
