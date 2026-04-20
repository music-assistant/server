"""Constants for Local Audio Out provider."""

from __future__ import annotations

import uuid

# UUID namespace for generating stable player IDs from device name + host API.
# NOTE: keeping the original local_audio namespace to preserve existing player IDs.
DEVICE_UUID_NAMESPACE = uuid.UUID("a7d68578-af81-4e3e-a8b8-df8f9d6d1f05")

# Category for caching previous player state (volume/mute).
# Bump the integer to invalidate old cached values when the format changes.
CACHE_CATEGORY_PREV_STATE = 1

# Volume control mode constants
VOLUME_CONTROL_HARDWARE = "hardware"
VOLUME_CONTROL_SOFTWARE = "software"
VOLUME_CONTROL_DISABLED = "disabled"

# Config entry keys
CONF_VOLUME_CONTROL = "volume_control"
CONF_HARDWARE_VOLUME_CEILING = "hardware_volume_ceiling"

# Defaults
DEFAULT_PLAYER_VOLUME = 25  # initial volume for new players (percent)
DEFAULT_HARDWARE_VOLUME_CEILING = 85  # default PA hardware ceiling (percent)
DEFAULT_BUFFER_FRAMES = 1024  # sounddevice blocksize (frames)
