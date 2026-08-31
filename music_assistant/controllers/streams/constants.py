"""Constants for the Streams Controller."""

from __future__ import annotations

from enum import StrEnum
from typing import Final

from music_assistant_models.enums import VolumeNormalizationMode

from music_assistant.helpers.util import get_total_system_memory, meets_memory_target

# What the volume normalization preference falls back to.
DEFAULT_VOLUME_NORMALIZATION_MODE: Final = VolumeNormalizationMode.FALLBACK_DYNAMIC

# Modes that are only ever an outcome, never something to ask for: SOURCE is set by a
# source that levels its own audio and UNKNOWN is what an unrecognised value
# deserializes to. Neither is offered as a preference, and one that reaches the config
# anyway is not honoured as one - it would otherwise be handed straight back as the
# mode to apply, which for SOURCE also means claiming a source levelled the audio.
OUTCOME_ONLY_NORMALIZATION_MODES: Final = (
    VolumeNormalizationMode.SOURCE,
    VolumeNormalizationMode.UNKNOWN,
)


class BufferMode(StrEnum):
    """Buffer mode determines buffer behavior."""

    SEEKABLE = "seekable"
    ROLLING = "rolling"


class BufferSize(StrEnum):
    """Buffer size presets for configuration."""

    MINIMAL = "minimal"
    BALANCED = "balanced"
    MAXIMUM = "maximum"


# Calculate total system memory once at module load time
TOTAL_SYSTEM_MEMORY_GB: Final[float] = get_total_system_memory()

# RAM thresholds for the buffer-size presets, as NOMINAL targets. Both are checked via
# meets_memory_target(), which absorbs the gap between a host's nominal size and what it
# reports (kernel MemTotal reservation plus any integrated-GPU carve-out), so a "4GB" box
# (reporting ~3.8GB) and an "8GB" box (reporting ~7.4GB) both qualify for their tier.
BALANCED_MIN_RAM_GB: Final[float] = 4.0
MAXIMUM_MIN_RAM_GB: Final[float] = 8.0

# Buffer size in seconds for each preset
BUFFER_SIZE_MAP: Final[dict[str, int]] = {
    BufferSize.MINIMAL: 60,
    BufferSize.BALANCED: 300,
    BufferSize.MAXIMUM: 1200,
}

# Buffer size for radio streams (short rolling buffer)
RADIO_BUFFER_SIZE: Final[int] = 15

# Ceiling on how fast a single queue item is handed to a player, once it has had its opening
# burst. Music Assistant serves audio for listening, not for collecting: at twice playback the
# player's buffer still grows in realtime, while pulling a whole catalogue takes about as long
# as listening to it would. These are the fastest we go, not a target - a player that needs
# feeding more gently (Chromecast is the known case) can be paced slower than this.
# Do not remove this to "fix" slow buffering; raise the burst instead. See the usage policy.
SINGLE_ITEM_READRATE: Final[str] = "1.2"
SINGLE_ITEM_READRATE_INITIAL_BURST: Final[str] = "60"

# Pacing for an item from a realtime source (streamdetails.is_realtime). The head
# start such a source banks into the item's buffer is the only material its
# end-of-track crossfade can be built from; the standard burst would hand it all to
# the player at stream open and the 1.2x drain would keep the buffer empty from then
# on. Matching the source's ceiling keeps it resident until EOF, the player still
# receives at least playback speed, and a realtime source cannot be collected faster
# than it plays. Buffered sources keep the standard pacing (MusicCast needs the big
# opening burst before it will do gapless at all).
REALTIME_ITEM_READRATE: Final[str] = "1.1"
REALTIME_ITEM_READRATE_INITIAL_BURST: Final[str] = "5"


def single_item_pacing_args(is_realtime: bool) -> list[str]:
    """
    Return the ffmpeg pacing arguments for one queue item's output stream.

    :param is_realtime: Whether the item's source is marked realtime.
    """
    if is_realtime:
        return [
            "-readrate",
            REALTIME_ITEM_READRATE,
            "-readrate_initial_burst",
            REALTIME_ITEM_READRATE_INITIAL_BURST,
        ]
    return [
        "-readrate",
        SINGLE_ITEM_READRATE,
        "-readrate_initial_burst",
        SINGLE_ITEM_READRATE_INITIAL_BURST,
    ]


