"""Constants for the Bose SoundTouch player provider."""

from __future__ import annotations

from enum import StrEnum

DOMAIN = "bose_soundtouch"
PLAYER_ID_PREFIX = "bose_soundtouch_"

IDLE_POLL_INTERVAL = 30
PLAYBACK_POLL_INTERVAL = 20

# Optional Bose SoundTouch developer app key. When configured, announcements are
# sent natively to the speaker as an overlay that ducks and resumes playback.
CONF_APP_KEY = "app_key"

# Bose SoundTouch exposes a local HTTP API on port 8090 and a websocket
# notification channel on port 8080 (the "gabbo" subprotocol).
NOTIFICATION_PORT = 8080
# e.g. http://1.2.3.4:8091/Xml/AVTransport3.xml
UPNP_PORT = 8091
UPNP_CONTROL_ENDPOINT = "AVTransport/Control"
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

# strings for overwriting a preset
ACTION_OVERWRITE_PRESET_1 = "action_overwrite_preset_1"
ACTION_OVERWRITE_PRESET_2 = "action_overwrite_preset_2"
ACTION_OVERWRITE_PRESET_3 = "action_overwrite_preset_3"
ACTION_OVERWRITE_PRESET_4 = "action_overwrite_preset_4"
ACTION_OVERWRITE_PRESET_5 = "action_overwrite_preset_5"
ACTION_OVERWRITE_PRESET_6 = "action_overwrite_preset_6"


class PlayerOptionKeys(StrEnum):
    """PlayerOptionKeys."""

    NETWORK_NAME = "network_name"
    BASS = "bass"
