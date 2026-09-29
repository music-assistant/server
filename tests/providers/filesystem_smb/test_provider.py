"""Tests for the SMB filesystem provider mount error handling."""

from __future__ import annotations

import logging
from contextlib import suppress
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from music_assistant_models.config_entries import ProviderConfig
from music_assistant_models.enums import ProviderStatus, ProviderType
from music_assistant_models.errors import LoginFailed, SetupFailedError, UnsupportedSystemError

from music_assistant.controllers.config.helpers import _provider_status
from music_assistant.mass import _provider_error_from_exc
from music_assistant.providers.filesystem_smb import SMBFileSystemProvider
from tests.common import capture_log_records

INSTANCE_ID = "filesystem_smb--test"
SETUP_VALUES = {
    "host": "nas.local",
    "share": "music",
    "subfolder": "",
    "username": "user",
    "password": "secret",
    "smb_version": "",
}


def _make_provider(**setup_values: str) -> SMBFileSystemProvider:
    values = {**SETUP_VALUES, **setup_values}
    provider = SMBFileSystemProvider.__new__(SMBFileSystemProvider)
    provider.base_path = f"/tmp/{INSTANCE_ID}"  # noqa: S108
    provider.logger = MagicMock()
    provider.config = MagicMock()
    provider.config.instance_id = INSTANCE_ID
    provider.get_setup_value = MagicMock(  # type: ignore[method-assign]
        side_effect=lambda key, default=None: values.get(key, default)
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


async def test_permission_denied_is_an_auth_error() -> None:
    """A rejected credential (mount.cifs) surfaces as a login failure."""
    err = await _mount_with_output("mount error(13): Permission denied")
    assert isinstance(err, LoginFailed)


async def test_mount_command() -> None:
    """The share, subfolder, credentials, version and cache mode reach the mount command."""
    provider = _make_provider(subfolder="albums\\A-K", smb_version="3.0")
    provider.config.get_value = MagicMock(return_value="strict")  # type: ignore[method-assign]
    with (
        patch(
            "music_assistant.providers.filesystem_smb.check_output",
            AsyncMock(return_value=(0, b"")),
        ) as check_output,
        patch("music_assistant.providers.filesystem_smb.platform.system", return_value="Linux"),
    ):
        await provider.mount()

    args = check_output.call_args.args
    assert args[-2:] == ("//nas.local/music/albums/A-K", provider.base_path)
    assert args[4].startswith("rw,username=user,vers=3.0,cache=strict,")
    assert check_output.call_args.kwargs == {"env": {"PASSWD": "secret"}}


async def test_unsupported_platform() -> None:
    """A platform without SMB mount support is reported as permanently incompatible."""
    provider = _make_provider()
    with (
        patch("music_assistant.providers.filesystem_smb.platform.system", return_value="Windows"),
        pytest.raises(UnsupportedSystemError),
    ):
        await provider.mount()


@pytest.mark.parametrize("system", ["Darwin", "Linux"])
@pytest.mark.parametrize("returncode", [0, 1])
async def test_mount_logs_no_password(system: str, returncode: int) -> None:
    """A mount logs what is mounted where, and leaves no trace of the password at any level."""
    provider = _make_provider(password="pa ss@word,1")
    provider.logger = logging.getLogger(f"{__name__}.mount")
    with (
        capture_log_records(provider.logger) as records,
        patch(
            "music_assistant.providers.filesystem_smb.check_output",
            AsyncMock(return_value=(returncode, b"mount error(112): Host is down")),
        ),
        patch("music_assistant.providers.filesystem_smb.platform.system", return_value=system),
        suppress(SetupFailedError),
    ):
        await provider.mount()

    assert f"Mounting //nas.local/music to {provider.base_path}" in [
        record.getMessage() for record in records
    ]
    for record in records:
        text = f"{record.getMessage()} {record.args}"
        assert "pa ss@word,1" not in text
        assert "pa%20ss%40word%2C1" not in text
