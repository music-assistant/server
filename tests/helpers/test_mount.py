"""Tests for the shared mount helpers."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from music_assistant_models.errors import LoginFailed, SetupFailedError, UnsupportedSystemError

from music_assistant.helpers.mount import (
    build_cifs_mount_cmd,
    build_nfs_mount_cmd,
    classify_mount_error,
    error_summary,
    is_mount_not_permitted,
    unmount,
)

MOUNT_PATH = "/tmp/filesystem_smb--test"  # noqa: S108
ISMOUNT = "music_assistant.helpers.mount.os.path.ismount"
CHECK_OUTPUT = "music_assistant.helpers.mount.check_output"
PLATFORM_SYSTEM = "music_assistant.helpers.mount.platform.system"
LINUX_CIFS_OPTIONS = (
    "cache=loose,iocharset=utf8,nocase,file_mode=0755,dir_mode=0755,uid=0,gid=0,noperm,nobrl,"
    "mfsymlinks,noserverino,actimeo=30"
)


def test_cifs_command_on_linux_passes_the_password_in_the_environment() -> None:
    """A password with special characters never lands on the command line."""
    cmd, env = build_cifs_mount_cmd(
        "Linux",
        "nas.local",
        "music",
        MOUNT_PATH,
        username="marcel",
        password="p,a=ss",
        version="3.0",
    )

    assert cmd == [
        "mount",
        "-t",
        "cifs",
        "-o",
        f"rw,username=marcel,vers=3.0,{LINUX_CIFS_OPTIONS}",
        "//nas.local/music",
        MOUNT_PATH,
    ]
    assert env == {"PASSWD": "p,a=ss"}
    assert not any("p,a=ss" in arg for arg in cmd)


@pytest.mark.parametrize("username", [None, "", "guest", "Guest"])
def test_cifs_command_on_linux_as_guest(username: str | None) -> None:
    """Without a user, or as the guest user, the share is mounted as guest without a password."""
    cmd, env = build_cifs_mount_cmd(
        "Linux", "nas.local", "music", MOUNT_PATH, username=username, password="unused"
    )

    assert cmd[4] == f"rw,guest,{LINUX_CIFS_OPTIONS}"
    assert env == {}


def test_cifs_command_on_linux_read_only_with_subfolder_and_cache_mode() -> None:
    """A read-only mount of a subfolder keeps the other options of today's SMB source."""
    cmd, _env = build_cifs_mount_cmd(
        "Linux", "nas.local", "music/albums", MOUNT_PATH, read_only=True, cache_mode="strict"
    )

    assert cmd[4].startswith("ro,guest,cache=strict,iocharset=utf8,")
    assert cmd[5] == "//nas.local/music/albums"


@pytest.mark.parametrize(
    ("version", "options"),
    [
        (None, []),
        # one bit per SMB major version: 1, 2 and 4
        ("1.0", ["-o", "protocol_vers_map=1"]),
        ("2.0", ["-o", "protocol_vers_map=2"]),
        ("2.1", ["-o", "protocol_vers_map=2"]),
        ("3.0", ["-o", "protocol_vers_map=4"]),
        ("3.1.1", ["-o", "protocol_vers_map=4"]),
    ],
)
def test_cifs_command_on_macos(version: str | None, options: list[str]) -> None:
    """On macOS the credentials go URL-encoded into the share URL, the version as a bitmap."""
    cmd, env = build_cifs_mount_cmd(
        "Darwin",
        "nas.local",
        "music",
        MOUNT_PATH,
        username="marcel",
        password="p@ss/word",
        version=version,
    )

    assert cmd == [
        "mount",
        "-t",
        "smbfs",
        *options,
        "//marcel:p%40ss%2Fword@nas.local/music",
        MOUNT_PATH,
    ]
    assert env == {}


@pytest.mark.parametrize(
    ("username", "share", "url"),
    [
        ("user@realm", "music", "//user%40realm:pw@nas.local/music"),
        ("marcel", "My Music", "//marcel:pw@nas.local/My%20Music"),
        ("marcel", "music/albums A-K", "//marcel:pw@nas.local/music/albums%20A-K"),
        ("DOMAIN\\marcel", "music#1/a?b", "//DOMAIN%5Cmarcel:pw@nas.local/music%231/a%3Fb"),
    ],
)
def test_cifs_url_on_macos_is_encoded(username: str, share: str, url: str) -> None:
    """The user and every part of the share path are encoded, the slashes between them kept."""
    cmd, _env = build_cifs_mount_cmd(
        "Darwin", "nas.local", share, MOUNT_PATH, username=username, password="pw"
    )

    assert cmd[-2] == url


