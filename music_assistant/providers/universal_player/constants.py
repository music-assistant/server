"""Universal Player constants."""

from __future__ import annotations

from typing import Final

UNIVERSAL_PLAYER_PREFIX: Final[str] = "up"

# Config key for storing device identifiers (MAC, UUID, etc.)
CONF_DEVICE_IDENTIFIERS: Final[str] = "device_identifiers"

# Config key for storing device info (model, manufacturer)
CONF_DEVICE_INFO: Final[str] = "device_info"

# Config key for the moment a universal player was created, as a nanosecond
# timestamp so players created in the same second still order, used to let the
# older player win when two of them merge.
CONF_CREATED_AT: Final[str] = "created_at"

# Protocols where external sources (e.g. Spotify Connect) can play
# independently of Music Assistant.
EXTERNAL_SOURCE_PROTOCOLS = {"chromecast", "dlna"}
