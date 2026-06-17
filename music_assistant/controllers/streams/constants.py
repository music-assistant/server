"""Constants for the Streams Controller."""

from __future__ import annotations

from enum import StrEnum
from typing import Final

from music_assistant.helpers.util import get_total_system_memory


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

# RAM thresholds (in GB) for the buffer-size presets. Balanced needs >= 4GB.
# Maximum uses 7GB rather than a literal 8GB so machines marketed as "8GB" that
# report slightly less still qualify for the largest buffer: integrated GPUs carve
# out shared system memory, and container runtimes report their cgroup limit.
BALANCED_MIN_RAM_GB: Final[float] = 4.0
MAXIMUM_MIN_RAM_GB: Final[float] = 7.0

# Buffer size in seconds for each preset
BUFFER_SIZE_MAP: Final[dict[str, int]] = {
    BufferSize.MINIMAL: 60,
    BufferSize.BALANCED: 300,
    BufferSize.MAXIMUM: 1200,
}

# Buffer size for radio streams (short rolling buffer)
RADIO_BUFFER_SIZE: Final[int] = 15


# Configuration keys
CONF_BUFFER_SIZE: Final[str] = "buffer_size"


def get_available_buffer_sizes() -> list[BufferSize]:
    """
    Return the buffer-size presets allowed for this host's RAM.

    Minimal is always available; Balanced requires >= 4GB and Maximum >= 7GB. When total
    memory is unknown (0.0, e.g. Windows) all presets are offered (fail open).
    """
    if TOTAL_SYSTEM_MEMORY_GB == 0.0:
        return [BufferSize.MINIMAL, BufferSize.BALANCED, BufferSize.MAXIMUM]
    sizes = [BufferSize.MINIMAL]
    if TOTAL_SYSTEM_MEMORY_GB >= BALANCED_MIN_RAM_GB:
        sizes.append(BufferSize.BALANCED)
    if TOTAL_SYSTEM_MEMORY_GB >= MAXIMUM_MIN_RAM_GB:
        sizes.append(BufferSize.MAXIMUM)
    return sizes


def _get_default_buffer_size() -> str:
    if TOTAL_SYSTEM_MEMORY_GB >= MAXIMUM_MIN_RAM_GB:
        return BufferSize.MAXIMUM
    if TOTAL_SYSTEM_MEMORY_GB >= BALANCED_MIN_RAM_GB:
        return BufferSize.BALANCED
    return BufferSize.MINIMAL


CONF_BUFFER_SIZE_DEFAULT: Final[str] = _get_default_buffer_size()
CONF_ALLOW_CROSSFADE_SAME_ALBUM: Final[str] = "allow_crossfade_same_album"
CONF_SMART_FADES_LOG_LEVEL: Final[str] = "smart_fades_log_level"

# Maximum seconds we wait for the buffer to catch up on a forward seek.
# Beyond this, the stream is re-fetched at the seek position.
SEEK_WAIT_THRESHOLD: Final[int] = 20

# Streams webserver default port
DEFAULT_PORT: Final[int] = 8097

# Cache constants for resolved radio URLs
CACHE_CATEGORY_RESOLVED_RADIO_URL: Final[int] = 100
CACHE_PROVIDER: Final[str] = "audio"
