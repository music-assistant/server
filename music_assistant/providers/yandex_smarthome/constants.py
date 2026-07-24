"""Constants for Yandex Smart Home provider."""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Config entry keys
# ---------------------------------------------------------------------------
CONF_INSTANCE_NAME = "instance_name"
CONF_CONNECTION_TYPE = "connection_type"
# Override for MA's webserver Base URL — used when generating callback /
# webhook URLs for Yandex. Lets users keep MA's global Base URL unset (so
# HA Ingress / local access keep working) while still exposing a public
# HTTPS URL only to Yandex via a reverse proxy.
CONF_EXTERNAL_BASE_URL = "external_base_url"
CONF_CLOUD_INSTANCE_ID = "cloud_instance_id"
CONF_CLOUD_INSTANCE_PASSWORD = "cloud_instance_password"
CONF_CLOUD_CONNECTION_TOKEN = "cloud_connection_token"
CONF_SKILL_ID = "skill_id"
CONF_SKILL_TOKEN = "skill_token"
CONF_EXPOSED_PLAYERS = "exposed_players"
CONF_EXPOSED_PLAYLISTS = "exposed_playlists"

# Auto-create-skill feature state (round-trips through the config form)
CONF_AUTO_CREATE_ARTIFACTS = "auto_create_artifacts"
CONF_AUTO_CREATE_SESSION_ID = "auto_create_session_id"
# Cached Yandex Passport x_token from the first successful Device Flow.
# Reused on subsequent auto-create runs so the user does not have to confirm
# the device code every time. Long-lived (months); automatically refreshed
# on use. Cleared if Yandex returns 401 on refresh.
CONF_AUTH_X_TOKEN = "auth_x_token"

# ---------------------------------------------------------------------------
# Config actions
# ---------------------------------------------------------------------------
CONF_ACTION_REGISTER = "register_cloud"
CONF_ACTION_GET_OTP = "get_otp"
CONF_ACTION_AUTO_CREATE = "auto_create_skill"

# Yandex account source: instance_id of a linked yandex_music provider to
# borrow the x_token from, or the shared "__own__" sentinel for this
# provider's own Device Flow (key name matches the other yandex providers).
CONF_YM_INSTANCE = "ym_instance"

# ---------------------------------------------------------------------------
# Connection types
# ---------------------------------------------------------------------------
CONNECTION_TYPE_CLOUD = "cloud"
CONNECTION_TYPE_CLOUD_PLUS = "cloud_plus"
CONNECTION_TYPE_DIRECT = "direct"

# ---------------------------------------------------------------------------
# Cloud relay — yaha-cloud.ru (dext0r's relay service)
# ---------------------------------------------------------------------------
CLOUD_BASE_URL = "https://yaha-cloud.ru"
CLOUD_WS_URL = "wss://yaha-cloud.ru/api/home_assistant/v1/connect"
CLOUD_REGISTER_URL = f"{CLOUD_BASE_URL}/api/home_assistant/v1/instance/register"
CLOUD_CALLBACK_URL = f"{CLOUD_BASE_URL}/api/home_assistant/v2/callback"
CLOUD_OAUTH_AUTHORIZE_URL = f"{CLOUD_BASE_URL}/oauth/authorize"
CLOUD_OAUTH_TOKEN_URL = f"{CLOUD_BASE_URL}/oauth/token"

# Platform identifier sent to the cloud relay
CLOUD_PLATFORM = "music_assistant"

# Account linking template: client_id = "yandex_smart_home:{instance_id}"
CLOUD_SKILL_CLIENT_ID_TEMPLATE = "yandex_smart_home:{instance_id}"
# Account linking: fixed client_secret required by yaha-cloud.ru relay protocol.
# This is NOT a per-install secret — the relay expects exactly this value.
# Direct mode uses a per-install auto-generated secret instead (see CONF_DIRECT_CLIENT_SECRET).
CLOUD_SKILL_CLIENT_SECRET = "secret"