def test_cifs_command_on_linux_keeps_names_as_they_are() -> None:
    """The Linux command is no URL: the share and the user go in as typed."""
    cmd, _env = build_cifs_mount_cmd(
        "Linux", "nas.local", "My Music/albums", MOUNT_PATH, username="user@realm", password="pw"
    )

    assert cmd[5] == "//nas.local/My Music/albums"
    assert "username=user@realm" in cmd[4]


@pytest.mark.parametrize(("username", "password"), [(None, None), ("Guest", "pw"), ("", "pw")])
def test_cifs_command_on_macos_as_guest_read_only(
    username: str | None, password: str | None
) -> None:
    """On macOS a share without a user, or as guest, is mounted as guest without a password."""
    cmd, _env = build_cifs_mount_cmd(
        "Darwin",
        "nas.local",
        "music",
        MOUNT_PATH,
        username=username,
        password=password,
        read_only=True,
    )

    assert cmd == ["mount", "-t", "smbfs", "-r", "//guest@nas.local/music", MOUNT_PATH]


@pytest.mark.parametrize(
    ("system", "options"),
    [
        ("Linux", "noatime,nolock,tcp,soft,timeo=30,retrans=5"),
        ("Darwin", "resvport,noatime,soft,timeo=30,retrans=5"),
    ],
)
def test_nfs_command(system: str, options: str) -> None:
    """An NFS export is mounted with the options of today's NFS source."""
    assert build_nfs_mount_cmd(system, "nas.local", "/volume1/music", MOUNT_PATH) == [
        "mount",
        "-t",
        "nfs",
        "-o",
        options,
        "nas.local:/volume1/music",
        MOUNT_PATH,
    ]


def test_nfs_command_read_only_with_version() -> None:
    """A read-only mount of a pinned version adds both options."""
    cmd = build_nfs_mount_cmd(
        "Linux", "nas.local", "/volume1/music", MOUNT_PATH, version="4.1", read_only=True
    )

    assert cmd[4] == "ro,noatime,nolock,tcp,soft,timeo=30,retrans=5,vers=4.1"


@pytest.mark.parametrize("system", ["Windows", "FreeBSD"])
def test_commands_on_an_unsupported_system(system: str) -> None:
    """A system without the mount tools is reported as permanently incompatible."""
    with pytest.raises(UnsupportedSystemError):
        build_cifs_mount_cmd(system, "nas.local", "music", MOUNT_PATH)
    with pytest.raises(UnsupportedSystemError):
        build_nfs_mount_cmd(system, "nas.local", "/music", MOUNT_PATH)


@pytest.mark.parametrize(
    "output",
    [
        "mount error(13): Permission denied",
        "Unable to find suitable address.NT_STATUS_LOGON_FAILURE",
        "mount_smbfs: server rejected the connection: Authentication error",
    ],
)
def test_rejected_credentials_of_a_cifs_share_are_a_login_failure(output: str) -> None:
    """Credentials the server rejected surface as a login failure."""
    assert isinstance(classify_mount_error("cifs", output), LoginFailed)


def test_busy_mountpoint_is_not_a_login_failure() -> None:
    """A non-auth failure shows the summary line but keeps the full output for support."""
    summary = "mount error(16): Device or resource busy"
    pointer = "Refer to the mount.cifs(8) manual page (e.g. man mount.cifs)"

    err = classify_mount_error("cifs", f"{summary}\n{pointer}")

    assert isinstance(err, SetupFailedError)
    assert not isinstance(err, LoginFailed)
    assert err.translation_key == "mount_failed"
    assert err.translation_args == [summary]
    assert pointer in str(err)
    assert str(err).startswith("SMB mount failed")


def test_nfs_permission_denied_is_no_login_failure() -> None:
    """An NFS server refusing an export is a failed mount: NFS has no credentials."""
    summary = "mount.nfs: access denied by server while mounting nas.local:/volume1/music"

    err = classify_mount_error("nfs", f"{summary}\nRefer to the nfs(5) manual page")

    assert not isinstance(err, LoginFailed)
    assert err.translation_args == [summary]
    assert str(err).startswith("NFS mount failed")


