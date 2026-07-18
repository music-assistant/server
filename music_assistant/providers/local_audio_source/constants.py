"""Constants for the Local Audio Source plugin."""

from __future__ import annotations

from music_assistant_models.enums import ProviderFeature

# config keys
CONF_INPUT_DEVICE = "input_device"  # e.g. "alsa:hw:1,0"
CONF_FRIENDLY_NAME = "friendly_name"  # UI label
CONF_ICON_PRESET = "icon_preset"  # bundled icon, or "custom" for CONF_THUMBNAIL_IMAGE
CONF_THUMBNAIL_IMAGE = "thumbnail_image"  # URL, only used when CONF_ICON_PRESET == "custom"

# bundled icon presets, shipped in the local images/ subfolder as "<key>.svg";
# maps preset key -> display label
ICON_PRESET_CUSTOM = "custom"
ICON_PRESETS: dict[str, str] = {
    "bluetooth": "Bluetooth Receiver",
    "cable": "Line-in / Cable",
    "chromecast": "Chromecast",
    "music": "Turntable / Vinyl",
    "stereo": "Stereo / Generic Input",
}

# fixed audio capture parameters
CHANNELS = 2  # 1=Mono, 2=Stereo
SAMPLE_RATE_HZ = 44100  # arecord -r
PERIOD_US = 10000  # arecord -F (ALSA period)
BUFFER_US = 20000  # arecord -B (small multiple of PERIOD_US)

PAUSE_DEBOUNCE_S = 0.5
RESUME_DEBOUNCE_S = 0.5

SUPPORTED_FEATURES = {ProviderFeature.AUDIO_SOURCE}

# stable id for the single AudioSource this provider exposes;
# combined with the provider instance_id this forms the persistent uri
AUDIO_SOURCE_ID = "main"
