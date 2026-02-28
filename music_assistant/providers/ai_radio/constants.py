"""Constants for the AI Radio plugin."""

from __future__ import annotations

from typing import Any

CONF_OPENAI_API_KEY = "openai_api_key"
CONF_ELEVENLABS_API_KEY = "elevenlabs_api_key"
CONF_UI_AUTO_REFRESH_SECONDS = "ui_auto_refresh_seconds"

DEFAULT_OPENAI_BASE_URL = "https://api.openai.com/v1"
DEFAULT_LLM_MODEL = "gpt-4o-mini"
DEFAULT_LLM_INSTRUCTIONS = "On-air radio host style with concise spoken-word phrasing."
DEFAULT_TEMPERATURE = 0.7
DEFAULT_MAX_TOKENS = 900
DEFAULT_TTS_PROVIDER = "openai"
DEFAULT_OPENAI_TTS_MODEL = "gpt-4o-mini-tts"
DEFAULT_OPENAI_TTS_VOICE = "ballad"
DEFAULT_OPENAI_TTS_INSTRUCTIONS = "Warm radio host delivery."
DEFAULT_ELEVENLABS_MODEL = "eleven_multilingual_v2"
DEFAULT_SECTION_STORE_PATH = "ai_radio_sections"
DEFAULT_WEATHER_PROVIDER = "open_meteo"
DEFAULT_WEATHER_TIMEOUT_SECONDS = 20
DEFAULT_MAX_CONCURRENT_RUNS = 1

SUPPORTED_FEATURES: set[Any] = set()
EMPTY_SECTION_ID = "EMPTY_SECTION"
VALID_WEB_SEARCH_MODES = {"disabled", "allow", "force"}
WEB_SEARCH_MODE_RANK = {"disabled": 0, "allow": 1, "force": 2}
AI_RADIO_WEB_BASE_PATH = "/plugin/ai_radio"
AI_RADIO_WEB_FILES = {
    "": "index.html",
    "/": "index.html",
    "/index.html": "index.html",
    "/air.png": "air.png",
    "/app.js": "app.js",
    "/styles.css": "styles.css",
    "/example_station.json": "example_station.json",
}
