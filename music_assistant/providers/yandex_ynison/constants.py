"""Constants for the Yandex Ynison plugin."""

from __future__ import annotations

from typing import Final

# Ynison WebSocket endpoints
YNISON_REDIRECT_URL: Final[str] = (
    "wss://ynison.music.yandex.ru/redirector.YnisonRedirectService/GetRedirectToYnison"
)
YNISON_STATE_PATH: Final[str] = "/ynison_state.YnisonStateService/PutYnisonState"

# Origin header required by Ynison
YNISON_ORIGIN: Final[str] = "https://music.yandex.ru"

# Configuration keys
CONF_TOKEN: Final[str] = "token"
CONF_X_TOKEN: Final[str] = "x_token"
CONF_MASS_PLAYER_ID: Final[str] = "mass_player_id"
CONF_PUBLISH_NAME: Final[str] = "publish_name"
CONF_ALLOW_PLAYER_SWITCH: Final[str] = "allow_player_switch"
CONF_DEVICE_ID: Final[str] = "device_id"
CONF_OUTPUT_SAMPLE_RATE: Final[str] = "output_sample_rate"
CONF_OUTPUT_BIT_DEPTH: Final[str] = "output_bit_depth"
CONF_FFMPEG_PACING: Final[str] = "ffmpeg_pacing"

# ffmpeg pacing mode values
PACING_REALTIME: Final[str] = "realtime"

# Special value for "auto" config options
OUTPUT_AUTO: Final[str] = "auto"

# Actions
CONF_ACTION_AUTH_QR: Final[str] = "auth_qr"
CONF_ACTION_CLEAR_AUTH: Final[str] = "clear_auth"
CONF_REMEMBER_SESSION: Final[str] = "remember_session"

# Player selection
PLAYER_ID_AUTO: Final[str] = "__auto__"

# Defaults
DEFAULT_DISPLAY_NAME: Final[str] = "Music Assistant"
DEFAULT_APP_NAME: Final[str] = "Music Assistant"
DEFAULT_APP_VERSION: Final[str] = "1.0.0"

# Device types (from Ynison protobuf DeviceType enum)
DEVICE_TYPE_WEB: Final[str] = "WEB"

# Reconnect settings
RECONNECT_DELAYS: Final[tuple[float, ...]] = (2.0, 4.0, 8.0, 16.0, 30.0, 60.0)
MAX_RECONNECT_ATTEMPTS: Final[int] = 5

# WebSocket timeouts
WS_CONNECT_TIMEOUT: Final[float] = 15.0
WS_HEARTBEAT: Final[float] = 30.0