@pytest.mark.parametrize(
    ("output", "not_permitted"),
    [
        ("mount error(1): Operation not permitted", True),
        ("mount: only root can do that", True),
        ("mount.nfs: Operation not permitted", True),
        # the server refused the credentials: this process may mount
        ("mount error(13): Permission denied", False),
        ("mount error(112): Host is down", False),
    ],
)
def test_mount_not_permitted(output: str, not_permitted: bool) -> None:
    """Only the refusal to mount at all counts, not a refusal by the server."""
    assert is_mount_not_permitted(output) is not_permitted


def test_error_summary_drops_troubleshooting_pointer() -> None:
    """The generic pointer line mount.cifs appends is left out of the summary."""
    output = (
        "mount error(16): Device or resource busy\n"
        "Refer to the mount.cifs(8) manual page (e.g. man mount.cifs) "
        "and kernel log messages (dmesg)"
    )
    assert error_summary(output) == "mount error(16): Device or resource busy"


def test_error_summary_keeps_single_line_output() -> None:
    """A single-line output is passed through unchanged."""
    assert error_summary("mount: only root can do that") == "mount: only root can do that"


async def test_unmount_skipped_when_not_mounted() -> None:
    """A path that is not a mountpoint does not trigger any umount call."""
    with (
        patch(ISMOUNT, return_value=False),
        patch(CHECK_OUTPUT, AsyncMock()) as check_output,
    ):
        await unmount(MOUNT_PATH, MagicMock())
    check_output.assert_not_called()


async def test_unmount_success() -> None:
    """A successful umount is not escalated and not logged as a problem."""
    logger = MagicMock()
    with (
        patch(ISMOUNT, return_value=True),
        patch(CHECK_OUTPUT, AsyncMock(return_value=(0, b""))) as check_output,
    ):
        await unmount(MOUNT_PATH, logger)
    check_output.assert_awaited_once_with("umount", MOUNT_PATH)
    logger.warning.assert_not_called()


async def test_unmount_busy_escalates_lazy_on_linux() -> None:
    """A busy mountpoint is lazily detached on Linux and the failure is logged."""
    logger = MagicMock()
    check_output = AsyncMock(side_effect=[(1, b"umount: target is busy"), (0, b"")])
    with (
        patch(ISMOUNT, return_value=True),
        patch(CHECK_OUTPUT, check_output),
        patch(PLATFORM_SYSTEM, return_value="Linux"),
    ):
        await unmount(MOUNT_PATH, logger)
    assert check_output.await_args_list[0].args == ("umount", MOUNT_PATH)
    assert check_output.await_args_list[1].args == ("umount", "-l", MOUNT_PATH)
    logger.warning.assert_called_once()


async def test_unmount_busy_escalates_forced_on_macos() -> None:
    """A busy mountpoint is force-detached on macOS, which has no lazy unmount."""
    check_output = AsyncMock(side_effect=[(1, b"umount: target is busy"), (0, b"")])
    with (
        patch(ISMOUNT, return_value=True),
        patch(CHECK_OUTPUT, check_output),
        patch(PLATFORM_SYSTEM, return_value="Darwin"),
    ):
        await unmount(MOUNT_PATH, MagicMock())
    assert check_output.await_args_list[1].args == ("umount", "-f", MOUNT_PATH)


async def test_unmount_raises_when_still_mounted() -> None:
    """A mountpoint that survives the escalation is reported as a setup failure."""
    check_output = AsyncMock(side_effect=[(1, b"umount: target is busy"), (1, b"umount: failed")])
    with (
        patch(ISMOUNT, return_value=True),
        patch(CHECK_OUTPUT, check_output),
        pytest.raises(SetupFailedError) as exc_info,
    ):
        await unmount(MOUNT_PATH, MagicMock())
    assert exc_info.value.translation_key == "unmount_failed"
    assert exc_info.value.translation_args == ["umount: failed"]


async def test_unmount_no_raise_when_detached() -> None:
    """A non-zero escalation that did free the mountpoint is still a success."""
    check_output = AsyncMock(side_effect=[(1, b"umount: target is busy"), (1, b"umount: failed")])
    with (
        patch(ISMOUNT, side_effect=[True, False]),
        patch(CHECK_OUTPUT, check_output),
    ):
        await unmount(MOUNT_PATH, MagicMock())
