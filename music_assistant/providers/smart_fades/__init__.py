"""Smart Fades audio analysis provider."""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant_models.config_entries import ConfigEntry, ConfigValueType
from music_assistant_models.enums import ConfigEntryType

from music_assistant.helpers.util import (
    system_meets_requirements,
    verify_system_meets_requirements,
)

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.enums import ProviderFeature
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant

    from .provider import SmartFadesProvider

SUPPORTED_FEATURES: set[ProviderFeature] = set()

# Smart Fades runs on-device ML (torch) inference; gate it to capable hardware.
# 4GB nominal, matching the Balanced buffer threshold (the minimum buffer smart crossfade
# needs). The gate applies meets_memory_target()'s tolerance, so a genuine 4GB host (which
# reports ~3.8GB after the kernel/firmware reservation) still passes.
MIN_RAM_GB = 4.0
MIN_CPU_CORES = 2
# Below the recommended thresholds the provider still runs, but we surface an
# informational notice (see get_config_entries) as it may be tight under load.
RECOMMENDED_RAM_GB = 6.0
RECOMMENDED_CPU_CORES = 4


async def setup(
    mass: MusicAssistant,
    manifest: ProviderManifest,
    config: ProviderConfig,
) -> SmartFadesProvider:
    """Set up the Smart Fades provider."""
    # Gate before importing the provider module so the heavy torch/beat_this stack is
    # never imported on a host that does not meet the minimal requirements.
    await verify_system_meets_requirements(
        feature_name="Smart Fades",
        min_memory_gb=MIN_RAM_GB,
        min_cpu_cores=MIN_CPU_CORES,
        require_ml_inference=True,
    )
    from .provider import SmartFadesProvider  # noqa: PLC0415

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
            key="resource_warning",
            type=ConfigEntryType.ALERT,
            required=False,
            hidden=system_meets_requirements(
                min_memory_gb=RECOMMENDED_RAM_GB,
                min_cpu_cores=RECOMMENDED_CPU_CORES,
            ),
        ),
    )
