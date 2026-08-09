"""Config constants for the WLED Audio Sync provider."""

from __future__ import annotations

from typing import Final

CONF_PORT: Final[str] = "port"
CONF_LATENCY_MS: Final[str] = "latency_ms"
CONF_GAIN_DB: Final[str] = "gain_db"

DEFAULT_PORT: Final[int] = 11988
DEFAULT_LATENCY_MS: Final[int] = 100
# The extractor's dB scale is calibrated against a theoretical full-scale sine
# wave, which typical program material (especially loudness-normalized radio
# streams) never approaches -- without a gain boost, everything sits
# compressed low in the 0-255 range WLED expects. Applied as a proper
# amplitude-domain multiplier (see packet._dbu16_to_amplitude), so it boosts
# real signal without lifting true silence off the floor. Most of the
# perceptual "make quiet content visible" work is now done by
# packet.PERCEPTUAL_GAMMA, so this stays modest to avoid the two compounding
# into an overdriven result; this default is a starting point, not a
# calibrated value -- needs on-device tuning.
DEFAULT_GAIN_DB: Final[float] = 6.0

# WLED always listens on this fixed multicast group; only the port varies
# per sync zone (see audio_reactive.cpp: beginMulticast(239.0.0.1, audioSyncPort)).
WLED_MULTICAST_GROUP: Final[str] = "239.0.0.1"

# ---- Spectrum config requested from the Sendspin visualizer ----

# WLED's own audio_reactive usermod (audio_reactive.cpp, the default
# non-bandpass-filter band table) bins fftResult[16] across 43Hz-9259Hz, per
# its own comment describing the design as a logarithmic progression
# ("Multiplier = (End freq/Start freq)^(1/16)"). Its actual hardcoded table
# is a hand-tuned refinement of that (extra density around ~1kHz) rather
# than a pure log curve, and Sendspin's extractor only supports a scale
# formula + overall range (not arbitrary custom edges), so this is a
# best-effort match: same scale type (log, i.e. np.logspace -- not "mel",
# which is a different perceptual curve) and the same overall range, not a
# bin-for-bin replica of WLED's exact table.
SPECTRUM_BINS: Final[int] = 16
SPECTRUM_SCALE: Final = "log"
SPECTRUM_F_MIN: Final[int] = 43
SPECTRUM_F_MAX: Final[int] = 9259

# Onset strength (0-255, see ExtractedFrame.peak) below this is not reported
# as a WLED samplePeak hit.
PEAK_MIN_STRENGTH: Final[int] = 100

# Send rate for the UDP audio-sync packet stream.
SEND_RATE_HZ: Final[int] = 40
