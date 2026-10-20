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

from .provider import ProfilerProvider

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return ProfilerProvider(mass, manifest, config)
