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
CONF_YM_INSTANCE: Final[str] = "ym_instance"
CONF_MASS_PLAYER_ID: Final[str] = "mass_player_id"
CONF_ALLOW_PLAYER_SWITCH: Final[str] = "allow_player_switch"
CONF_DEVICE_ID: Final[str] = "device_id"
CONF_OUTPUT_SAMPLE_RATE: Final[str] = "output_sample_rate"
CONF_OUTPUT_BIT_DEPTH: Final[str] = "output_bit_depth"
CONF_STREAM_MODE: Final[str] = "stream_mode"

# Special value for "auto" config options
OUTPUT_AUTO: Final[str] = "auto"

# AudioSource session policies
STREAM_MODE_STABLE: Final[str] = "stable"
STREAM_MODE_MAX_QUALITY: Final[str] = "max_quality_dynamic"

# Legacy sentinel and auth keys retained only to reject/clear old own-mode setup data.
LEGACY_YM_INSTANCE_OWN: Final[str] = "__own__"
LEGACY_AUTOMATIC_PLAYER: Final[str] = "__auto__"
LEGACY_AUTH_KEYS: Final[tuple[str, ...]] = (
    "token",
    "x_token",
    "account_login",
    "remember_session",
)

# yandex_music provider config keys
YANDEX_MUSIC_CONF_TOKEN: Final[str] = "token"
YANDEX_MUSIC_CONF_X_TOKEN: Final[str] = "x_token"
YANDEX_MUSIC_LOSSLESS_QUALITIES: Final[frozenset[str]] = frozenset({"superb", "lossless"})

# Defaults
DEFAULT_DISPLAY_NAME: Final[str] = "Music Assistant"
DEFAULT_APP_NAME: Final[str] = "Music Assistant"
DEFAULT_APP_VERSION: Final[str] = "1.0.0"

# Device types (from Ynison protobuf DeviceType enum)
DEVICE_TYPE_WEB: Final[str] = "WEB"
YNISON_DEVICE_SERVER_DISCONNECT: Final[str] = "server-paused-on-active-device-disconnecting"

# Reconnect settings — indexed by attempt number; attempts past the tuple
# saturate at the last entry (so reconnect continues forever at 60 s intervals).
RECONNECT_DELAYS: Final[tuple[float, ...]] = (5.0, 10.0, 30.0, 60.0)

# WebSocket timeouts
WS_CONNECT_TIMEOUT: Final[float] = 15.0
WS_HEARTBEAT: Final[float] = 30.0

# Ynison error codes that require immediate reconnection
YNISON_ERROR_REBALANCED: Final[str] = "300100001"
YNISON_ERROR_NOT_SERVED: Final[str] = "300100002"
YNISON_RECONNECT_ERROR_CODES: Final[frozenset[str]] = frozenset(
    {
        YNISON_ERROR_REBALANCED,
        YNISON_ERROR_NOT_SERVED,
    }
)