# Time to keep the flow stream response open after the last audio byte of a queue.
# Players buffer a few seconds ahead of what they actually render; some of them drop
# that buffer the moment the connection is closed, cutting off the end of the queue.
# Holding the (idle) connection open gives them time to play it out first. Kept below
# the webserver shutdown timeout so a lead-out never stalls a restart of the server.
FLOW_STREAM_LEAD_OUT_SECONDS: Final[int] = 8


# Configuration keys
CONF_BUFFER_SIZE: Final[str] = "buffer_size"


def get_available_buffer_sizes() -> list[BufferSize]:
    """
    Return the buffer-size presets allowed for this host's RAM.

    Minimal is always available; Balanced needs ~4GB and Maximum ~8GB (both within the
    reporting tolerance). When total memory is unknown (0.0, e.g. Windows) all presets are
    offered (fail open).
    """
    if TOTAL_SYSTEM_MEMORY_GB == 0.0:
        return [BufferSize.MINIMAL, BufferSize.BALANCED, BufferSize.MAXIMUM]
    sizes = [BufferSize.MINIMAL]
    if meets_memory_target(TOTAL_SYSTEM_MEMORY_GB, BALANCED_MIN_RAM_GB):
        sizes.append(BufferSize.BALANCED)
    if meets_memory_target(TOTAL_SYSTEM_MEMORY_GB, MAXIMUM_MIN_RAM_GB):
        sizes.append(BufferSize.MAXIMUM)
    return sizes


def _get_default_buffer_size() -> str:
    # Unknown memory (0.0) picks the conservative Minimal default, unlike the
    # available-presets list which fails open — meets_memory_target() also fails open,
    # so the 0.0 case is handled explicitly here before consulting it.
    if TOTAL_SYSTEM_MEMORY_GB == 0.0:
        return BufferSize.MINIMAL
    if meets_memory_target(TOTAL_SYSTEM_MEMORY_GB, MAXIMUM_MIN_RAM_GB):
        return BufferSize.MAXIMUM
    if meets_memory_target(TOTAL_SYSTEM_MEMORY_GB, BALANCED_MIN_RAM_GB):
        return BufferSize.BALANCED
    return BufferSize.MINIMAL


CONF_BUFFER_SIZE_DEFAULT: Final[str] = _get_default_buffer_size()
CONF_ALLOW_CROSSFADE_SAME_ALBUM: Final[str] = "allow_crossfade_same_album"
CONF_SMART_FADES_LOG_LEVEL: Final[str] = "smart_fades_log_level"

# Maximum wait for a provider source-stream slot before a speculative attempt gives up.
STREAM_SLOT_WAIT_TIMEOUT: Final[float] = 5.0

# Total capacity budget when an actual playback start retries/reselects provider mappings.
STREAM_SLOT_PLAYBACK_WAIT_TIMEOUT: Final[float] = 15.0

# Maximum time spent searching other streaming providers for an alternative mapping
# when every known candidate is capacity-saturated.
STREAM_SLOT_MATCH_TIMEOUT: Final[float] = 5.0

# Maximum seconds we wait for the buffer to catch up on a forward seek.
# Beyond this, the stream is re-fetched at the seek position.
SEEK_WAIT_THRESHOLD: Final[int] = 20

# Streams webserver default port
DEFAULT_PORT: Final[int] = 8097

# Cache constants for resolved radio URLs
CACHE_CATEGORY_RESOLVED_RADIO_URL: Final[int] = 100
CACHE_PROVIDER: Final[str] = "audio"

# StreamDetails.data key providers set to opt into the in-band title handoff.
STREAMDETAILS_INBAND_TITLE_HANDOFF_KEY: Final[str] = "inband_title_handoff"
# StreamDetails.data key where the streams controller records the in-band (ICY)
# stream title after an opted-in provider takes ownership of stream_metadata
# (StreamDetails.stream_title is a derived view whose setter would overwrite it).
STREAMDETAILS_INBAND_TITLE_KEY: Final[str] = "inband_stream_title"
