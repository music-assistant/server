"""Module-level constants for the Sonic Similarity plugin."""

from __future__ import annotations

from music_assistant_models.enums import ProviderFeature

USEARCH_INDEX_FILENAME_TPL = "sonic_signatures_{domain}_v{version}.usearch"
USEARCH_INDEX_FILENAME_GLOB = "sonic_signatures_{domain}_v*.usearch"

# The only AudioAnalysisProvider whose rows carry every scalar the 18-dim
# vector assembler needs (plus the CLAP embedding the optional index uses).
# Hardcoded because `depends_on: sonic_analysis` in manifest.json already
# forecloses any other choice; revisit if a second compatible AA provider ships.
AA_PROVIDER_DOMAIN = "sonic_analysis"

CONF_ENABLE_CLAP_INDEX = "enable_clap_index"
CONF_ENABLE_TEXT_SEARCH = "enable_text_search"
CONF_ENABLE_DISCOVER_ROW = "enable_discover_row"
CONF_DISCOVER_PRESET = "discover_preset"
CONF_DISCOVER_DIVERSITY = "discover_diversity"
EXTRA_DATA_CLAP_EMBEDDING = "clap_embedding"

# Keys for the read-only status rows on the plugin config page.
CONF_LABEL_STATUS_18DIM = "status_label_18dim"
CONF_LABEL_STATUS_CLAP = "status_label_clap"
CONF_LABEL_STATUS_TEXT = "status_label_text"
# Action keys dispatched back into get_config_entries via the ACTION button entries.
ACTION_REBUILD_18DIM = "rebuild_18dim_index"
ACTION_REBUILD_CLAP = "rebuild_clap_index"

PERIODIC_REFRESH_TASK_ID = "sonic_similarity_periodic_refresh"
PERIODIC_REFRESH_INTERVAL_HOURS = 1

# Both hooks return [] when the engine isn't ready, which the cross-provider
# dispatchers treat as "this provider has nothing right now" — no dynamic
# feature-set tricks needed.
SUPPORTED_FEATURES = {
    ProviderFeature.SIMILAR_TRACKS,
    ProviderFeature.RECOMMENDATIONS,
}

# Tunables for the recommendations() folder. RECOMMEND_SEED_COUNT keeps the
# fan-out cost bounded; RECOMMEND_ITEM_LIMIT is the visible row length.
RECOMMEND_SEED_COUNT: int = 5
RECOMMEND_PER_SEED_LIMIT: int = 10
RECOMMEND_ITEM_LIMIT: int = 12

# Genre/year reranking bonus scale. Raw genre Jaccard + year-proximity terms reach
# magnitudes ~10-20x the audio-distance dynamic range; without scaling they dominate
# ranking and make preset weight changes invisible. 0.1 caps combined bonus at ~0.2,
# comparable to the audio-distance range — categorical context nudges, not overrides.
METADATA_BONUS_SCALE: float = 0.1

# Audio-vector group weights match FEATURE_GROUPS in vectors.py. `genre` and `era`
# are metadata-rerank-only knobs that don't enter the vector distance.
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
