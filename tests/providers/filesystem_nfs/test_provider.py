"""Tests for the NFS filesystem provider mount error handling."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from music_assistant_models.errors import SetupFailedError, UnsupportedSystemError

from music_assistant.providers.filesystem_nfs.provider import NFSFileSystemProvider

INSTANCE_ID = "filesystem_nfs--test"
SETUP_VALUES = {
    "host": "nas.local",
    "export_path": "/volume1/music",
    "subfolder": "",
    "nfs_version": "",
}


def _make_provider() -> NFSFileSystemProvider:
    provider = NFSFileSystemProvider.__new__(NFSFileSystemProvider)
    provider.base_path = f"/tmp/{INSTANCE_ID}"  # noqa: S108
    provider.logger = MagicMock()
    provider.config = MagicMock()
    provider.config.instance_id = INSTANCE_ID
    provider.get_setup_value = MagicMock(  # type: ignore[method-assign]
        side_effect=lambda key, default=None: SETUP_VALUES.get(key, default)
    )
    return provider


async def test_mount_failure_shows_the_summary_line() -> None:
    """A failed mount shows the summary line but keeps the full output for support."""
    summary = "mount.nfs: access denied by server while mounting nas.local:/volume1/music"
    pointer = "Refer to the nfs(5) manual page"
    provider = _make_provider()
    with (
        patch(
            "music_assistant.providers.filesystem_nfs.provider.check_output",
            AsyncMock(return_value=(32, f"{summary}\n{pointer}".encode())),
        ),
        patch(
            "music_assistant.providers.filesystem_nfs.provider.platform.system",
            return_value="Linux",
        ),
        pytest.raises(SetupFailedError) as exc_info,
    ):
        await provider.mount()
    assert exc_info.value.translation_key == "mount_failed"
    assert exc_info.value.translation_args == [summary]
    assert pointer in str(exc_info.value)


async def test_unsupported_platform() -> None:
    """A platform without NFS mount support is reported as permanently incompatible."""
    provider = _make_provider()
    with (
        patch(
            "music_assistant.providers.filesystem_nfs.provider.platform.system",
            return_value="Windows",
        ),
        pytest.raises(UnsupportedSystemError),
    ):
        await provider.mount()
