"""Protocol and analysis constants for the Hue Entertainment bridge."""

from __future__ import annotations

from typing import Final

# ---- Hue bridge & streaming ----

HUE_ENTERTAINMENT_PORT: Final[int] = 2100
HUESTREAM_HEADER: Final[bytes] = b"HueStream"
HUESTREAM_VERSION: Final[bytes] = bytes([0x02, 0x00])
COLOR_SPACE_RGB: Final[int] = 0x00
COLOR_SPACE_XY: Final[int] = 0x01
MAX_LIGHTS_PER_MESSAGE: Final[int] = 20
TARGET_UPDATE_RATE_HZ: Final[int] = 25
UPDATE_INTERVAL_S: Final[float] = 1.0 / TARGET_UPDATE_RATE_HZ
KEEPALIVE_INTERVAL_S: Final[float] = 5.0

# ---- Spectrum config for Sendspin visualizer ----

# 17 mel bins over 20-20kHz: enough resolution to map distinct bands to lights
# while keeping the per-frame payload small (bin ~10 ≈ 3.5kHz is the musical
# ceiling, see _CHANNEL_BIN_MAX).
SPECTRUM_BINS: Final[int] = 17
SPECTRUM_SCALE: Final = "mel"
SPECTRUM_F_MIN: Final[int] = 20
SPECTRUM_F_MAX: Final[int] = 20000
