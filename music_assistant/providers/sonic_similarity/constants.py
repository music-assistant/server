"""Module-level constants for the Sonic Similarity plugin."""

from __future__ import annotations

from music_assistant_models.enums import ProviderFeature

USEARCH_INDEX_FILENAME_TPL = "sonic_signatures_{domain}_v{version}.usearch"
USEARCH_INDEX_FILENAME_GLOB = "sonic_signatures_{domain}_v*.usearch"

# Source AA provider; hardcoded since manifest depends_on already forecloses any other.
AA_PROVIDER_DOMAIN = "sonic_analysis"

CONF_ENABLE_CLAP_INDEX = "enable_clap_index"
CONF_ENABLE_TEXT_SEARCH = "enable_text_search"
CONF_ENABLE_DISCOVER_ROW = "enable_discover_row"
CONF_DISCOVER_PRESET = "discover_preset"
CONF_DISCOVER_DIVERSITY = "discover_diversity"
EXTRA_DATA_CLAP_EMBEDDING = "clap_embedding"

# Engine for the SIMILAR_TRACKS hook; CLAP option offered only when its index is enabled.
CONF_SIMILAR_TRACKS_ENGINE = "similar_tracks_engine"
SIMILAR_ENGINE_18DIM = "18dim"
SIMILAR_ENGINE_CLAP = "clap"

# 18-dim SIMILAR_TRACKS tuning; shown only when the 18-dim engine is selected.
CONF_SIMILAR_PRESET = "similar_tracks_preset"
CONF_SIMILAR_DIVERSITY = "similar_tracks_diversity"

# Engine for the discover-row recommendations; mirrors the SIMILAR_TRACKS selector.
CONF_DISCOVER_ENGINE = "discover_engine"

CONF_LABEL_STATUS_18DIM = "status_label_18dim"
CONF_LABEL_STATUS_CLAP = "status_label_clap"
CONF_LABEL_STATUS_TEXT = "status_label_text"
ACTION_REBUILD_18DIM = "rebuild_18dim_index"
ACTION_REBUILD_CLAP = "rebuild_clap_index"

PERIODIC_REFRESH_TASK_ID = "sonic_similarity_periodic_refresh"
PERIODIC_REFRESH_INTERVAL_HOURS = 1

SUPPORTED_FEATURES = {
    ProviderFeature.SIMILAR_TRACKS,
    ProviderFeature.RECOMMENDATIONS,
}

# recommendations() folder tunables: seed fan-out bound and visible row length.
RECOMMEND_SEED_COUNT: int = 5
RECOMMEND_PER_SEED_LIMIT: int = 10
RECOMMEND_ITEM_LIMIT: int = 12

# Scales genre/year rerank bonus down to the audio-distance range so it nudges, not dominates.
METADATA_BONUS_SCALE: float = 0.1

# Group weights match FEATURE_GROUPS in vectors.py; `genre`/`era` are rerank-only knobs.
SIMILARITY_PRESETS: dict[str, dict[str, float]] = {
    "balanced": {
        "rhythm": 1.0,
        "loudness": 1.0,
        "timbre": 1.0,
        "regularity": 1.0,
        "mood": 1.0,
        "tonal": 1.0,
        "dynamics": 1.0,
        "genre": 1.0,
        "era": 1.0,
    },
    "vibe": {
        "rhythm": 0.3,
        "loudness": 0.5,
        "timbre": 1.0,
        "regularity": 0.3,
        "mood": 1.0,
        "tonal": 0.5,
        "dynamics": 0.8,
        "genre": 0.5,
        "era": 0.5,
    },
    "party": {
        "rhythm": 1.0,
        "loudness": 0.5,
        "timbre": 0.3,
        "regularity": 0.8,
        "mood": 0.5,
        "tonal": 0.2,
        "dynamics": 0.3,
        "genre": 0.3,
        "era": 0.3,
    },
    "genre_era": {
        "rhythm": 0.5,
        "loudness": 0.5,
        "timbre": 0.5,
        "regularity": 0.5,
        "mood": 0.8,
        "tonal": 0.8,
        "dynamics": 0.5,
        "genre": 1.0,
        "era": 1.0,
    },
    "discover": {
        "rhythm": 0.5,
        "loudness": 0.7,
        "timbre": 1.0,
        "regularity": 0.5,
        "mood": 0.8,
        "tonal": 0.8,
        "dynamics": 0.7,
        "genre": 0.3,
        "era": 0.3,
    },
}
