"""Tests for the shared mount helpers."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from music_assistant_models.errors import SetupFailedError

from music_assistant.helpers.mount import error_summary, unmount

MOUNT_PATH = "/tmp/filesystem_smb--test"  # noqa: S108
ISMOUNT = "music_assistant.helpers.mount.os.path.ismount"
CHECK_OUTPUT = "music_assistant.helpers.mount.check_output"
PLATFORM_SYSTEM = "music_assistant.helpers.mount.platform.system"


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
