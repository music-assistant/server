"""Constants for Local PulseAudio Out provider."""
from __future__ import annotations

import uuid

DEVICE_UUID_NAMESPACE = uuid.UUID("b2c3d4e5-f6a7-8901-bcde-f12345678901")

CONF_VOLUME_CONTROL = "volume_control"
VOLUME_CONTROL_SOFTWARE = "software"
VOLUME_CONTROL_HARDWARE = "hardware"
VOLUME_CONTROL_DISABLED = "disabled"

CONF_HARDWARE_VOLUME_CEILING = "hardware_volume_ceiling"
DEFAULT_HARDWARE_VOLUME_CEILING = 50
