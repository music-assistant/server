"""Constants for the OpenAI Compatible provider."""

from __future__ import annotations

from typing import Final

CONF_SERVICE: Final = "service"
CONF_BASE_URL: Final = "base_url"
CONF_API_KEY: Final = "api_key"
CONF_MODELS: Final = "models"

SERVICE_CUSTOM: Final = "custom"

# base URLs of the services this is most often pointed at, so the address does not have
# to be looked up. SERVICE_CUSTOM covers everything else, and the address stays editable
# either way - a localhost default is wrong whenever the server runs on another machine.
SERVICE_BASE_URLS: Final = {
    "openai": "https://api.openai.com/v1",
    "groq": "https://api.groq.com/openai/v1",
    "openrouter": "https://openrouter.ai/api/v1",
    "together": "https://api.together.xyz/v1",
    "ollama": "http://localhost:11434/v1",
    "lmstudio": "http://localhost:1234/v1",
}

DEFAULT_SERVICE: Final = "openai"

# the shared HTTP session carries no deadline of its own, so each call brings one.
# Generous enough for a local server on CPU, while still well short of the point
# where a caller waiting on the answer has any use for it.
CHAT_REQUEST_TIMEOUT: Final = 120

# the model listing only fills a picker, so it gets a shorter deadline: a slow
# endpoint must not hold up the settings page
MODELS_REQUEST_TIMEOUT: Final = 15
