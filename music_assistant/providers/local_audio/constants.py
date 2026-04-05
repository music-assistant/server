"""Constants for Local Audio Out provider."""

from __future__ import annotations

import uuid

# UUID namespace for generating stable player IDs from device name + host API.
# This ensures the same device always gets the same player_id across restarts.
DEVICE_UUID_NAMESPACE = uuid.UUID("a1b2c3d4-e5f6-7890-abcd-ef1234567890")

# Default buffer size in frames for the sounddevice output stream.
DEFAULT_BUFFER_FRAMES = 2048
