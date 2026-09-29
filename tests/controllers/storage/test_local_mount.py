"""Tests for network shares the server mounts itself."""

from __future__ import annotations

import logging
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from music_assistant_models.errors import LoginFailed, MusicAssistantError, SetupFailedError

from music_assistant.controllers.storage.backends import local_mount
from music_assistant.controllers.storage.backends.base import BackendUnavailable, ShareState
from music_assistant.controllers.storage.backends.local_mount import (
    LocalMounter,
    create_local_mounter,
    get_local_mount_support,
)
from music_assistant.controllers.storage.constants import SHARES_DOCS_URL, TRANSLATION_OWNER
from music_assistant.controllers.storage.models import MountBackend, NetworkShareSpec, ShareType
from tests.controllers.storage.conftest import mount_line

ALL_CIFS = ["1.0", "2.0", "2.1", "3.0", "3.1.1"]
ALL_NFS = ["3", "4", "4.1", "4.2"]
BOTH = {ShareType.CIFS: ALL_CIFS, ShareType.NFS: ALL_NFS}
HELPERS = ("mount.cifs", "mount.nfs")
SYS_ADMIN = 1 << 21
DAC_READ_SEARCH = 1 << 2
# the effective capabilities of root in a container started without extra capabilities (it has
# neither of the two), and with --cap-add SYS_ADMIN --cap-add DAC_READ_SEARCH
DOCKER_DEFAULT_CAPS = 0x00000000A80425FB
DOCKER_MOUNT_CAPS = DOCKER_DEFAULT_CAPS | SYS_ADMIN | DAC_READ_SEARCH
FULL_ROOT_CAPS = 0x000001FFFFFFFFFF


def _helpers(*installed: str) -> Callable[[str], bool]:
    """Return a lookup of the mount helpers that finds the given ones."""
    return lambda binary: binary in installed


@pytest.mark.parametrize(
    ("system", "euid", "cap_eff", "installed", "supported", "reason"),
    [
        ("Linux", 0, FULL_ROOT_CAPS, HELPERS, BOTH, None),
        ("Linux", 0, DOCKER_MOUNT_CAPS, HELPERS, BOTH, None),
        ("Linux", 0, DOCKER_MOUNT_CAPS, ("mount.cifs",), {ShareType.CIFS: ALL_CIFS}, None),
        ("Linux", 0, DOCKER_MOUNT_CAPS, ("mount.nfs",), {ShareType.NFS: ALL_NFS}, None),
        ("Linux", 0, DOCKER_MOUNT_CAPS, (), {}, "no mount helpers"),
        # each capability missing on its own, and both
        ("Linux", 0, DOCKER_DEFAULT_CAPS | DAC_READ_SEARCH, HELPERS, {}, "no CAP_SYS_ADMIN"),
        ("Linux", 0, DOCKER_DEFAULT_CAPS | SYS_ADMIN, HELPERS, {}, "no CAP_DAC_READ_SEARCH"),
        (
            "Linux",
            0,
            DOCKER_DEFAULT_CAPS,
            HELPERS,
            {},
            "no CAP_SYS_ADMIN and no CAP_DAC_READ_SEARCH",
        ),
        ("Linux", 0, None, HELPERS, {}, "no CAP_SYS_ADMIN and no CAP_DAC_READ_SEARCH"),
        ("Linux", 1000, FULL_ROOT_CAPS, HELPERS, {}, "not running as root"),
        ("Darwin", 501, None, (), {ShareType.CIFS: ALL_CIFS}, None),
        ("Darwin", 0, None, (), BOTH, None),
        ("Windows", None, None, HELPERS, {}, "not supported on Windows"),
    ],
)
def test_what_this_process_can_mount(
    system: str,
    euid: int | None,
    cap_eff: int | None,
    installed: tuple[str, ...],
    supported: dict[ShareType, list[str]],
    reason: str | None,
) -> None:
    """
    Linux needs root, the capabilities to mount and read any folder, and the mount helper.

    macOS mounts NFS as root only, any other system mounts nothing.
    """
    result, problem = get_local_mount_support(system, euid, cap_eff, _helpers(*installed))

    assert result == supported
    if reason is None:
        assert problem is None
    else:
        assert problem is not None
        assert reason in problem


