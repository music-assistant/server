"""Constants for the AI Radio plugin."""

from __future__ import annotations

from typing import Any

CONF_AI_ENGINE = "ai_engine"
CONF_TTS_ENGINE = "tts_engine"
CONF_TIMEZONE = "timezone"
CONF_WEATHER_CITY = "weather_city"
CONF_WEATHER_COUNTRY = "weather_country"

# providers load concurrently, so the plugin supplying the engines may still be
# loading when AI Radio initializes: wait this long for it before giving up
ENGINE_DISCOVERY_TIMEOUT = 30

TRANSLATION_OWNER = "provider.ai_radio"

DEFAULT_LLM_INSTRUCTIONS = (
    "Host personality: warm, sharp, music-literate, and slightly premium "
    "without sounding formal. Program instructions: write for spoken delivery, "
    "keep segments concise, avoid bullet-point phrasing, avoid clichés, "
    "mention concrete details when available, and maintain a believable "
    "radio flow between sections."
)
DEFAULT_WEATHER_PROVIDER = "open_meteo"
DEFAULT_WEATHER_TIMEOUT_SECONDS = 20
DEFAULT_MAX_CONCURRENT_RUNS = 1
MAX_FINISHED_SESSIONS = 20
DEFAULT_DYNAMIC_STALL_TIMEOUT_SECONDS = 300

SUPPORTED_FEATURES: set[Any] = set()
EMPTY_SECTION_ID = "EMPTY_SECTION"
VALID_WEB_SEARCH_MODES = {"disabled", "allow", "force"}
WEB_SEARCH_MODE_RANK = {"disabled": 0, "allow": 1, "force": 2}
