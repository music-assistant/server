"""Constants for Local Audio Out provider."""

from __future__ import annotations

import uuid

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
# Both modes map the slider through the shared audio taper curve
# (helpers.pulse_capture.volume_pct_to_amplitude) so the same slider
# position sounds the same regardless of which mode is active.
VOLUME_CONTROL_HARDWARE = "hardware"
VOLUME_CONTROL_SOFTWARE = "software"

# Defaults
DEFAULT_PLAYER_VOLUME = 25  # initial volume for new players (percent)
DEFAULT_BUFFER_FRAMES = 1024  # sounddevice blocksize for macOS/ALSA PortAudio output (frames)
