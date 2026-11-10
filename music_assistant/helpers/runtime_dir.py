"""
Private runtime directory for the FIFOs and sockets the server creates.

A read or write on a FIFO updates its inode times. On a disk filesystem that can wait for a
journal or transaction commit for seconds, even with O_NONBLOCK, which stalls the event loop that
reads the audio. A FIFO on tmpfs never touches a disk.
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
from pathlib import Path
from typing import Final

from music_assistant.constants import MASS_LOGGER_NAME

LOGGER = logging.getLogger(f"{MASS_LOGGER_NAME}.helpers.runtime_dir")

# tmpfs in Docker, in the Home Assistant app and on every systemd host
SHM_PATH: Final[str] = "/dev/shm"  # noqa: S108
# systemd-logind (RemoveIPC) wipes a regular user's files in /dev/shm when their last login
# session ends, so only root and system users get it
FIRST_REGULAR_UID: Final[int] = 1000


def prepare_runtime_dir(cache_path: str) -> str:
    """
    Create an empty runtime directory, private to this server, for its FIFOs and sockets.

    The directory is on the shared-memory tmpfs when that is available to the server's user,
    otherwise under the cache path. Anything left in it by a previous run is removed.
    Blocking: call it from a worker thread.

    :param cache_path: The server's cache path, which ties the directory to this server.
    :returns: Absolute path of the directory, accessible to the server's user only.
    """
    cache_dir = Path(cache_path).resolve()
    if os.getuid() < FIRST_REGULAR_UID:
        # /dev/shm is shared by every process and user on a host, so the name is derived from
        # the cache path, which differs per server
        instance_key = hashlib.sha256(str(cache_dir).encode()).hexdigest()[:12]
        shm_dir = Path(SHM_PATH, f"music-assistant-{instance_key}")
        try:
            _create_empty_private_dir(shm_dir)
        except OSError as err:
            LOGGER.debug(
                "Runtime dir falls back to the cache path, %s is unusable: %s", SHM_PATH, err
            )
        else:
            return str(shm_dir)
    fallback_dir = cache_dir / "runtime"
    _create_empty_private_dir(fallback_dir)
    return str(fallback_dir)


def _create_empty_private_dir(path: Path) -> None:
    """Create path as a new, empty directory only its owner can access."""
    # a directory (or symlink) another user holds under this name survives the removal, and
    # the mkdir then refuses it: the result is always a directory this process just created
    shutil.rmtree(path, ignore_errors=True)
    path.mkdir(mode=0o700)
