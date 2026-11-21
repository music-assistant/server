"""Spotify Soloist implementation of the Spotify Connect backend."""

from .api_key import find_reusable_api_key
from .backend import (
    VOLUME_MODE_PLAYER_ONLY,
    VOLUME_MODE_SYNC_SPOTIFY,
    SoloistBackend,
)
from .prefs import write_audio_prefs
from .runtime import (
    BuildExpiredError,
    ConsentRequiredError,
    DownloadFailedError,
    InvalidArchiveError,
    SoloistBinaryManager,
    SoloistClient,
    SoloistError,
    UnsupportedPlatformError,
    verify_platform_supported,
)

__all__ = [
    "VOLUME_MODE_PLAYER_ONLY",
    "VOLUME_MODE_SYNC_SPOTIFY",
    "BuildExpiredError",
    "ConsentRequiredError",
    "DownloadFailedError",
    "InvalidArchiveError",
    "SoloistBackend",
    "SoloistBinaryManager",
    "SoloistClient",
    "SoloistError",
    "UnsupportedPlatformError",
    "find_reusable_api_key",
    "verify_platform_supported",
    "write_audio_prefs",
]
