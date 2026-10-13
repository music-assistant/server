"""Constants for the Web Kiosk Provider."""

from __future__ import annotations

import re

CONF_HTTP_PORT = "http_port"
CONF_KIOSK_URL = "kiosk_url"
CONF_PLAYER_IDLE_TIMEOUT = "player_idle_timeout"
CONF_SHOW_STOP_NOTIFICATION = "show_stop_notification"
CONF_ENABLE_SENDSPIN_BRIDGE = "enable_sendspin_bridge"

DEFAULT_HTTP_PORT = 8098
DEFAULT_PLAYER_IDLE_TIMEOUT = 30  # minutes
DEFAULT_SHOW_STOP_NOTIFICATION = False
DEFAULT_ENABLE_SENDSPIN_BRIDGE = True

# Player ID prefix for dynamically registered kiosk players
WEB_KIOSK_PLAYER_ID_PREFIX = "wk_"

# Sanitize device_id or IP for use in player_id (alphanumeric + underscore only)
PLAYER_ID_SANITIZE_RE = re.compile(r"[^a-zA-Z0-9_]+")

# sendspin-js 5 identity: base64url X25519 public key (43 chars, no padding).
# Music Assistant 2.11 registers the bridge under this id, not a synthetic prefix.
SENDSPIN_CLIENT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{16,128}$")
# Seconds the bridge waits for the kiosk's JS client to connect after a stream
# start before transferring playback back to the regular HTTP player.
SENDSPIN_CONNECT_TIMEOUT = 15.0

# Seconds to keep a player after its last browser socket closes. A reload opens
# a new socket inside this window, so the player stays. The page also reconnects
# a dropped socket after 2 seconds, and this window is longer than that.
PLAYER_DISCONNECT_GRACE_SECONDS = 5.0


def normalize_sendspin_client_id(value: str | None) -> str | None:
    """Return a browser Sendspin identity, or None when the value is not one."""
    if value and SENDSPIN_CLIENT_ID_RE.fullmatch(value):
        return value
    return None
