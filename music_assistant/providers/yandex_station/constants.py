"""Constants for Yandex Station Player provider."""

from __future__ import annotations

DOMAIN = "yandex_station"

# Config entry keys
CONF_X_TOKEN = "x_token"
CONF_MUSIC_TOKEN = "music_token"
CONF_REFRESH_TOKEN = "refresh_token"
CONF_REMEMBER_SESSION = "remember_session"
CONF_COOKIES = "cookies"

# Config action keys — the canonical shared values from ya_passport_auth.ma
# (auth_device / auth_qr / auth_cookies / clear_auth). All are UI-only ACTION
# entries, so the rename from the old action_auth_* values affects nothing
# persisted. Re-exported here for the provider's own action comparisons/tests.
CONF_ACTION_AUTH_DEVICE = "auth_device"
CONF_ACTION_AUTH_QR = "auth_qr"
CONF_ACTION_AUTH_COOKIES = "auth_cookies"
CONF_ACTION_CLEAR_AUTH = "clear_auth"
CONF_VOICE_CONTROL = "voice_control"

# Yandex account source: instance_id of a linked yandex_music provider to
# borrow credentials from, or the shared "__own__" sentinel for the
# provider's own login (key name matches the yandex_ynison plugin).
CONF_YM_INSTANCE = "ym_instance"

# Intercept feature: provider-level kill switch (off by default)
CONF_INTERCEPT_FEATURE_ENABLED = "intercept_feature_enabled"

# Intercept feature: per-player toggles
CONF_INTERCEPT_ENABLED = "intercept_enabled"
CONF_INTERCEPT_TARGET = "intercept_target_player_id"

# Yandex API endpoints
QUASAR_DEVICES_URL = "https://iot.quasar.yandex.ru/m/v3/user/devices"
QUASAR_IOT_URL = "https://iot.quasar.yandex.ru"

# mDNS service type for Yandex Station devices
MDNS_TYPE = "_yandexio._tcp.local."

# WebSocket settings
WS_HEARTBEAT = 55
WS_RECONNECT_BASE_DELAY = 30
WS_RECONNECT_MAX_MULTIPLIER = 10
WS_COMMAND_TIMEOUT = 5

# DDoS protection: minimum interval between Yandex API requests (seconds)
API_REQUEST_INTERVAL = 0.2
