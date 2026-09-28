"""Constants for the storage controller."""

from __future__ import annotations

from typing import Final

REFRESH_INTERVAL: Final[int] = 60
REFRESH_TASK_ID: Final[str] = "storage_refresh"
# how long a caller waits for the probe of a location (in seconds): a network share whose server
# is gone can block a probe much longer, and counts as not answering meanwhile
PROBE_TIMEOUT: Final[float] = 10
# how long the answer of a probe is used before a caller that needs the location probes again
PROBE_MAX_AGE: Final[float] = 30
# measuring the data and cache directories walks every file in them, so it is repeated at most
# this often (in seconds)
DIR_SIZE_MAX_AGE: Final[int] = 600
DIR_SIZES_TASK_ID: Final[str] = "storage_dir_sizes"
MAX_LISTED_FOLDERS: Final[int] = 500

# files whose presence marks a Docker or Podman container
CONTAINER_MARKER_FILES: Final[tuple[str, ...]] = ("/.dockerenv", "/run/.containerenv")
