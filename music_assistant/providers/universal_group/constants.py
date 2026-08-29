"""Universal Group Player constants."""

from __future__ import annotations

from typing import Final

from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption
from music_assistant_models.enums import ConfigEntryType, ContentType, PlayerFeature
from music_assistant_models.media_items import AudioFormat

from music_assistant.helpers.ffmpeg import DEFAULT_MP3_BIT_RATE

UGP_PREFIX: Final[str] = "ugp_"

# Features the group has no hardware of its own for: the player manager fans these
# commands out to the members. They are advertised only when a member can actually
# carry them out, which is the same condition under which the group state for them
# (group_volume / group_volume_muted) resolves to a value.
EXTRA_FEATURES_FROM_MEMBERS: Final[set[PlayerFeature]] = {
    PlayerFeature.VOLUME_SET,
    PlayerFeature.VOLUME_MUTE,
}

# Grace period (seconds) before a universal group releases its members after
# the active stream naturally ends (playback_state transitions to IDLE without
# an explicit stop). Matches the SyncGroup grace window.
IDLE_GRACE_SECONDS: Final[float] = 10.0

# Static output format options for the UGP stream. UGP is a heterogeneous-protocol
# compatibility group, not a hi-res delivery path; every member receives the same
# encoded stream, so the format is a single deliberate choice instead of a per-member
# negotiation.
CONF_UGP_OUTPUT_FORMAT: Final[str] = "ugp_output_format"

UGP_OUTPUT_MP3: Final[str] = "mp3"
UGP_OUTPUT_FLAC_44100_16: Final[str] = "flac_44100_16"
UGP_OUTPUT_FLAC_48000_24: Final[str] = "flac_48000_24"

# Map each option to (audio format the server will encode/serve, URL extension).
# The audio format also drives the internal PCM pivot used by UGPStream so the
# multiplexer runs at exactly the rate the encoder needs.
UGP_OUTPUT_FORMATS: Final[dict[str, tuple[AudioFormat, str]]] = {
    UGP_OUTPUT_MP3: (
        AudioFormat(
            content_type=ContentType.MP3,
            codec_type=ContentType.MP3,
            sample_rate=44100,
            bit_depth=16,
            bit_rate=DEFAULT_MP3_BIT_RATE,
        ),
        "mp3",
    ),
    UGP_OUTPUT_FLAC_44100_16: (
        AudioFormat(content_type=ContentType.FLAC, sample_rate=44100, bit_depth=16),
        "flac",
    ),
    UGP_OUTPUT_FLAC_48000_24: (
        AudioFormat(content_type=ContentType.FLAC, sample_rate=48000, bit_depth=24),
        "flac",
    ),
}


CONFIG_ENTRY_UGP_NOTE = ConfigEntry(
    key="ugp_note",
    type=ConfigEntryType.ALERT,
    required=False,
)

CONF_ENTRY_UGP_OUTPUT_FORMAT = ConfigEntry(
    key=CONF_UGP_OUTPUT_FORMAT,
    type=ConfigEntryType.STRING,
    options=[
        ConfigValueOption(UGP_OUTPUT_MP3),
        ConfigValueOption(UGP_OUTPUT_FLAC_44100_16),
        ConfigValueOption(UGP_OUTPUT_FLAC_48000_24),
    ],
    default_value=UGP_OUTPUT_MP3,
    category="generic",
    requires_reload=True,
)


def resolve_ugp_output_format(option: str) -> tuple[AudioFormat, str]:
    """Return the (output AudioFormat, URL extension) for a configured UGP option."""
    return UGP_OUTPUT_FORMATS.get(option, UGP_OUTPUT_FORMATS[UGP_OUTPUT_MP3])
