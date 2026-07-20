"""Constants for the Yandex Ynison plugin."""

from __future__ import annotations

from typing import Final

from ya_passport_auth.ma import BORROW_SOURCE_OWN

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
CONF_ACCOUNT_LOGIN: Final[str] = "account_login"
CONF_REMEMBER_SESSION: Final[str] = "remember_session"
CONF_YM_INSTANCE: Final[str] = "ym_instance"
CONF_MASS_PLAYER_ID: Final[str] = "mass_player_id"
CONF_PUBLISH_NAME: Final[str] = "publish_name"
CONF_ALLOW_PLAYER_SWITCH: Final[str] = "allow_player_switch"
CONF_DEVICE_ID: Final[str] = "device_id"
CONF_OUTPUT_SAMPLE_RATE: Final[str] = "output_sample_rate"
CONF_OUTPUT_BIT_DEPTH: Final[str] = "output_bit_depth"

# Action keys (own-mode QR auth flow)
CONF_ACTION_AUTH_QR: Final[str] = "auth_qr"
CONF_ACTION_CLEAR_AUTH: Final[str] = "clear_auth"

# Special value for "auto" config options
OUTPUT_AUTO: Final[str] = "auto"

# Sentinel value for CONF_YM_INSTANCE — use own manually entered token
# Sentinel for "use own credentials" — canonical value lives in the shared
# auth layer (same literal the plugin has always persisted).
YM_INSTANCE_OWN: Final[str] = BORROW_SOURCE_OWN

# Player selection
PLAYER_ID_AUTO: Final[str] = "__auto__"

# yandex_music provider config keys (read via provider.config.get_value)
YANDEX_MUSIC_CONF_QUALITY: Final[str] = "quality"
YANDEX_MUSIC_CONF_TOKEN: Final[str] = "token"
YANDEX_MUSIC_CONF_X_TOKEN: Final[str] = "x_token"
YANDEX_MUSIC_LOSSLESS_QUALITIES: Final[frozenset[str]] = frozenset({"superb", "lossless"})

# Defaults
DEFAULT_DISPLAY_NAME: Final[str] = "Music Assistant"
DEFAULT_APP_NAME: Final[str] = "Music Assistant"
DEFAULT_APP_VERSION: Final[str] = "1.0.0"

# Device types (from Ynison protobuf DeviceType enum)
DEVICE_TYPE_WEB: Final[str] = "WEB"

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
