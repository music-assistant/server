"""Helper functions for nicovideo provider."""

from music_assistant.providers.nicovideo.helpers.hls_models import (
    HLSSegment,
    ParsedHLSPlaylist,
)
from music_assistant.providers.nicovideo.helpers.hls_seek_optimizer import (
    HLSSeekOptimizer,
    SeekOptimizedStreamContext,
)
from music_assistant.providers.nicovideo.helpers.utils import (
    AlbumWithTracks,
    PlaylistWithTracks,
    create_audio_format,
    log_verbose,
)

__all__ = [
    "AlbumWithTracks",
    "HLSSeekOptimizer",
    "HLSSegment",
    "ParsedHLSPlaylist",
    "PlaylistWithTracks",
    "SeekOptimizedStreamContext",
    "create_audio_format",
    "log_verbose",
]
