"""Constants for the OpenAI Compatible provider."""

from __future__ import annotations

from typing import Final

CONF_BASE_URL: Final = "base_url"
CONF_API_KEY: Final = "api_key"
CONF_MODELS: Final = "models"
CONF_REQUEST_TIMEOUT: Final = "request_timeout"

DEFAULT_BASE_URL: Final = "https://api.openai.com/v1"

# generous by default: a local server running on CPU needs far longer for a
# completion than a hosted one, and the consumers apply their own deadlines on top
DEFAULT_REQUEST_TIMEOUT: Final = 120
MIN_REQUEST_TIMEOUT: Final = 10
MAX_REQUEST_TIMEOUT: Final = 600

# the model listing is only read to fill a picker, so it gets a short deadline of
# its own: a slow endpoint must not hold up the settings page
MODELS_REQUEST_TIMEOUT: Final = 15
