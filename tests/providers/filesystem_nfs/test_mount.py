"""Tests for the NFS provider's mountpoint / scan root split."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from music_assistant_models.config_entries import ConfigEntry, ProviderConfig
from music_assistant_models.enums import ConfigEntryType, ProviderType
from music_assistant_models.errors import SetupFailedError

from music_assistant.constants import CONF_LOG_LEVEL
from music_assistant.providers.filesystem_local import LocalFileSystemProvider
from music_assistant.providers.filesystem_nfs.provider import NFSFileSystemProvider

INSTANCE_ID = "filesystem_nfs--test"
MOUNT_PATH = f"/tmp/{INSTANCE_ID}"  # noqa: S108
HOST = "nas.local"
EXPORT_PATH = "/mnt/vault"


def _provider(
    subfolder: str | None = None, export_path: str = EXPORT_PATH
) -> NFSFileSystemProvider:
    """Create an NFS provider instance backed by in-memory setup data."""
    setup_data: dict[str, Any] = {
        "content_type": "music",
        "host": HOST,
        "export_path": export_path,
        "nfs_version": "3",
    }
    if subfolder is not None:
        setup_data["subfolder"] = subfolder
    config = ProviderConfig(
        values={
            # required, and CRITICAL is the only value that cannot lower the root logger
            CONF_LOG_LEVEL: ConfigEntry(
                key=CONF_LOG_LEVEL, type=ConfigEntryType.STRING, value="CRITICAL"
            )
        },
        type=ProviderType.MUSIC,
        domain="filesystem_nfs",
        instance_id=INSTANCE_ID,
    )
    manifest = MagicMock()
    manifest.domain = "filesystem_nfs"
    mass = MagicMock()
    mass.config.get = MagicMock(return_value=setup_data)
    mass.config.decrypt_string = MagicMock(side_effect=lambda value: value)
    provider = NFSFileSystemProvider(mass, manifest, config, MOUNT_PATH)
    provider.logger = MagicMock()
    return provider


@pytest.mark.parametrize("subfolder", [None, "", "   ", "/"])
def test_scan_root_is_the_mountpoint_without_subfolder(subfolder: str | None) -> None:
    """Without a subfolder the scan root is the mountpoint itself, with no trailing separator."""
    provider = _provider(subfolder)
    assert provider.mount_path == MOUNT_PATH
    assert provider.base_path == MOUNT_PATH


@pytest.mark.parametrize(
    ("subfolder", "expected"),
    [
        ("Music", "Music"),
        # a leading slash is tolerated
        ("/Music", "Music"),
        ("Music/", "Music"),
        ("albums/A-K", "albums/A-K"),
    ],
)
def test_scan_root_is_the_subfolder_inside_the_mount(subfolder: str, expected: str) -> None:
    """A configured subfolder becomes the scan root inside the (unchanged) mountpoint."""
    provider = _provider(subfolder)
    assert provider.mount_path == MOUNT_PATH
    assert provider.base_path == f"{MOUNT_PATH}/{expected}"


def test_instance_name_postfix_survives_the_config_migration() -> None:
    """
    Folding the subfolder into the export path must not rename the instance.

    The settings migration rewrites `Export=/mnt/vault` + `Subfolder=Music` to
    `Export=/mnt/vault/Music`; both must yield the same default instance name postfix.
    """
    assert _provider("Music").instance_name_postfix == "Music"
    assert _provider(export_path=f"{EXPORT_PATH}/Music").instance_name_postfix == "Music"


async def test_mount_source_never_includes_the_subfolder() -> None:
    """The mount source is the export as configured; the subfolder stays out of the argv."""
    provider = _provider("Music")
    with patch(
        "music_assistant.providers.filesystem_nfs.provider.check_output",
        AsyncMock(return_value=(0, b"")),
    ) as mock_check_output:
        await provider.mount()

    argv = mock_check_output.call_args.args
    assert f"{HOST}:{EXPORT_PATH}" in argv
    assert argv[-1] == MOUNT_PATH
    assert not any("Music" in str(arg) for arg in argv)


async def test_unmount_targets_the_mountpoint() -> None:
    """Umount must be given the mountpoint; a subdirectory of a mount cannot be unmounted."""
    provider = _provider("Music")
    mock_unmount = AsyncMock()
    with patch("music_assistant.providers.filesystem_nfs.provider.unmount", mock_unmount):
        await provider.unload()

    mock_unmount.assert_awaited_once_with(MOUNT_PATH, provider.logger)


async def test_only_the_mountpoint_is_ever_created() -> None:
    """MA creates the mountpoint but never the subfolder, on either side of the mount."""
    provider = _provider("Music")
    mock_makedirs = AsyncMock()
    with (
        patch(
            "music_assistant.providers.filesystem_nfs.provider.get_ip_from_host",
            AsyncMock(return_value="192.0.2.10"),
        ),
        patch.object(provider, "mount", AsyncMock()),
        patch("music_assistant.providers.filesystem_nfs.provider.unmount", AsyncMock()),
        patch.object(provider, "check_write_access", AsyncMock()),
        patch(
            "music_assistant.providers.filesystem_nfs.provider.exists",
            AsyncMock(return_value=False),
        ),
        patch("music_assistant.providers.filesystem_nfs.provider.makedirs", mock_makedirs),
        patch(
            "music_assistant.providers.filesystem_nfs.provider.isdir",
            AsyncMock(return_value=True),
        ),
    ):
        await provider.handle_async_init()

    # created before the mount it would be masked; after it, MA writes to the user's share
    mock_makedirs.assert_awaited_once_with(MOUNT_PATH)


async def test_diagnostics_report_the_mountpoint_mount_state() -> None:
    """A subdirectory of a mount is not itself a mountpoint, so ismount must get mount_path."""
    provider = _provider("Music")
    with (
        patch(
            "music_assistant.providers.filesystem_nfs.provider.ismount",
            AsyncMock(return_value=True),
        ) as mock_ismount,
        patch.object(
            LocalFileSystemProvider,
            "get_diagnostics",
            AsyncMock(return_value={"write_access": True}),
        ),
    ):
        diagnostics = await provider.get_diagnostics()

    mock_ismount.assert_awaited_once_with(MOUNT_PATH)
    assert diagnostics["mounted"] is True


def test_traversal_subfolder_is_rejected_before_mounting() -> None:
    """A subfolder escaping the mount is refused while deriving the scan root."""
    with pytest.raises(SetupFailedError) as exc_info:
        _provider("../../etc")

    assert exc_info.value.translation_key == "invalid_subfolder"
    assert exc_info.value.translation_owner == "provider.filesystem_nfs"
    assert MOUNT_PATH not in str(exc_info.value)


@pytest.mark.parametrize(
    "cleanup_error",
    [
        # the mountpoint turned out to be busy
        SetupFailedError("Unable to unmount"),
        # umount itself is missing or not executable
        FileNotFoundError("umount"),
        PermissionError("umount"),
    ],
)
async def test_failed_cleanup_never_masks_the_missing_subfolder(
    cleanup_error: Exception,
) -> None:
    """The best-effort unmount must not replace the one error the user can act on."""
    provider = _provider("Music")
    with (
        patch(
            "music_assistant.providers.filesystem_nfs.provider.get_ip_from_host",
            AsyncMock(return_value="192.0.2.10"),
        ),
        patch.object(provider, "mount", AsyncMock()),
        patch(
            "music_assistant.providers.filesystem_nfs.provider.unmount",
            # only the post-mount cleanup fails; a blocked pre-mount unmount is a real error
            AsyncMock(side_effect=[None, cleanup_error]),
        ),
        patch(
            "music_assistant.providers.filesystem_nfs.provider.exists",
            AsyncMock(return_value=True),
        ),
        patch(
            "music_assistant.providers.filesystem_nfs.provider.isdir",
            AsyncMock(return_value=False),
        ),
        pytest.raises(SetupFailedError) as exc_info,
    ):
        await provider.handle_async_init()

    assert exc_info.value.translation_key == "subfolder_not_found"


async def test_missing_subfolder_fails_setup_instead_of_an_empty_library() -> None:
    """A subfolder that does not exist inside the mount surfaces a translated setup error."""
    provider = _provider("Music")
    mock_unmount = AsyncMock()
    with (
        patch(
            "music_assistant.providers.filesystem_nfs.provider.get_ip_from_host",
            AsyncMock(return_value="192.0.2.10"),
        ),
        patch.object(provider, "mount", AsyncMock()),
        patch("music_assistant.providers.filesystem_nfs.provider.unmount", mock_unmount),
        patch(
            "music_assistant.providers.filesystem_nfs.provider.exists",
            AsyncMock(return_value=True),
        ),
        patch(
            "music_assistant.providers.filesystem_nfs.provider.isdir",
            AsyncMock(return_value=False),
        ),
        pytest.raises(SetupFailedError) as exc_info,
    ):
        await provider.handle_async_init()

    err = exc_info.value
    assert err.translation_key == "subfolder_not_found"
    assert err.translation_owner == "provider.filesystem_nfs"
    assert err.translation_args == ["Music"]
    # the internal mountpoint must not leak into user-facing text
    assert MOUNT_PATH not in str(err)
    mock_unmount.assert_awaited()
