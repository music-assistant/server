"""
AudioMuse-AI plugin.

A thin HTTP client over an external AudioMuse-AI server
(https://github.com/NeptuneHub/AudioMuse-AI). Where the ``sonic_similarity``
plugin computes similarity locally from ``sonic_analysis`` data, this plugin
delegates all similarity, recommendation, and free-text search to a running
AudioMuse-AI instance via its REST API.

AudioMuse-AI keys tracks on the media server's native item id, which Music
Assistant stores in that server's provider mapping, for some media servers with
a fixed prefix (see ``ITEM_ID_PREFIXES``). The ``media_provider`` config entry
names which Music Assistant provider to map against, so results round-trip back
to real Tracks via ``mass.music.tracks.get(item_id, media_provider)``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant_models.enums import ProviderFeature

from music_assistant.providers.audiomuse_ai.constants import (
    CONF_ENABLE_TEXT_SEARCH,
    SUPPORTED_FEATURES,
)
from music_assistant.providers.audiomuse_ai.provider import AudioMuseAiPlugin

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider instance with given configuration."""
    features = SUPPORTED_FEATURES.copy()
    if bool(config.get_value(CONF_ENABLE_TEXT_SEARCH)):
        features.add(ProviderFeature.SEARCH)
    return AudioMuseAiPlugin(mass, manifest, config, features)
