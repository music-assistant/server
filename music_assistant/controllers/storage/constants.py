"""Constants for the storage controller."""

from __future__ import annotations

from typing import Final

from music_assistant.controllers.storage.models import StorageKind

REFRESH_INTERVAL: Final[int] = 60
REFRESH_TASK_ID: Final[str] = "storage_refresh"
# measuring the data and cache directories walks every file in them, so it is repeated at most
# this often (in seconds)
DIR_SIZE_MAX_AGE: Final[int] = 600
DIR_SIZES_TASK_ID: Final[str] = "storage_dir_sizes"
MAX_LISTED_FOLDERS: Final[int] = 500

# the kinds of media location a caller that does not manage every music source may see
MEMBER_VISIBLE_KINDS: Final[frozenset[StorageKind]] = frozenset(
    {
        StorageKind.BUILTIN_MEDIA,
        StorageKind.CONTAINER_VOLUME,
        StorageKind.NETWORK_SHARE,
        StorageKind.REMOVABLE,
    }
)

# files whose presence marks a Docker or Podman container
CONTAINER_MARKER_FILES: Final[tuple[str, ...]] = ("/.dockerenv", "/run/.containerenv")
