"""Constants for the QQ Music provider."""

from __future__ import annotations

from typing import Final

CONF_UIN: Final[str] = "uin"
CONF_MUSICID: Final[str] = "musicid"
CONF_MUSICKEY: Final[str] = "musickey"
CONF_LOGIN_TYPE: Final[str] = "login_type"
CONF_CREDENTIAL_JSON: Final[str] = "credential_json"
CONF_QR_IDENTIFIER: Final[str] = "qr_identifier"
CONF_QR_TYPE: Final[str] = "qr_type"
CONF_QR_PAGE_URL: Final[str] = "qr_page_url"
CONF_QUALITY: Final[str] = "quality"
CONF_ACTION_START_QR_AUTH: Final[str] = "start_qr_auth"
CONF_ACTION_START_QQ_QR_AUTH: Final[str] = "start_qq_qr_auth"
CONF_ACTION_START_WX_QR_AUTH: Final[str] = "start_wx_qr_auth"
CONF_ACTION_CHECK_QR_AUTH: Final[str] = "check_qr_auth"
CONF_ACTION_CLEAR_AUTH: Final[str] = "clear_auth"

QUALITY_MP3_128: Final[str] = "mp3_128"
QUALITY_MP3_320: Final[str] = "mp3_320"
QUALITY_FLAC: Final[str] = "flac"
QUALITY_HI_RES: Final[str] = "hi_res"
