"""Music-Assistant-side configuration keys for the WLED Audio Sync provider."""

from __future__ import annotations

# Re-export the protocol constants from the bridge subpackage so callers can
# keep importing them from the provider root if they were already doing so.
from .wled_audiosync_bridge.constants import (
    WLED_AUDIOSYNC_DEFAULT_MULTICAST_GROUP,
    WLED_AUDIOSYNC_DEFAULT_PORT,
    WLED_V2_MAGIC_HEADER,
    WLED_V2_PACKET_SIZE,
)

# Provider-level config keys.
CONF_MANUAL_PLAYERS = "manual_players"
CONF_REQUIRE_AUDIOREACTIVE = "require_audioreactive"

# Provider-level config defaults.
DEFAULT_REQUIRE_AUDIOREACTIVE = True
# Timeout for the `/json/info` HTTP probe used to detect the AudioReactive
# usermod on a discovered WLED. Real-LAN responses arrive in ~10-50 ms.
JSON_INFO_PROBE_TIMEOUT_S = 5.0

# Per-player config keys.
CONF_DESTINATION_ADDRESS = "destination_address"
CONF_DESTINATION_PORT = "destination_port"
CONF_DESTINATION_KIND = "destination_kind"  # one of: unicast, broadcast, multicast
CONF_MULTICAST_TTL = "multicast_ttl"
CONF_DUPLICATE_TRANSMIT = "duplicate_transmit"

# Per-player config defaults.
DEFAULT_DUPLICATE_TRANSMIT = True

# Discovered-vs-manual marker stored in player config so we can tell them apart.
CONF_PLAYER_SOURCE = "player_source"
PLAYER_SOURCE_MDNS = "mdns"
PLAYER_SOURCE_MANUAL = "manual"

__all__ = [
    "CONF_DESTINATION_ADDRESS",
    "CONF_DESTINATION_KIND",
    "CONF_DESTINATION_PORT",
    "CONF_DUPLICATE_TRANSMIT",
    "CONF_MANUAL_PLAYERS",
    "CONF_MULTICAST_TTL",
    "CONF_PLAYER_SOURCE",
    "CONF_REQUIRE_AUDIOREACTIVE",
    "DEFAULT_DUPLICATE_TRANSMIT",
    "DEFAULT_REQUIRE_AUDIOREACTIVE",
    "JSON_INFO_PROBE_TIMEOUT_S",
    "PLAYER_SOURCE_MANUAL",
    "PLAYER_SOURCE_MDNS",
    "WLED_AUDIOSYNC_DEFAULT_MULTICAST_GROUP",
    "WLED_AUDIOSYNC_DEFAULT_PORT",
    "WLED_V2_MAGIC_HEADER",
    "WLED_V2_PACKET_SIZE",
]
