"""
Sonic Similarity plugin.

Two similarity engines in one plugin, both backed by usearch HNSW:

* **Traits (18-dim weighted-Euclidean)** (always on): per-track signature
  assembled from sonic_analysis scalars (BPM, energy, loudness, …) and
  ranked with a configurable weight preset. Atomic mmap-view rebuild.

* **Character (1024-dim CLAP cosine)** (opt-in via the ``enable_clap_index`` config
  entry): builds a second usearch index over the CLAP audio embeddings
  already stored by sonic_analysis under
  ``audio_analysis.extra_data["clap_embedding"]``. Track-to-track
  semantic-audio similarity, with no additional dependencies beyond
  usearch itself.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant_models.enums import ProviderFeature

from music_assistant.providers.sonic_similarity.constants import (
    CONF_ENABLE_TEXT_SEARCH,
    SUPPORTED_FEATURES,
)
from music_assistant.providers.sonic_similarity.provider import (
    SonicSimilarityPlugin as SonicSimilarityPlugin,
)

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
    # read the stored value directly: the config's option entries are only resolved
    # (and its values populated) once the instance exists, which is after this call
    if bool(mass.config.get_raw_provider_config_value(config.instance_id, CONF_ENABLE_TEXT_SEARCH)):
        features.add(ProviderFeature.SEARCH)
    return SonicSimilarityPlugin(mass, manifest, config, features)
