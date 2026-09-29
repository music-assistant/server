"""Constants for the AI Radio plugin."""

from __future__ import annotations

from typing import Any

from music_assistant_models.enums import ContentType
from music_assistant_models.media_items import AudioFormat

CONF_AI_ENGINE = "ai_engine"
CONF_TTS_ENGINE = "tts_engine"
CONF_TTS_LOUDNESS_BOOST = "tts_loudness_boost"
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
    "without explaining the change. "
    "Names and titles often stay in their original language while the voice reads everything "
    "with the pronunciation rules of the script's language. When a name would be mangled that "
    "way, respell it phonetically for the script's language so it still sounds like the "
    "original; leave names that already read correctly untouched."
)
MERGE_SECTION_PROMPT = (
    "Merge the drafts below into one coherent radio break. "
    "Preserve factual content, remove duplication, and make the "
    "final segment sound like one host speaking naturally.\n"
    "<section_drafts>"
)
DEFAULT_WEATHER_PROVIDER = "open_meteo"
DEFAULT_WEATHER_TIMEOUT_SECONDS = 20

# countries and US territories that use Fahrenheit for everyday temperatures
FAHRENHEIT_COUNTRY_CODES = frozenset(
    {"US", "PR", "GU", "VI", "AS", "MP", "LR", "MM", "BS", "BZ", "KY", "PW"}
)
DEFAULT_MAX_CONCURRENT_RUNS = 1
MAX_FINISHED_SESSIONS = 20

# a show whose playback never starts within this window is declared failed
SHOW_START_TIMEOUT_SECONDS = 300

# last-resort guard so a wedged engine fails the clip instead of hanging the session.
# Kept above the deadlines the engines apply themselves (120s in the OpenAI-compatible
# providers), so their own, more specific error is the one that surfaces.
AI_QUERY_TIMEOUT_SECONDS = 180

# ffprobe reports no status code, so its message is all we have to spot a failed render
TTS_SERVER_ERROR_MARKERS = ("Server returned 5XX", "HTTP error 5")

DEFAULT_TTS_LOUDNESS_BOOST = 3

# speech carries ~16 dB between its average level and its peaks, so a plain gain that
# reaches the target clips instead. speechnorm evens the clip out so the level is carried
# by the whole clip, the trim then places it, and the limiter backstops the peaks
TTS_SPEECHNORM_FILTER = "speechnorm=e=12.5:r=0.0005:l=1"
TTS_PEAK_CEILING_DB = -1.5

# one measurement stands in for every clip an engine voices, but a fragment of a few
# words is not representative enough of its level to become that reference
MIN_LOUDNESS_REFERENCE_SECONDS = 2

# a clip is seconds of audio, so a measurement that takes this long is a wedged fetch
LOUDNESS_MEASURE_TIMEOUT = 60

# spoken clips are handed to MA already decoded, so the filter chain runs once here
# instead of once per output
TTS_CLIP_PCM_FORMAT = AudioFormat(
    content_type=ContentType.PCM_S16LE,
    sample_rate=48000,
    bit_depth=16,
    channels=2,
)

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
ATTR_WEATHER_REQUIRED = "ai_radio_weather_required"
# the section's RSS feeds, serialized to JSON so the clip carries them until render time
ATTR_RSS_FEEDS = "ai_radio_rss_feeds"

# placeholders resolved at render time rather than at plan time, so the aired script
# reflects the moment it plays
DEFERRED_PLACEHOLDERS = frozenset(
    {"<timestamp>", "<weather_hourly>", "<weather_daily>", "<rss_feed>"}
)

# the deferred placeholders that need a successful weather fetch to say anything at all
WEATHER_PLACEHOLDER_TOKENS = ("<weather_hourly>", "<weather_daily>")

# substituted for an unresolved weather token in clips that still air
NO_WEATHER_DATA_INSTRUCTION = (
    "(no weather data available - leave out all weather talk, do not invent a forecast)"
)

# RSS feed support: sections may carry a list of RSS/Atom feed URLs whose articles are
# fetched at render time and injected in place of <rss_feed>
RSS_FEED_PLACEHOLDER = "<rss_feed>"

# the deferred placeholders that need a successful RSS fetch to say anything at all
RSS_PLACEHOLDER_TOKENS = ("<rss_feed>",)

# how long to wait for a single RSS feed to respond, in seconds, before giving up
RSS_REQUEST_TIMEOUT = 10

# per-feed article count: server-owned default and bounds, kept in sync with the frontend so the
# behaviour is identical no matter which client wrote the config (companion frontend PR #2859)
RSS_DEFAULT_MAX_ARTICLES = 5
RSS_MIN_MAX_ARTICLES = 1
RSS_MAX_MAX_ARTICLES = 20

# a section may only carry so many feeds; extra entries are dropped during normalization so a
# single section can never fan out into an unbounded number of outbound requests
RSS_MAX_FEEDS_PER_SECTION = 5

# never run more than this many feed downloads at once, so one clip cannot drain the shared pool
RSS_MAX_CONCURRENT_FETCHES = 4

# hard cap on how many bytes we read from a single feed response, to bound memory use on a
# hostile or runaway feed (roughly 2 MiB is plenty for any real RSS/Atom document)
RSS_MAX_FEED_BYTES = 2 * 1024 * 1024

# per-article text is trimmed to this many characters before it enters the LLM prompt, so a
# verbose feed cannot blow up the prompt size
RSS_MAX_ARTICLE_CHARS = 500

# feed downloads are cached for this many seconds so back-to-back clips reuse one fetch instead
# of hammering the feed server, while still refreshing often enough to stay timely
RSS_CACHE_TTL = 300

# cache namespace for RSS feed downloads (scoped per provider instance at call time)
RSS_CACHE_CATEGORY = 0

# substituted for an unresolved RSS token in clips that still air
NO_RSS_DATA_INSTRUCTION = (
    "(no RSS news data available - do not invent or mention news items; "
    "omit the news segment or transition smoothly)"
)

# HA drops a tts_proxy token 60s after its last use at the lowest configurable time_memory
CLIP_STREAMDETAILS_EXPIRATION = 60

# a cached clip with less life than this left is not worth handing out, so it is re-minted
MIN_CLIP_MEDIA_LIFETIME = 5
