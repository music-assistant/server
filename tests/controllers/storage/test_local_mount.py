"""Tests for network shares the server mounts itself."""

from __future__ import annotations

import logging
import os
import stat
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from music_assistant_models.errors import LoginFailed, MusicAssistantError, SetupFailedError

from music_assistant.constants import CONF_STORAGE_SHARES
from music_assistant.controllers.storage import StorageController
from music_assistant.controllers.storage.backends import local_mount
from music_assistant.controllers.storage.backends.base import BackendUnavailable, ShareState
from music_assistant.controllers.storage.backends.local_mount import (
    LocalMounter,
    create_local_mounter,
    get_local_mount_support,
)
from music_assistant.controllers.storage.constants import SHARES_DOCS_URL, TRANSLATION_OWNER
from music_assistant.controllers.storage.models import MountBackend, NetworkShareSpec, ShareType
from tests.common import capture_log_records
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
    Provide a mounter on Linux whose mount root is a folder that does not exist yet.

    :param tmp_path: Temporary directory for the mountpoints.
    :param monkeypatch: Pytest monkeypatch fixture.
    """
    monkeypatch.setattr(local_mount, "MOUNT_ROOT", str(tmp_path / "mounts"))
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
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, system: str, returncode: int
) -> None:
    """A mount logs what is mounted where, and leaves no trace of the password at any level."""
    monkeypatch.setattr(local_mount, "MOUNT_ROOT", str(tmp_path / "mounts"))
    monkeypatch.setattr(f"{local_mount.__name__}.platform.system", lambda: system)
    monkeypatch.setattr(
        local_mount,
        "check_output",
        AsyncMock(return_value=(returncode, b"mount error(112): Host is down")),
    )
    mounter = LocalMounter({ShareType.CIFS: ALL_CIFS}, logging.getLogger(f"{__name__}.mount"))
    spec = _spec(mounter)

    with capture_log_records(mounter.logger) as records, suppress(SetupFailedError):
        await mounter.add(spec, "pa ss@word,1")

    assert f"Mounting network share music on {spec.path}" in [
        record.getMessage() for record in records
    ]
    for record in records:
        text = f"{record.getMessage()} {record.args}"
        assert "pa ss@word,1" not in text
        assert "pa%20ss%40word%2C1" not in text


async def test_fresh_mount_root_is_private(
    mounter: LocalMounter, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The mount root is created for this process alone."""
    monkeypatch.setattr(local_mount, "check_output", AsyncMock(return_value=(0, b"")))

    await mounter.add(_spec(mounter), "secret")

    assert stat.S_IMODE((tmp_path / "mounts").stat().st_mode) == 0o700
    assert (tmp_path / "mounts" / "music").is_dir()


def _symlinked_root(tmp_path: Path) -> str:
    (tmp_path / "elsewhere").mkdir()
    (tmp_path / "mounts").symlink_to(tmp_path / "elsewhere", target_is_directory=True)
    return str(tmp_path / "mounts")


def _shared_root(tmp_path: Path) -> str:
    (tmp_path / "mounts").mkdir()
    (tmp_path / "mounts").chmod(0o777)
    return str(tmp_path / "mounts")


def _root_of_another_user(tmp_path: Path) -> str:
    (tmp_path / "mounts").mkdir(mode=0o700)
    return str(tmp_path / "mounts")


def _symlinked_mountpoint(tmp_path: Path) -> str:
    (tmp_path / "mounts").mkdir(mode=0o700)
    (tmp_path / "elsewhere").mkdir()
    (tmp_path / "mounts" / "music").symlink_to(tmp_path / "elsewhere", target_is_directory=True)
    return str(tmp_path / "mounts" / "music")


@pytest.mark.parametrize(
    "prepare",
    [_symlinked_root, _shared_root, _root_of_another_user, _symlinked_mountpoint],
)
async def test_folder_another_user_could_have_prepared_is_refused(
    mounter: LocalMounter,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    prepare: Callable[[Path], str],
) -> None:
    """Nothing is mounted on a folder that is a link, open to others or not our own."""
    check_output = AsyncMock(return_value=(0, b""))
    monkeypatch.setattr(local_mount, "check_output", check_output)
    unsafe = prepare(tmp_path)
    if prepare is _root_of_another_user:
        other_user = os.geteuid() + 1
        monkeypatch.setattr(f"{local_mount.__name__}.os.geteuid", lambda: other_user)

    with pytest.raises(SetupFailedError) as exc_info:
        await mounter.add(_spec(mounter), "secret")

    assert exc_info.value.translation_key == "mount_folder_unsafe"
    assert exc_info.value.translation_owner == TRANSLATION_OWNER
    assert exc_info.value.translation_args == [unsafe]
    check_output.assert_not_called()
    assert not (tmp_path / "elsewhere" / "music").exists()


