"""Helpers to manage OS-level mounts of remote filesystems."""

from __future__ import annotations

import asyncio
import os
import platform
from typing import TYPE_CHECKING
from urllib.parse import quote

from music_assistant_models.errors import LoginFailed, SetupFailedError, UnsupportedSystemError

from music_assistant.helpers.process import check_output

if TYPE_CHECKING:
    from logging import Logger

# lowercase fragments that both mount tools (Linux mount.cifs and macOS mount_smbfs) emit when
# the server rejected the credentials - only those must be reported back as an auth problem
_AUTH_FAILURE_MARKERS = (
    "permission denied",
    "authentication error",
    "nt_status_logon_failure",
    "nt_status_access_denied",
    "nt_status_account_disabled",
    "nt_status_account_locked_out",
    "nt_status_password_expired",
    "nt_status_wrong_password",
)
# lowercase fragments the mount tools emit when the process itself may not mount anything
_NOT_PERMITTED_MARKERS = ("operation not permitted", "only root can")
# the bit of each SMB major version in the protocol_vers_map of macOS
_SMBFS_VERSION_BITS = {"1": 1, "2": 2, "3": 4}


def build_cifs_mount_cmd(
    system: str,
    server: str,
    share: str,
    mountpoint: str,
    *,
    username: str | None = None,
    password: str | None = None,
    version: str | None = None,
    read_only: bool = False,
) -> tuple[list[str], dict[str, str]]:
    """
    Return the command that mounts a CIFS (SMB) share, and the environment variables it needs.

    On Linux the password travels in the PASSWD environment variable, so a password with
    special characters (commas, etc.) needs no escaping on the command line.

    :param system: The operating system, as ``platform.system()`` names it.
    :param server: The hostname or IP address of the server.
    :param share: The share name.
    :param mountpoint: The local folder to mount the share on.
    :param username: The user to log in as, None or ``guest`` for guest access.
    :param password: The password of the user.
    :param version: The SMB protocol version, None to let the client negotiate it.
    :param read_only: Whether to mount the share read-only.
    :raises UnsupportedSystemError: When the system can not mount a CIFS share.
    """
    is_guest = not username or username.lower() == "guest"
    if system == "Darwin":
        mount_options = ["-r"] if read_only else []
        # macOS takes the allowed SMB major versions as a bitmap (nsmb.conf): 1, 2 and 4 for
        # SMB 1, 2 and 3
        if version and (version_bit := _SMBFS_VERSION_BITS.get(version.split(".")[0])):
            mount_options.extend(["-o", f"protocol_vers_map={version_bit}"])
        # the credentials and the share are parts of a URL, so special characters are encoded
        encoded_password = f":{quote(password, safe='')}" if password and not is_guest else ""
        user = "guest" if is_guest else quote(str(username), safe="")
        path = "/".join(quote(part, safe="") for part in share.split("/"))
        url = f"//{user}{encoded_password}@{server}/{path}"
        return ["mount", "-t", "smbfs", *mount_options, url, mountpoint], {}
    if system != "Linux":
        msg = f"Mounting a CIFS share is not supported on {system}"
        raise UnsupportedSystemError(msg)
    env_vars: dict[str, str] = {}
    options = ["ro" if read_only else "rw"]
    if not is_guest:
        options.append(f"username={username}")
        if password:
            env_vars["PASSWD"] = password
    else:
        options.append("guest")
    # SMB version for better compatibility and performance
    if version:
        options.append(f"vers={version}")
    options.append("cache=loose")
    # Case insensitive by default (standard for SMB) and other performance options.
    # Note: emoji and other 4-byte UTF-8 characters (U+10000+) in folder/file names
    # are NOT supported due to a Linux kernel limitation in the CIFS client's NLS layer.
    # Items with such characters will be skipped during library sync.
    options.extend(
        [
            "iocharset=utf8",
            "nocase",
            "file_mode=0755",
            "dir_mode=0755",
            "uid=0",
            "gid=0",
            "noperm",
            "nobrl",
            "mfsymlinks",
            "noserverino",
            "actimeo=30",
        ]
    )
    mount_cmd = ["mount", "-t", "cifs", "-o", ",".join(options), f"//{server}/{share}", mountpoint]
    return mount_cmd, env_vars


def build_nfs_mount_cmd(
    system: str,
    server: str,
    export_path: str,
    mountpoint: str,
    *,
    version: str | None = None,
    read_only: bool = False,
) -> list[str]:
    """
    Return the command that mounts an NFS export.

    :param system: The operating system, as ``platform.system()`` names it.
    :param server: The hostname or IP address of the server.
    :param export_path: The absolute path of the export on the server.
    :param mountpoint: The local folder to mount the export on.
    :param version: The NFS protocol version, None to let the client negotiate it.
    :param read_only: Whether to mount the export read-only.
    :raises UnsupportedSystemError: When the system can not mount an NFS export.
    """
    if system == "Darwin":
        options = ["resvport", "noatime", "soft", "timeo=30", "retrans=5"]
    elif system == "Linux":
        options = ["noatime", "nolock", "tcp", "soft", "timeo=30", "retrans=5"]
    else:
        msg = f"Mounting an NFS export is not supported on {system}"
        raise UnsupportedSystemError(msg)
    if read_only:
        options.insert(0, "ro")
    if version:
        options.append(f"vers={version}")
    return ["mount", "-t", "nfs", "-o", ",".join(options), f"{server}:{export_path}", mountpoint]


def classify_mount_error(share_type: str, output: str) -> SetupFailedError | LoginFailed:
    """
    Return the error for a failed mount of a network share.

    Credentials a CIFS server rejected become a login failure, anything else a failed mount that
    shows the summary line of the output.

    :param share_type: The protocol of the share: ``cifs`` or ``nfs``.
    :param output: The output of the mount command, or the message of whatever mounted it.
    """
    label = "SMB" if share_type == "cifs" else "NFS"
    msg = f"{label} mount failed with error: {output}"
    if share_type == "cifs" and any(marker in output.lower() for marker in _AUTH_FAILURE_MARKERS):
        return LoginFailed(msg)
    return SetupFailedError(
        msg,
        translation_key="mount_failed",
        translation_args=[error_summary(output)],
    )


def is_mount_not_permitted(output: str) -> bool:
    """
    Return whether a mount command failed because this process may not mount at all.

    :param output: The output of the mount command.
    """
    lowered = output.lower()
    return any(marker in lowered for marker in _NOT_PERMITTED_MARKERS)


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