async def test_system_without_user_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    """A system that does not know user ids reports that it can not mount, and nothing breaks."""
    monkeypatch.setattr(f"{local_mount.__name__}.platform.system", lambda: "Windows")
    monkeypatch.delattr(f"{local_mount.__name__}.os.geteuid", raising=False)

    with pytest.raises(BackendUnavailable, match="not supported on Windows"):
        await create_local_mounter(logging.getLogger(__name__))


async def test_backend_reads_the_capabilities(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The effective capability set comes from the status of the process."""
    status = tmp_path / "status"
    status.write_text("Name:\tmass\nCapInh:\t0000000000000000\nCapEff:\t00000000a80425fb\n")
    monkeypatch.setattr(local_mount, "PROC_STATUS_PATH", str(status))
    monkeypatch.setattr(f"{local_mount.__name__}.platform.system", lambda: "Linux")
    monkeypatch.setattr(f"{local_mount.__name__}.os.geteuid", lambda: 0)
    monkeypatch.setattr(local_mount, "_has_helper", lambda _binary: True)

    with pytest.raises(BackendUnavailable, match="CAP_SYS_ADMIN"):
        await create_local_mounter(logging.getLogger(__name__))

    status.write_text("CapEff:\t00000000a82425ff\n")
    mounter = await create_local_mounter(logging.getLogger(__name__))
    assert mounter.backend == MountBackend.LOCAL_MOUNT
    assert set(mounter.supported_versions) == {ShareType.CIFS, ShareType.NFS}


@pytest.fixture
def mounter(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> LocalMounter:
    """
    Provide a mounter on Linux that mounts below a temporary folder.

    :param tmp_path: Temporary directory for the mountpoints.
    :param monkeypatch: Pytest monkeypatch fixture.
    """
    monkeypatch.setattr(local_mount, "MOUNT_ROOT", str(tmp_path))
    monkeypatch.setattr(f"{local_mount.__name__}.platform.system", lambda: "Linux")
    return LocalMounter(
        {ShareType.CIFS: ALL_CIFS, ShareType.NFS: ALL_NFS}, logging.getLogger(__name__)
    )


def _spec(mounter: LocalMounter, share_type: ShareType = ShareType.CIFS) -> NetworkShareSpec:
    """Return a share of the mounter."""
    return NetworkShareSpec(
        name="music",
        share_type=share_type,
        server="nas.local",
        share="music" if share_type == ShareType.CIFS else "/volume1/music",
        backend=MountBackend.LOCAL_MOUNT,
        path=mounter.get_path("music"),
        username="marcel" if share_type == ShareType.CIFS else None,
        version="3.0" if share_type == ShareType.CIFS else "4.1",
        read_only=True,
    )


async def test_add_cifs(mounter: LocalMounter, monkeypatch: pytest.MonkeyPatch) -> None:
    """A CIFS share is mounted on its own folder, the password in the environment."""
    check_output = AsyncMock(return_value=(0, b""))
    monkeypatch.setattr(local_mount, "check_output", check_output)
    spec = _spec(mounter)

    await mounter.add(spec, "secret")

    assert Path(spec.path).is_dir()
    assert check_output.await_args is not None
    args = check_output.await_args.args
    assert args[:4] == ("mount", "-t", "cifs", "-o")
    assert args[4].startswith("ro,username=marcel,vers=3.0,cache=loose,")
    assert args[5:] == ("//nas.local/music", spec.path)
    assert check_output.await_args.kwargs == {"env": {"PASSWD": "secret"}}


async def test_add_nfs(mounter: LocalMounter, monkeypatch: pytest.MonkeyPatch) -> None:
    """An NFS export is mounted with the options of today's NFS source."""
    check_output = AsyncMock(return_value=(0, b""))
    monkeypatch.setattr(local_mount, "check_output", check_output)
    spec = _spec(mounter, ShareType.NFS)

    await mounter.add(spec, None)

    assert check_output.await_args is not None
    assert check_output.await_args.args == (
        "mount",
        "-t",
        "nfs",
        "-o",
        "ro,noatime,nolock,tcp,soft,timeo=30,retrans=5,vers=4.1",
        "nas.local:/volume1/music",
        spec.path,
    )


@pytest.mark.parametrize(
    ("output", "error", "translation_key"),
    [
        ("mount error(1): Operation not permitted", SetupFailedError, "mount_not_permitted"),
        ("mount: only root can do that", SetupFailedError, "mount_not_permitted"),
        ("mount error(13): Permission denied", LoginFailed, "login_failed"),
        (
            "mount error(112): Host is down\nRefer to the mount.cifs(8)",
            SetupFailedError,
            "mount_failed",
        ),
    ],
)
async def test_failed_mount(
    mounter: LocalMounter,
    monkeypatch: pytest.MonkeyPatch,
    output: str,
    error: type[MusicAssistantError],
    translation_key: str,
) -> None:
    """A refusal to mount at all points to the docs; other failures are the tool's own."""
    monkeypatch.setattr(local_mount, "check_output", AsyncMock(return_value=(1, output.encode())))

    with pytest.raises(error) as exc_info:
        await mounter.add(_spec(mounter), "secret")

    assert exc_info.value.translation_key == translation_key
    if translation_key == "mount_not_permitted":
        assert exc_info.value.translation_owner == TRANSLATION_OWNER
        assert exc_info.value.translation_args == [SHARES_DOCS_URL]
    if translation_key == "mount_failed":
        assert exc_info.value.translation_args == ["mount error(112): Host is down"]


async def test_mount_tool_missing(mounter: LocalMounter, monkeypatch: pytest.MonkeyPatch) -> None:
    """A mount command that can not run at all is a failed mount."""
    monkeypatch.setattr(
        local_mount, "check_output", AsyncMock(side_effect=FileNotFoundError("mount"))
    )

    with pytest.raises(SetupFailedError) as exc_info:
        await mounter.add(_spec(mounter), None)

    assert exc_info.value.translation_key == "mount_failed"


async def test_reload_and_remove(mounter: LocalMounter, monkeypatch: pytest.MonkeyPatch) -> None:
    """A reload unmounts before it mounts again; a removal also removes the empty folder."""
    unmount = AsyncMock()
    monkeypatch.setattr(local_mount, "unmount", unmount)
    monkeypatch.setattr(local_mount, "check_output", AsyncMock(return_value=(0, b"")))
    spec = _spec(mounter)

    await mounter.reload(spec, None)
    await mounter.update(spec, None)
    assert [call.args[0] for call in unmount.await_args_list] == [spec.path, spec.path]
    assert Path(spec.path).is_dir()

    await mounter.remove(spec)
    assert not Path(spec.path).exists()


async def test_share_states(mounter: LocalMounter, monkeypatch: pytest.MonkeyPatch) -> None:
    """The mount table tells which shares need to be mounted, without touching them."""
    mounted = _spec(mounter)
    unmounted = NetworkShareSpec(
        name="movies",
        share_type=ShareType.CIFS,
        server="nas.local",
        share="movies",
        backend=MountBackend.LOCAL_MOUNT,
        path=mounter.get_path("movies"),
    )
    table = "\n".join((mount_line("/", "ext4"), mount_line(mounted.path, "cifs")))
    monkeypatch.setattr(local_mount, "read_mountinfo", lambda: table)

    assert await mounter.get_states([mounted, unmounted]) == {
        "music": ShareState.PRESENT,
        "movies": ShareState.MISSING,
    }


@pytest.mark.parametrize("system", ["Darwin", "Linux"])
@pytest.mark.parametrize("returncode", [0, 1])
async def test_mount_logs_no_password(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    system: str,
    returncode: int,
) -> None:
    """A mount leaves no trace of the password in the log, at any level."""
    monkeypatch.setattr(local_mount, "MOUNT_ROOT", str(tmp_path))
    monkeypatch.setattr(f"{local_mount.__name__}.platform.system", lambda: system)
    monkeypatch.setattr(
        local_mount,
        "check_output",
        AsyncMock(return_value=(returncode, b"mount error(112): Host is down")),
    )
    mounter = LocalMounter(
        {ShareType.CIFS: ALL_CIFS}, logging.getLogger("music_assistant.test.local_mount")
    )
    caplog.set_level(1)

    with suppress(SetupFailedError):
        await mounter.add(_spec(mounter), "pa ss@word,1")

    assert caplog.records
    for record in caplog.records:
        text = f"{record.getMessage()} {record.args}"
        assert "pa ss@word,1" not in text
        assert "pa%20ss%40word%2C1" not in text
