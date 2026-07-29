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

# Pre-warm PA streams at provider startup (PulseAudio backend only).
# Eliminates per-player stream-open latency (~50-200ms) that would otherwise
# become a fixed sync offset within sync groups. Trade-off: held-open streams
# keep the sound device active (sink RUNNING) while the provider runs,
# preventing idle suspend — negligible on PCI cards, but potentially costly
# for USB audio devices on virtualized hosts. Default on; disable on such
# setups if sync-group alignment is not needed.
CONF_PREWARM_STREAMS = "prewarm_streams"

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

# --- Audio taper curve (dr-lex exponential, with linear roll-off) -----------
#
# y = a * e^(b*x) gives constant dB-per-slider-step ("audio taper" /
# logarithmic potentiometer behavior), unlike a plain linear-amplitude
# mapping (y = x) where the bottom of the slider is wildly more sensitive
# than the top. See https://www.dr-lex.be/info-stuff/volumecontrols.html
#
# a = 10**(-range_dB/20) sets the amplitude floor; b = ln(1/a) ensures
# y(1.0) = 1.0 (0dB) at full volume. Below _TAPER_ROLLOFF_X, a linear ramp
# to (0, 0) is used so volume_pct=0 is true silence rather than asymptoting
# toward the floor.
#
# Reference values for common dB ranges (pick one _TAPER_A and comment out
# the rest; _TAPER_B recalculates automatically):
#
#   Range   _TAPER_A   _TAPER_B   MA 70% =    Notes
#   40 dB   0.01       ~4.605     -12 dB      receiver / outdoor speakers (current)
#   50 dB   0.003162   ~5.757     -15 dB      medium-range setups
#   60 dB   0.001      ~6.908     -18 dB      consumer headphones / desktop speakers
#   70 dB   0.000316   ~8.059     -21 dB      high-dynamic-range hi-fi systems
#
# Used both for PA hardware volume (pa_simple.PAVolumeController, after a
# cube-root step to counteract PA's own cubic volume curve) and for the
# software PCM-scaling fallback path, so the same slider position sounds
# the same regardless of which volume-control mode is active.
_TAPER_A: Final = 0.01  # 10**(-40/20) — 40dB range, suits receiver/outdoor setups
# _TAPER_A: Final = 0.003162  # 10**(-50/20) — 50dB range
# _TAPER_A: Final = 0.001     # 10**(-60/20) — 60dB range, suits headphones/desktop
# _TAPER_A: Final = 0.000316  # 10**(-70/20) — 70dB range, hi-fi high dynamic range
_TAPER_B: Final = math.log(1.0 / _TAPER_A)  # recalculates automatically from _TAPER_A
_TAPER_ROLLOFF_X: Final = 0.10  # below 10% slider, linear ramp to true silence


def volume_pct_to_amplitude(volume_pct: int) -> float:
    """
    Map a 0-100 volume percentage to a linear amplitude scale factor.

    Uses the dr-lex exponential audio taper (y = a*e^(b*x)) for
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