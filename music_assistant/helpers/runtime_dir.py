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
import tempfile
from pathlib import Path
from typing import Final

from music_assistant.constants import MASS_LOGGER_NAME

LOGGER = logging.getLogger(f"{MASS_LOGGER_NAME}.helpers.runtime_dir")

# tmpfs in Docker, in the Home Assistant app and on every systemd host
SHM_PATH: Final[str] = "/dev/shm"  # noqa: S108
# systemd-logind (RemoveIPC) wipes a regular user's files in /dev/shm when their last login
# session ends; a host without systemd, such as a container, has no logind to do that
FIRST_REGULAR_UID: Final[int] = 1000
SYSTEMD_RUN_PATH: Final[str] = "/run/systemd/system"


def prepare_runtime_dir(cache_path: str) -> str:
    """
    Create an empty runtime directory, private to this server, for its FIFOs and sockets.

    The directory is on the shared-memory tmpfs when that is safe for the server's user,
    otherwise in the system temp dir, and under the cache path as a last resort. Anything left
    in it by a previous run is removed. Blocking: call it from a worker thread.

    :param cache_path: The server's cache path, which ties the directory to this server.
    :returns: Absolute path of the directory, accessible to the server's user only.
    """
    cache_dir = Path(cache_path).resolve()
    # the shared dirs serve every process and user on a host, so the name is derived from the
    # cache path, which differs per server
    name = f"music-assistant-{hashlib.sha256(str(cache_dir).encode()).hexdigest()[:12]}"
    bases: list[str] = []
    if os.getuid() < FIRST_REGULAR_UID or not Path(SYSTEMD_RUN_PATH).is_dir():
        bases.append(SHM_PATH)
    try:
        bases.append(tempfile.gettempdir())
    except OSError as err:
        LOGGER.debug("Runtime dir cannot use a temp dir: %s", err)
    for base in bases:
        runtime_dir = Path(base, name)
        try:
            _create_empty_private_dir(runtime_dir)
        except OSError as err:
            LOGGER.debug("Runtime dir cannot use %s: %s", base, err)
        else:
            LOGGER.debug("Runtime dir: %s", runtime_dir)
            return str(runtime_dir)
    # the server owns its cache dir, so a leftover it cannot remove is reused, not refused
    fallback_dir = cache_dir / "runtime"
    shutil.rmtree(fallback_dir, ignore_errors=True)
    fallback_dir.mkdir(mode=0o700, exist_ok=True)
    fallback_dir.chmod(0o700)
    LOGGER.debug("Runtime dir: %s", fallback_dir)
    return str(fallback_dir)


def _create_empty_private_dir(path: Path) -> None:
    """Create path as a new, empty directory only its owner can access."""
    # a directory (or symlink) another user holds under this name survives the removal, and
    # the mkdir then refuses it: the result is always a directory this process just created
    shutil.rmtree(path, ignore_errors=True)
    path.mkdir(mode=0o700)
