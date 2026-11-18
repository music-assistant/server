"""Constants for the WLED Audio Sync provider."""

from __future__ import annotations

from enum import StrEnum
from typing import Final


class ScalingMode(StrEnum):
    """Perceptual curves matching WLED's own FFT scaling modes."""

    SQUARE_ROOT = "square_root"
    LINEAR = "linear"
    LOGARITHMIC = "logarithmic"


CONF_LATENCY_MS: Final[str] = "latency_ms"
CONF_GAIN_DB: Final[str] = "gain_db"
CONF_SCALING_MODE: Final[str] = "scaling_mode"

DEFAULT_PORT: Final[int] = 11988
DEFAULT_LATENCY_MS: Final[int] = 100
# The extractor is calibrated against a full-scale sine, which real program material never reaches.
DEFAULT_GAIN_DB: Final[float] = 6.0
DEFAULT_SCALING_MODE: Final = ScalingMode.SQUARE_ROOT

# WLED always listens on this multicast group; only the port varies per zone.
WLED_MULTICAST_GROUP: Final[str] = "239.0.0.1"

# Best-effort match of WLED's 16-band log spectrum (43Hz-9259Hz).
SPECTRUM_BINS: Final[int] = 16
SPECTRUM_SCALE: Final = "log"
SPECTRUM_F_MIN: Final[int] = 43
SPECTRUM_F_MAX: Final[int] = 9259

# Onset strength (0-255) below this is not reported as a WLED samplePeak hit.
PEAK_MIN_STRENGTH: Final[int] = 100

SEND_RATE_HZ: Final[int] = 40
