"""Tests for the SMB filesystem provider mount error handling."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from music_assistant_models.config_entries import ProviderConfig
from music_assistant_models.enums import ProviderStatus, ProviderType
from music_assistant_models.errors import LoginFailed, SetupFailedError, UnsupportedSystemError

from music_assistant.controllers.config.helpers import _provider_status
from music_assistant.mass import _provider_error_from_exc
from music_assistant.providers.filesystem_smb import SMBFileSystemProvider

INSTANCE_ID = "filesystem_smb--test"
SETUP_VALUES = {
    "host": "nas.local",
    "share": "music",
    "subfolder": "",
    "username": "user",
    "password": "secret",
    "smb_version": "",
}


def _make_provider() -> SMBFileSystemProvider:
    provider = SMBFileSystemProvider.__new__(SMBFileSystemProvider)
    provider.base_path = f"/tmp/{INSTANCE_ID}"  # noqa: S108
    provider.logger = MagicMock()
    provider.config = MagicMock()
    provider.config.instance_id = INSTANCE_ID
    provider.get_setup_value = MagicMock(  # type: ignore[method-assign]
        side_effect=lambda key, default=None: SETUP_VALUES.get(key, default)
    )
    return provider


async def _mount_with_output(output: str, system: str = "Linux") -> BaseException:
    """Run a mount that fails with the given tool output and return the raised error."""
    provider = _make_provider()
    with (
        patch(
            "music_assistant.providers.filesystem_smb.check_output",
            AsyncMock(return_value=(1, output.encode())),
        ),
        patch("music_assistant.providers.filesystem_smb.platform.system", return_value=system),
        pytest.raises(Exception) as exc_info,  # noqa: PT011
    ):
        await provider.mount()
    return exc_info.value


def _status_for(err: BaseException) -> ProviderStatus:
    """Derive the provider status the UI would show for a failed load."""
    conf = ProviderConfig(
        values={},
        type=ProviderType.MUSIC,
        domain="filesystem_smb",
        instance_id=INSTANCE_ID,
        last_error=_provider_error_from_exc(err),
    )
    return _provider_status(conf, is_loaded=False)


async def test_busy_mountpoint_is_not_an_auth_error() -> None:
    """A busy mountpoint surfaces as a plain setup error, not as invalid credentials."""
    err = await _mount_with_output("mount error(16): Device or resource busy")
    assert isinstance(err, SetupFailedError)
    assert not isinstance(err, LoginFailed)
    assert _status_for(err) == ProviderStatus.ERROR


async def test_busy_mountpoint_keeps_the_tool_output() -> None:
    """A non-auth mount failure shows the summary line but keeps the full output for support."""
    summary = "mount error(16): Device or resource busy"
    pointer = "Refer to the mount.cifs(8) manual page (e.g. man mount.cifs)"
    err = await _mount_with_output(f"{summary}\n{pointer}")
    assert isinstance(err, SetupFailedError)
    assert err.translation_key == "mount_failed"
    assert err.translation_args == [summary]
    assert pointer in str(err)


async def test_permission_denied_is_an_auth_error() -> None:
    """A rejected credential (mount.cifs) surfaces as a login failure."""
    err = await _mount_with_output("mount error(13): Permission denied")
    assert isinstance(err, LoginFailed)


async def test_nt_status_logon_failure_is_an_auth_error() -> None:
    """A rejected credential reported as an NT status code surfaces as a login failure."""
    err = await _mount_with_output("Unable to find suitable address.NT_STATUS_LOGON_FAILURE")
    assert isinstance(err, LoginFailed)


async def test_unsupported_platform() -> None:
    """A platform without SMB mount support is reported as permanently incompatible."""
    provider = _make_provider()
    with (
        patch("music_assistant.providers.filesystem_smb.platform.system", return_value="Windows"),
        pytest.raises(UnsupportedSystemError),
    ):
        await provider.mount()
