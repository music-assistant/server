"""Spotify Soloist implementation of the Spotify Connect backend."""

from .backend import (
    VOLUME_MODE_PLAYER_ONLY,
    VOLUME_MODE_SYNC_SPOTIFY,
    SoloistBackend,
)
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
    "verify_platform_supported",
]
