"""Constants for Local Audio Out provider."""

from __future__ import annotations

import uuid

# UUID namespace for generating stable player IDs from device name + host API
DEVICE_UUID_NAMESPACE = uuid.UUID("a1b2c3d4-e5f6-7890-abcd-ef1234567890")

# Default buffer size in frames for the sounddevice output stream
DEFAULT_BUFFER_FRAMES = 2048

CONF_VOLUME_CONTROL = "volume_control"
VOLUME_CONTROL_SOFTWARE = "software"
VOLUME_CONTROL_HARDWARE = "hardware"
VOLUME_CONTROL_DISABLED = "disabled"
