"""Constants for the Bose SoundTouch player provider."""

from __future__ import annotations

DOMAIN = "bose_soundtouch"
PLAYER_ID_PREFIX = "bose_soundtouch_"

# Optional Bose SoundTouch developer app key. When configured, announcements are
# sent natively to the speaker as an overlay that ducks and resumes playback.
CONF_APP_KEY = "app_key"

# Bose SoundTouch exposes a local HTTP API on port 8090 and a websocket
# notification channel on port 8080 (the "gabbo" subprotocol).
NOTIFICATION_PORT = 8080
WS_SUBPROTOCOLS = ("gabbo",)
WS_HEARTBEAT = 30
REQUEST_TIMEOUT = 10
RECONNECT_DELAY = 10

# mDNS service type advertised by SoundTouch speakers on the network.
MDNS_TYPE = "_soundtouch._tcp.local."

# physical favorite/preset buttons available on the SoundTouch speakers.
PRESET_IDS = range(1, 7)

# now_playing "source" value reported while the speaker is in standby.
SOURCE_STANDBY = "STANDBY"
SOURCE_INVALID = "INVALID_SOURCE"
