"""Universal Player constants."""

from __future__ import annotations

from typing import Final

UNIVERSAL_PLAYER_PREFIX: Final[str] = "up"

# Config key for storing linked protocol player IDs (hidden config entry)
CONF_LINKED_PROTOCOL_IDS: Final[str] = "linked_protocol_ids"

# Config key for storing device identifiers (MAC, UUID, etc.)
CONF_DEVICE_IDENTIFIERS: Final[str] = "device_identifiers"

# Config key for storing device info (model, manufacturer)
CONF_DEVICE_INFO: Final[str] = "device_info"
