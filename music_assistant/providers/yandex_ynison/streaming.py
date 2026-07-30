"""
PCM normalization helpers for Yandex Music audio streams.

Contains format profiles, the AudioFormat factory, and ffmpeg probe args.
"""

from __future__ import annotations

from typing import Any

from music_assistant_models.enums import ContentType
from music_assistant_models.media_items import AudioFormat

# PCM normalization profiles by YM quality tier.
# Ensures MA's single ffmpeg receives a consistent format between tracks.
# NOTE: AudioFormat is a *mutable* dataclass — MA's FFMpeg._log_reader_task
# mutates input_format.codec_type in-place.  We MUST create a fresh copy for
# every place that stores a reference (StreamDetails.audio_format, PreBuffer,
# ffmpeg output_format) so that mutation of one doesn't corrupt the others.
# No-hint lossless floor is CD-rate (44.1 kHz): most of the Yandex lossless
# catalogue is 44.1 kHz FLAC, and with MA's AudioSource passthrough this PCM is
# delivered to the player as-is, so a missing format hint must not upsample the
# common case to 48 kHz. A real hint or explicit config still lifts the rate.
PCM_LOSSLESS_PARAMS: dict[str, Any] = {
    "content_type": ContentType.PCM_S24LE,
    "sample_rate": 44100,
    "bit_depth": 24,
    "channels": 2,
}
PCM_LOSSY_PARAMS: dict[str, Any] = {
    "content_type": ContentType.PCM_S16LE,
    "sample_rate": 44100,
    "bit_depth": 16,
    "channels": 2,
}

# Override MA's default probesize (8096) and analyzeduration (500000).
# Flac-MP4 containers can have moov/stsd atoms beyond 8KB, and slow CDN
# responses over pipe input may cause analyseduration timeouts — both of
# which result in ffmpeg failing to detect the container and producing
# garbage PCM (white noise).
PROBE_ARGS: list[str] = ["-probesize", "65536", "-analyzeduration", "5000000"]


def make_pcm_format(params: dict[str, Any]) -> AudioFormat:
    """Create a fresh AudioFormat from stored params (safe from mutation)."""
    return AudioFormat(**params)