async def test_existing_root_and_mountpoint_are_used(
    mounter: LocalMounter, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A root of this process with a mountpoint in it is mounted on as it is."""
    check_output = AsyncMock(return_value=(0, b""))
    monkeypatch.setattr(local_mount, "check_output", check_output)
    (tmp_path / "mounts" / "music").mkdir(parents=True)
    (tmp_path / "mounts").chmod(0o700)

    await mounter.add(_spec(mounter), "secret")

    check_output.assert_awaited_once()


async def test_folder_that_is_not_ours_is_not_unmounted(
    mounter: LocalMounter, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A reload or removal never unmounts what a link in place of the mount root points to."""
    unmount = AsyncMock()
    check_output = AsyncMock(return_value=(0, b""))
    monkeypatch.setattr(local_mount, "unmount", unmount)
    monkeypatch.setattr(local_mount, "check_output", check_output)
    root = _symlinked_root(tmp_path)
    (tmp_path / "elsewhere" / "music").mkdir()

    for command in (mounter.remove(_spec(mounter)), mounter.reload(_spec(mounter), "secret")):
        with pytest.raises(SetupFailedError) as exc_info:
            await command
        assert exc_info.value.translation_key == "mount_folder_unsafe"
        assert exc_info.value.translation_args == [root]

    unmount.assert_not_called()
    check_output.assert_not_called()
    assert (tmp_path / "elsewhere" / "music").is_dir()


async def test_readable_root_of_our_own_is_made_private(
    mounter: LocalMounter, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A root of this process that others may look into is closed before it is used."""
    check_output = AsyncMock(return_value=(0, b""))
    monkeypatch.setattr(local_mount, "check_output", check_output)
    (tmp_path / "mounts").mkdir()
    (tmp_path / "mounts").chmod(0o755)

    await mounter.add(_spec(mounter), "secret")

    assert stat.S_IMODE((tmp_path / "mounts").stat().st_mode) == 0o700
    check_output.assert_awaited_once()


async def test_root_that_can_not_be_made_private_is_refused(
    mounter: LocalMounter, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A root whose mode can not be changed is not used."""
    check_output = AsyncMock(return_value=(0, b""))
    monkeypatch.setattr(local_mount, "check_output", check_output)
    (tmp_path / "mounts").mkdir()
    (tmp_path / "mounts").chmod(0o755)

    def _refuse(*_args: object) -> None:
        raise PermissionError("read-only file system")

    monkeypatch.setattr(Path, "chmod", _refuse)
    with pytest.raises(SetupFailedError) as exc_info:
        await mounter.add(_spec(mounter), "secret")

    assert exc_info.value.translation_key == "mount_folder_unsafe"
    assert exc_info.value.translation_args == [str(tmp_path / "mounts")]
    check_output.assert_not_called()


async def test_remove_on_an_unsafe_root_keeps_the_share(
    storage: StorageController, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A share that may still be mounted where the root now points to is not forgotten."""
    root = _symlinked_root(tmp_path)
    monkeypatch.setattr(local_mount, "MOUNT_ROOT", root)
    unmount = AsyncMock()
    monkeypatch.setattr(local_mount, "unmount", unmount)
    mounter = LocalMounter({ShareType.CIFS: ALL_CIFS}, logging.getLogger(__name__))
    storage._mounters = {MountBackend.LOCAL_MOUNT: mounter}
    storage.mass.config.set(f"{CONF_STORAGE_SHARES}/music", _spec(mounter).to_dict())

    with pytest.raises(SetupFailedError) as exc_info:
        await storage.remove_network_share("music")

    assert exc_info.value.translation_key == "mount_folder_unsafe"
    assert "music" in storage.mass.config.get(CONF_STORAGE_SHARES)
    unmount.assert_not_called()
