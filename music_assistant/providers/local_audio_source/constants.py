"""Constants for the Local Audio Source plugin."""

from __future__ import annotations

from music_assistant_models.enums import ProviderFeature

# config keys
CONF_INPUT_DEVICE = "input_device"  # PulseAudio/PipeWire source name, e.g. "alsa_input.usb-..."
CONF_FRIENDLY_NAME = "friendly_name"  # UI label
CONF_ICON_PRESET = "icon_preset"  # bundled icon, or "custom" for CONF_THUMBNAIL_IMAGE
CONF_THUMBNAIL_IMAGE = "thumbnail_image"  # URL, only used when CONF_ICON_PRESET == "custom"
CONF_INCLUDE_MONITORS = "include_monitors"  # bool: show sink monitor sources in the picker
CONF_AUTO_TRIGGER = "auto_trigger"  # bool: watch signal level and auto play/stop
CONF_TARGET_PLAYER_ID = "target_player_id"  # player to auto-start when signal is detected
CONF_TRIGGER_THRESHOLD_DBFS = "trigger_threshold_dbfs"  # float, dBFS RMS level to treat as signal

# sentinel for "auto-pick a target player"
# PLAYER_ID_AUTO: prefer whichever player is currently playing, else fall
# back to the first available player
PLAYER_ID_AUTO = "__auto__"

# bundled icon presets, shipped in the local images/ subfolder as "<key>.svg".
# Display labels for these (and for ICON_PRESET_CUSTOM) live in strings.json
# under config_entries.icon_preset.options.<value>, not here — this is just
# the set of valid preset keys, used for iteration and validation.
ICON_PRESET_CUSTOM = "custom"
ICON_PRESETS: tuple[str, ...] = (
    "bluetooth",
    "cable",
    "chromecast",
    "music",
    "stereo",
)

# fixed audio capture parameters — PulseAudio/PipeWire resample/remap to this
# spec on the server side, so it's safe to request regardless of the
# selected source's native format.
CHANNELS = 2  # 1=Mono, 2=Stereo
SAMPLE_RATE_HZ = 44100

PAUSE_DEBOUNCE_S = 0.5
RESUME_DEBOUNCE_S = 0.5

# signal-detection sensor tuning (used only when CONF_AUTO_TRIGGER is enabled)
DEFAULT_TRIGGER_THRESHOLD_DBFS = -50.0  # RMS level above which the source counts as "active"
SENSOR_CHUNK_MS = 50  # how often the sensor samples the source while idle
TRIGGER_ATTACK_S = 0.3  # signal must stay above threshold this long before we start playback
TRIGGER_RELEASE_S = 5.0  # signal must stay below threshold this long before we stop playback
SENSOR_RETRY_S = 5.0  # backoff when the sensor can't open the configured source
# if an auto-triggered play_media() never results in on_source_selected()
# claiming the queue within this long (target player unreachable, playback
# failed to start, etc), drop the pending claim so the sensor can retry
# instead of being wedged waiting for a start that's never coming
TRIGGER_PENDING_TIMEOUT_S = 25.0

SUPPORTED_FEATURES = {ProviderFeature.AUDIO_SOURCE}

# stable id for the single AudioSource this provider exposes;
# combined with the provider instance_id this forms the persistent uri
AUDIO_SOURCE_ID = "main"
