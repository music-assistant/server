"""Helpers to manage OS-level mounts of remote filesystems."""

from __future__ import annotations

import asyncio
import os
import platform
from typing import TYPE_CHECKING

from music_assistant_models.errors import SetupFailedError

from music_assistant.helpers.process import check_output

if TYPE_CHECKING:
    from logging import Logger


def error_summary(output: str) -> str:
    """
    Return the summary line of a (u)mount tool's output, for display to the user.

    :param output: The decoded output of the (u)mount command.
    """
    # the mount tools state the actual problem on the first line and then append generic
    # troubleshooting pointers (man pages, dmesg) that are noise in a UI message
    for line in output.splitlines():
        if stripped := line.strip():
            return stripped
    return ""


async def unmount(path: str, logger: Logger) -> None:
    """
    Unmount the given path, ensuring it is free for a new mount afterwards.

    Does nothing if the path is not a mountpoint.

    :param path: The (local) mountpoint to unmount.
    :param logger: Logger to report a failed (regular) unmount on.
    :raises SetupFailedError: If the path could not be freed.
    """
    if not await asyncio.to_thread(os.path.ismount, path):
        return
    returncode, output = await check_output("umount", path)
    if returncode == 0:
        return
    error = output.decode().strip()
    logger.warning("Unmount of %s failed with error: %s", path, error)
    # a busy mountpoint keeps blocking a new mount on the same path, so detach it anyway:
    # lazy detach on Linux (frees the mountpoint immediately, even with files still open)
    # and the forced variant on macOS, which has no lazy equivalent.
    detach_flag = "-f" if platform.system() == "Darwin" else "-l"
    returncode, output = await check_output("umount", detach_flag, path)
    if returncode != 0 and await asyncio.to_thread(os.path.ismount, path):
        error = output.decode().strip()
        msg = f"Unable to unmount {path}: {error}"
        raise SetupFailedError(
            msg,
            translation_key="unmount_failed",
            translation_args=[error_summary(error)],
        )
