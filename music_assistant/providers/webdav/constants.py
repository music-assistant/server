"""Constants for WebDAV filesystem provider."""

from __future__ import annotations

from typing import Final

# Config keys
CONF_URL = "url"
CONF_VERIFY_SSL = "verify_ssl"
CONF_CONTENT_TYPE = "content_type"

# File extensions - reuse from filesystem_local patterns
TRACK_EXTENSIONS: Final[tuple[str, ...]] = (
    "mp3",
    "flac",
    "wav",
    "m4a",
    "aac",
    "ogg",
    "wma",
    "opus",
    "mp4",
    "m4p",
)

PLAYLIST_EXTENSIONS: Final[tuple[str, ...]] = ("m3u", "m3u8", "pls")

AUDIOBOOK_EXTENSIONS: Final[tuple[str, ...]] = (
    "mp3",
    "flac",
    "m4a",
    "m4b",
    "aac",
    "ogg",
    "opus",
    "wav",
)

PODCAST_EPISODE_EXTENSIONS: Final[tuple[str, ...]] = TRACK_EXTENSIONS

IMAGE_EXTENSIONS: Final[tuple[str, ...]] = ("jpg", "jpeg", "png", "gif", "bmp", "webp", "tiff")

SUPPORTED_EXTENSIONS: Final[tuple[str, ...]] = (
    *TRACK_EXTENSIONS,
    *PLAYLIST_EXTENSIONS,
    *AUDIOBOOK_EXTENSIONS,
    *IMAGE_EXTENSIONS,
)

# WebDAV specific constants
WEBDAV_TIMEOUT: Final[int] = 30

# Concurrent processing limit
MAX_CONCURRENT_TASKS: Final[int] = 3
