"""Constants for the AI Radio plugin."""

from __future__ import annotations

from typing import Any

CONF_AI_ENGINE = "ai_engine"
CONF_TTS_ENGINE = "tts_engine"
CONF_TIMEZONE = "timezone"
CONF_WEATHER_CITY = "weather_city"
CONF_WEATHER_COUNTRY = "weather_country"
CONF_WEATHER_PROVIDER = "weather_provider"
CONF_WEATHER_TIMEOUT = "weather_timeout_seconds"

# providers load concurrently, so the plugin supplying the engines may still be
# loading when AI Radio initializes: wait this long for it before giving up
ENGINE_DISCOVERY_TIMEOUT = 30

# grace period for an engine that disappears while AI Radio is loaded. Generous enough
# to sit out a Home Assistant restart, so a running show is not torn down for it
ENGINE_RECHECK_GRACE = 300

# how long to wait before reloading after an engine stayed missing, matching the
# cadence the load path uses for its own retries
ENGINE_RETRY_DELAY = 120

TRANSLATION_OWNER = "provider.ai_radio"

DEFAULT_LLM_INSTRUCTIONS = (
    "Host personality: warm, sharp, music-literate, and slightly premium "
    "without sounding formal. Program instructions: write for spoken delivery, "
    "keep segments concise, avoid bullet-point phrasing, avoid clichés, "
    "mention concrete details when available, and maintain a believable "
    "radio flow between sections."
)
# appended to every AI query on top of the station's own instructions: how a name has to be
# spelled to survive the TTS engine is a pipeline concern, not a per-station style choice
TTS_PRONUNCIATION_INSTRUCTIONS = (
    "The output is sent directly to a text-to-speech engine. "
    "Always write names exactly as they should be spoken aloud. Replace stylized spellings, "
    "acronyms, abbreviations, and unusual artist or band names with their natural spoken "
    "equivalents. Never include the original spelling, pronunciation explanation, phonetic "
    "notation, or both versions. Output only the spoken version. Examples: INXS → In Excess; "
    "Mi-Sex → My Sex; P!nk → Pink; blink-182 → Blink One Eighty-Two. If a name could be "
    "mispronounced by the TTS engine, rewrite it into the clearest natural spoken form "
    "without explaining the change."
)
MERGE_SECTION_PROMPT = (
    "Merge the drafts below into one coherent radio break. "
    "Preserve factual content, remove duplication, and make the "
    "final segment sound like one host speaking naturally.\n"
    "<section_drafts>"
)
DEFAULT_WEATHER_PROVIDER = "open_meteo"
DEFAULT_WEATHER_TIMEOUT_SECONDS = 20
DEFAULT_MAX_CONCURRENT_RUNS = 1
MAX_FINISHED_SESSIONS = 20

# a show whose playback never starts within this window is declared failed
SHOW_START_TIMEOUT_SECONDS = 300

# last-resort guards so a wedged engine fails the clip instead of hanging the session.
# Kept above the deadlines the engines apply themselves (120s in the OpenAI-compatible
# providers), so their own, more specific error is the one that surfaces.
AI_QUERY_TIMEOUT_SECONDS = 180
TTS_QUERY_TIMEOUT_SECONDS = 180

# ffprobe reports no status code, so its message is all we have to spot a failed render
TTS_SERVER_ERROR_MARKERS = ("Server returned 5XX", "HTTP error 5")

SUPPORTED_FEATURES: set[Any] = set()
EMPTY_SECTION_ID = "EMPTY_SECTION"
VALID_WEB_SEARCH_MODES = {"disabled", "allow", "force"}
WEB_SEARCH_MODE_RANK = {"disabled": 0, "allow": 1, "force": 2}

# QueueItem.extra_attributes keys carrying a clip's pending render state. Scalars only —
# extra_attributes is serialized to clients and persisted with the queue.
ATTR_SESSION_ID = "ai_radio_session_id"
ATTR_STATION_ID = "ai_radio_station_id"
ATTR_PROMPT = "ai_radio_prompt"
ATTR_MAX_CHARS = "ai_radio_max_chars"
ATTR_WEB_SEARCH_MODE = "ai_radio_web_search_mode"
ATTR_RENDERED_TEXT = "ai_radio_rendered_text"
ATTR_HOST_ID = "ai_radio_host_id"
ATTR_QUEUE_DJ = "ai_radio_queue_dj"
ATTR_GAP_NEXT_ID = "ai_radio_gap_next_id"

# placeholders resolved at render time rather than at plan time, so the aired script
# reflects the moment it plays
DEFERRED_PLACEHOLDERS = frozenset({"<timestamp>", "<weather_hourly>", "<weather_daily>"})

# HA drops a tts_proxy token 60s after its last use at the lowest configurable time_memory
CLIP_STREAMDETAILS_EXPIRATION = 60
