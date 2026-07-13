"""
Profiler plugin provider for Music Assistant.

Temporarily install this provider on a running server to diagnose CPU,
memory and event-loop issues without shell access. While loaded it
continuously records lightweight health metrics and (optionally) periodic
CPU profile windows; the aggregated result is available as a shareable
report via the `profiler/report` API command.

This provider is a debugging aid, not a regular provider: only enable it
while investigating a performance issue (or when asked to in a support
request) and uninstall it afterwards.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import ConfigEntryType

from .provider import (
    CONF_ACTION_RUN_CPU_PROFILE,
    CONF_CPU_PROFILE_DURATION,
    CONF_CPU_PROFILE_ENABLED,
    CONF_CPU_PROFILE_INTERVAL,
    CONF_TRACEMALLOC_ENABLED,
    CPU_PROFILE_MAX_DURATION,
    CPU_PROFILE_MAX_INTERVAL,
    CPU_PROFILE_MIN_DURATION,
    CPU_PROFILE_MIN_INTERVAL,
    ProfilerProvider,
)

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigValueType, ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return ProfilerProvider(mass, manifest, config)


async def get_config_entries(
    mass: MusicAssistant,
    instance_id: str | None = None,
    action: str | None = None,
    values: dict[str, ConfigValueType] | None = None,
) -> tuple[ConfigEntry, ...]:
    """
    Return Config entries to setup this provider.

    instance_id: id of an existing provider instance (None if new instance setup).
    action: [optional] action key called from config entries UI.
    values: the (intermediate) raw values for config entries sent with the action.
    """
    # ruff: noqa: ARG001
    if (
        action == CONF_ACTION_RUN_CPU_PROFILE
        and instance_id
        and (prov := mass.get_provider(instance_id, provider_type=ProfilerProvider))
    ):
        prov.start_cpu_profile()
    return (
        ConfigEntry(
            key="profiler_note",
            type=ConfigEntryType.LABEL,
            required=False,
        ),
        ConfigEntry(
            key=CONF_CPU_PROFILE_ENABLED,
            type=ConfigEntryType.BOOLEAN,
            default_value=True,
            required=False,
        ),
        ConfigEntry(
            key=CONF_CPU_PROFILE_DURATION,
            type=ConfigEntryType.INTEGER,
            default_value=60,
            range=(CPU_PROFILE_MIN_DURATION, CPU_PROFILE_MAX_DURATION),
            required=False,
            advanced=True,
            depends_on=CONF_CPU_PROFILE_ENABLED,
        ),
        ConfigEntry(
            key=CONF_CPU_PROFILE_INTERVAL,
            type=ConfigEntryType.INTEGER,
            default_value=30,
            range=(CPU_PROFILE_MIN_INTERVAL, CPU_PROFILE_MAX_INTERVAL),
            required=False,
            advanced=True,
            depends_on=CONF_CPU_PROFILE_ENABLED,
        ),
        ConfigEntry(
            key=CONF_ACTION_RUN_CPU_PROFILE,
            type=ConfigEntryType.ACTION,
            action=CONF_ACTION_RUN_CPU_PROFILE,
            required=False,
            hidden=instance_id is None,
        ),
        ConfigEntry(
            key=CONF_TRACEMALLOC_ENABLED,
            type=ConfigEntryType.BOOLEAN,
            default_value=False,
            required=False,
        ),
    )
