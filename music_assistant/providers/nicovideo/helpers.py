"""Utility functions for handling cookies and converting them into Netscape format."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from mashumaro import DataClassDictMixin

# Playlist, Album, and Track cannot be placed under TYPE_CHECKING
# because they are used at runtime by DataClassDictMixin
from music_assistant_models.media_items import (
    Album,
    AudioFormat,
    Playlist,
    Track,
)

from music_assistant.constants import VERBOSE_LOG_LEVEL
from music_assistant.providers.nicovideo.constants import (
    NICOVIDEO_AUDIO_BIT_DEPTH,
    NICOVIDEO_AUDIO_CHANNELS,
    NICOVIDEO_CODEC_TYPE,
    NICOVIDEO_CONTENT_TYPE,
)

if TYPE_CHECKING:
    import logging

    from music_assistant.models.music_provider import MusicProvider


@dataclass
class PlaylistWithTracks(DataClassDictMixin):
    """Helper class to hold playlist and its tracks."""

    playlist: Playlist
    tracks: list[Track]


@dataclass
class AlbumWithTracks(DataClassDictMixin):
    """Helper class to hold album and its tracks."""

    album: Album
    tracks: list[Track]


async def cache_track(provider: MusicProvider, track: Track) -> None:
    """Cache single track with provider item cache.

    Note: While MusicAssistant's get_provider_item automatically handles cache retrieval
    and storage, this helper function is needed for explicit cache updates (e.g., when
    adding album information to tracks) since MusicAssistant doesn't provide a dedicated
    cache-only update function.
    """
    cache_key = f"track.{track.item_id}"
    await provider.mass.cache.set(
        cache_key,
        track.to_dict(),
        provider=provider.lookup_key,
    )


def log_verbose(logger: logging.Logger, message: str, *args: object) -> None:
    """Log a message at VERBOSE level with performance optimization.

    Args:
        logger: Logger instance
        message: Log message format string
        *args: Arguments for the message format string
    """
    if logger.isEnabledFor(VERBOSE_LOG_LEVEL):
        logger.log(VERBOSE_LOG_LEVEL, message, *args)


def create_audio_format(
    *, bit_rate: int | None = None, sample_rate: int | None = None
) -> AudioFormat:
    """Create AudioFormat from stream format data."""
    audio_format = AudioFormat(
        content_type=NICOVIDEO_CONTENT_TYPE,
        codec_type=NICOVIDEO_CODEC_TYPE,
        channels=NICOVIDEO_AUDIO_CHANNELS,
        bit_depth=NICOVIDEO_AUDIO_BIT_DEPTH,
    )

    if bit_rate is not None:
        audio_format.bit_rate = bit_rate
    if sample_rate is not None:
        audio_format.sample_rate = sample_rate

    return audio_format