# ---------------------------------------------------------------------------
# Cloud Plus / Direct mode — Yandex Dialogs API
# ---------------------------------------------------------------------------
YANDEX_DIALOGS_CALLBACK_BASE = "https://dialogs.yandex.net/api/v1/skills"
YANDEX_DIALOGS_DEVELOPER_URL = "https://dialogs.yandex.ru/developer/smart-home"
YANDEX_OAUTH_URL = "https://oauth.yandex.ru/authorize?response_type=token&client_id=c473ca268cd749d3a8371351a8f2bcbd"

# Webhook URL template for yaha-cloud relay (private skill points here)
CLOUD_SKILL_WEBHOOK_TEMPLATE = "https://yaha-cloud.ru/api/yandex_smart_home"

# ---------------------------------------------------------------------------
# Direct connection — HTTP endpoints on MA webserver
# ---------------------------------------------------------------------------
DIRECT_API_BASE_PATH = "/api/yandex_smarthome/v1.0"
# What we send to Yandex as the skill Backend URI. Yandex appends /v1.0/...
# itself when calling our endpoints, so the backend URI must NOT include
# /v1.0 — otherwise Yandex calls /v1.0/v1.0/user/devices and gets 404.
DIRECT_BACKEND_URI_PATH = "/api/yandex_smarthome"
DIRECT_AUTH_BASE_PATH = "/api/yandex_smarthome/auth"
DIRECT_HEALTH_RESPONSE = "Yandex Smart Home for Music Assistant"
CONF_DIRECT_ACCESS_TOKEN = "direct_access_token"
CONF_DIRECT_CLIENT_SECRET = "direct_client_secret"
DIRECT_OAUTH_CLIENT_ID = "https://social.yandex.net/"
OAUTH_CODE_EXPIRY = 300  # pending authorization codes expire after 5 minutes
MAX_PENDING_CODES = 20  # hard cap on concurrent pending authorization codes (DoS protection)

# ---------------------------------------------------------------------------
# Timing (seconds)
# ---------------------------------------------------------------------------
STATE_REPORT_DELAY = 1.0  # debounce window for batched state reports
STATE_HEARTBEAT_INTERVAL = 3600  # report all states hourly
STATE_INITIAL_REPORT_DELAY = 15  # initial report after startup
CLOUD_RECONNECT_MIN = 2  # initial reconnect delay
CLOUD_RECONNECT_MAX = 180  # max reconnect delay (exponential backoff cap)
CLOUD_HEARTBEAT_INTERVAL = 45  # WebSocket heartbeat

# ---------------------------------------------------------------------------
# Yandex Smart Home API — device & capability constants
# ---------------------------------------------------------------------------
YANDEX_DEVICE_TYPE_MEDIA = "devices.types.media_device"
YANDEX_DEVICE_TYPE_RECEIVER = "devices.types.media_device.receiver"

CAPABILITY_ON_OFF = "devices.capabilities.on_off"
CAPABILITY_RANGE = "devices.capabilities.range"
CAPABILITY_TOGGLE = "devices.capabilities.toggle"

INSTANCE_ON = "on"
INSTANCE_VOLUME = "volume"
INSTANCE_MUTE = "mute"
INSTANCE_PAUSE = "pause"
INSTANCE_CHANNEL = "channel"
INSTANCE_INPUT_SOURCE = "input_source"

UNIT_PERCENT = "unit.percent"

# Yandex mode values for input_source mapping (by index position)
YANDEX_MODE_VALUES = (
    "one",
    "two",
    "three",
    "four",
    "five",
    "six",
    "seven",
    "eight",
    "nine",
    "ten",
)

# Combined cap for native sources + playlist sources in mode(input_source).
# Yandex allows max 10 modes per capability.
MAX_INPUT_SOURCES = len(YANDEX_MODE_VALUES)

# ---------------------------------------------------------------------------
# Yandex Smart Home API — response codes
# ---------------------------------------------------------------------------
RESPONSE_OK = "DONE"
ERROR_DEVICE_UNREACHABLE = "DEVICE_UNREACHABLE"
ERROR_INVALID_ACTION = "INVALID_ACTION"
ERROR_INTERNAL_ERROR = "INTERNAL_ERROR"
ERROR_DEVICE_NOT_FOUND = "DEVICE_NOT_FOUND"
