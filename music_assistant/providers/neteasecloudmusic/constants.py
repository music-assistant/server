"""Constants for the NetEase Cloud Music provider."""

from __future__ import annotations

from typing import Final

CONF_API_BASE_URL: Final[str] = "api_base_url"
CONF_COOKIE: Final[str] = "cookie"
CONF_UID: Final[str] = "uid"
CONF_QUALITY: Final[str] = "quality"

QUALITY_STANDARD: Final[str] = "standard"
QUALITY_HIGHER: Final[str] = "higher"
QUALITY_EXHIGH: Final[str] = "exhigh"
QUALITY_LOSSLESS: Final[str] = "lossless"
QUALITY_HIRES: Final[str] = "hires"
QUALITY_JYEFFECT: Final[str] = "jyeffect"
QUALITY_JYMASTER: Final[str] = "jymaster"

DEFAULT_API_BASE_URL: Final[str] = "http://127.0.0.1:3000"
