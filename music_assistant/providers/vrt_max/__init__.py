"""
VRT MAX provider for Music Assistant.

Live radio is streamed anonymously via direct Icecast URLs (no authentication).
The programme/podcast catalogue is browsed through VRT's anonymous, page-path
keyed GraphQL API: radio programme archives ("herbeluister") and podcasts are
exposed as Podcast / PodcastEpisode items. On-demand playback of those episodes
requires authentication.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .provider import VrtMaxProvider

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Set up the VRT MAX provider."""
    return VrtMaxProvider(mass, manifest, config)
