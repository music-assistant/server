"""Constants for the NetEase Cloud Music provider."""

from __future__ import annotations

from typing import Final

CONF_API_BASE_URL: Final[str] = "api_base_url"
CONF_COOKIE: Final[str] = "cookie"
CONF_UID: Final[str] = "uid"
CONF_QR_KEY: Final[str] = "qr_key"
CONF_QR_PAGE_URL: Final[str] = "qr_page_url"
CONF_QUALITY: Final[str] = "quality"

CONF_ACTION_START_QR_AUTH: Final[str] = "start_qr_auth"
CONF_ACTION_CHECK_QR_AUTH: Final[str] = "check_qr_auth"
CONF_ACTION_CLEAR_AUTH: Final[str] = "clear_auth"

QUALITY_STANDARD: Final[str] = "standard"
QUALITY_HIGHER: Final[str] = "higher"
QUALITY_EXHIGH: Final[str] = "exhigh"
QUALITY_LOSSLESS: Final[str] = "lossless"
QUALITY_HIRES: Final[str] = "hires"
QUALITY_JYEFFECT: Final[str] = "jyeffect"
QUALITY_JYMASTER: Final[str] = "jymaster"

DEFAULT_API_BASE_URL: Final[str] = "http://127.0.0.1:3000"
