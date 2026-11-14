"""Module-level constants for the AudioMuse-AI plugin."""

from __future__ import annotations

from music_assistant_models.enums import ProviderFeature

DOMAIN = "audiomuse_ai"

# === Config keys ===
CONF_BASE_URL = "base_url"
CONF_API_TOKEN = "api_token"  # noqa: S105 - config key
# Instance id of the Music Assistant provider whose item ids match the media
# server AudioMuse-AI was pointed at — the bridge between the two id spaces.
CONF_MEDIA_PROVIDER = "media_provider"
CONF_ENABLE_TEXT_SEARCH = "enable_text_search"
CONF_ENABLE_DISCOVER_ROW = "enable_discover_row"
CONF_LABEL_STATUS = "status_label"

# Library aggregator domain, excluded from the media-provider picker since
# AudioMuse-AI ids are the streaming/file provider's ids, not library numeric ids.
LIBRARY_DOMAIN = "library"

# Prefix Music Assistant adds to the media-server id AudioMuse-AI stores, per provider domain.
ITEM_ID_PREFIXES = {"plex": "/library/metadata/"}

# HTTP request timeout (seconds) for every AudioMuse-AI call.
REQUEST_TIMEOUT = 30

# Default neighbour count for the SIMILAR_TRACKS hook / API command.
DEFAULT_SIMILAR_LIMIT = 25

# Discover-row tunables (mirror the sonic_similarity plugin):
# seed fan-out bound, per-seed neighbour pull, and visible row length.
RECOMMEND_SEED_COUNT = 5
RECOMMEND_PER_SEED_LIMIT = 10
RECOMMEND_ITEM_LIMIT = 12

# Max concurrent provider lookups for AudioMuse-AI ids missing from the library.
RESOLVE_CONCURRENCY = 5

SUPPORTED_FEATURES = {
    ProviderFeature.SIMILAR_TRACKS,
    ProviderFeature.RECOMMENDATIONS,
}
