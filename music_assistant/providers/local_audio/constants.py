"""Constants for Local Audio Out provider."""

from __future__ import annotations

import math
import uuid
from typing import Final

# Stable namespace for generating player IDs from device name + host API.
# IMPORTANT: This exact value is part of the persisted player_id contract for
# local_audio devices and must never change after release, otherwise existing
# users will lose stored state and any links keyed by the old player IDs.
DEVICE_UUID_NAMESPACE = uuid.UUID("a7d68578-af81-4e3e-a8b8-df8f9d6d1f05")

# Category for caching previous player state (volume/mute).
# Bump the integer to invalidate old cached values when the format changes.
CACHE_CATEGORY_PREV_STATE = 1

# Audio backend selector
CONF_AUDIO_BACKEND = "audio_backend"
AUDIO_BACKEND_AUTO = "auto"
AUDIO_BACKEND_PULSEAUDIO = "pulseaudio"
AUDIO_BACKEND_ALSA = "alsa"

# Volume control mode.
# HARDWARE: PA sink hardware volume via PAVolumeController (libpulse direct
#   calls, no subprocess) — used automatically for the PulseAudio/PipeWire
#   backend, including module-remap-sink.c sinks (each has an independent
#   hardware volume that does not affect its master sink or siblings).
# SOFTWARE: numpy-based PCM scaling — used for the ALSA/sounddevice backend,
#   and as an automatic fallback if PAVolumeController fails to connect.
VOLUME_CONTROL_HARDWARE = "hardware"
VOLUME_CONTROL_SOFTWARE = "software"

# Defaults
DEFAULT_PLAYER_VOLUME = 25  # initial volume for new players (percent)
DEFAULT_BUFFER_FRAMES = 1024  # sounddevice blocksize for macOS/ALSA PortAudio output (frames)

# --- Audio taper curve (dr-lex 60dB exponential, with linear roll-off) ------
#
# y = a * e^(b*x) gives constant dB-per-slider-step ("audio taper" /
# logarithmic potentiometer behavior), unlike a plain linear-amplitude
# mapping (y = x) where the bottom of the slider is wildly more sensitive
# than the top. See https://www.dr-lex.be/info-stuff/volumecontrols.html
#
# a/b are chosen for a 60dB range: y(1.0) = 1.0 (0dB), and the exponential
# alone would approach y -> a (-60dB) as x -> 0. Below _TAPER_ROLLOFF_X, a
# linear ramp to (0, 0) is used instead so volume_pct=0 is true silence
# rather than asymptotic toward -60dB.
#
# Used both for PA hardware volume (pa_simple.PAVolumeController, after a
# cube-root step to counteract PA's own cubic volume curve) and for the
# software PCM-scaling fallback path, so the same slider position sounds
# the same regardless of which volume-control mode is active.
_TAPER_A: Final = 0.001  # 10**(-60/20) -- amplitude at the -60dB reference point
_TAPER_B: Final = math.log(1.0 / _TAPER_A)  # ~6.908, gives 60dB range over x in [0, 1]
_TAPER_ROLLOFF_X: Final = 0.10  # below 10% slider, linear ramp to true silence


def volume_pct_to_amplitude(volume_pct: int) -> float:
    """
    Map a 0-100 volume percentage to a linear amplitude scale factor.

    Uses the dr-lex 60dB exponential audio taper (y = a*e^(b*x)) for
    volume_pct >= 10, giving constant dB change per slider step. Below 10%,
    a linear ramp to (0, 0) ensures volume_pct=0 produces true silence.
    """
    x = max(0, min(volume_pct, 100)) / 100.0
    if x <= 0:
        return 0.0
    if x < _TAPER_ROLLOFF_X:
        y1 = _TAPER_A * math.exp(_TAPER_B * _TAPER_ROLLOFF_X)
        return y1 * (x / _TAPER_ROLLOFF_X)
    return _TAPER_A * math.exp(_TAPER_B * x)
