"""Constants for the AI Radio plugin."""

from __future__ import annotations

from typing import Any

CONF_OPENAI_API_KEY = "openai_api_key"
CONF_ELEVENLABS_API_KEY = "elevenlabs_api_key"
CONF_UI_AUTO_REFRESH_SECONDS = "ui_auto_refresh_seconds"

DEFAULT_OPENAI_BASE_URL = "https://api.openai.com/v1"
DEFAULT_LLM_MODEL = "gpt-4o-mini"
DEFAULT_LLM_INSTRUCTIONS = (
    "Host personality: warm, sharp, music-literate, and slightly premium "
    "without sounding formal. Program instructions: write for spoken delivery, "
    "keep segments concise, avoid bullet-point phrasing, avoid clichés, "
    "mention concrete details when available, and maintain a believable "
    "radio flow between sections."
)
DEFAULT_TEMPERATURE = 0.7
DEFAULT_MAX_TOKENS = 900
DEFAULT_TTS_PROVIDER = "openai"
DEFAULT_OPENAI_TTS_MODEL = "gpt-4o-mini-tts"
DEFAULT_OPENAI_TTS_VOICE = "ballad"
DEFAULT_OPENAI_TTS_INSTRUCTIONS = (
    "Delivery instructions: confident radio host, natural pacing, "
    "clear sentence endings, subtle energy lift on intros and transitions, "
    "and a warm close without exaggerated theatrics."
)
DEFAULT_ELEVENLABS_MODEL = "eleven_multilingual_v2"
DEFAULT_SECTION_STORE_PATH = "ai_radio_sections"
DEFAULT_WEATHER_PROVIDER = "open_meteo"
DEFAULT_WEATHER_TIMEOUT_SECONDS = 20
DEFAULT_TTS_TIMEOUT_SECONDS = 30
DEFAULT_MAX_CONCURRENT_RUNS = 1

SUPPORTED_FEATURES: set[Any] = set()
EMPTY_SECTION_ID = "EMPTY_SECTION"
VALID_WEB_SEARCH_MODES = {"disabled", "allow", "force"}
WEB_SEARCH_MODE_RANK = {"disabled": 0, "allow": 1, "force": 2}
