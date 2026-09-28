"""Tests for the storage locations, their visibility and their availability."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import NamedTuple
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from music_assistant_models.auth import User, UserRole

from music_assistant.constants import CONF_STORAGE_FOLDERS
from music_assistant.controllers.storage import (
    StorageController,
    StorageKind,
    StorageLocation,
    StorageUsage,
)
from music_assistant.controllers.storage import controller as controller_module
from music_assistant.controllers.storage.constants import (
    DIR_SIZE_MAX_AGE,
    DIR_SIZES_TASK_ID,
    REFRESH_INTERVAL,
    REFRESH_TASK_ID,
)
from music_assistant.controllers.webserver.helpers.auth_middleware import set_current_user
from tests.controllers.storage.conftest import make_location


class _DiskUsage(NamedTuple):
    total: int
    used: int
    free: int


GB = 1 << 30
MEMBER_KINDS = {
    StorageKind.BUILTIN_MEDIA,
    StorageKind.CONTAINER_VOLUME,
    StorageKind.NETWORK_SHARE,
    StorageKind.REMOVABLE,
}


def _mount_line(mountpoint: Path | str, fstype: str = "cifs") -> str:
    """Return a mountinfo line for a mount, escaped the way the kernel writes it."""
    escaped = str(mountpoint).replace(" ", "\\040")
    return f"100 1 0:50 / {escaped} rw,relatime - {fstype} //nas/music rw"


def _all_kinds() -> list[StorageLocation]:
    """Return a media location of every kind plus the data and cache rows."""
    return [
        *(make_location(f"/{kind.value}", kind=kind) for kind in StorageKind),
        make_location("/data", usage=StorageUsage.DATA),
        make_location("/data/.cache", usage=StorageUsage.CACHE),
    ]


@pytest.mark.parametrize("role", [UserRole.USER, UserRole.SERVICE])
async def test_info_for_a_member(storage: StorageController, role: str) -> None:
    """A caller that does not manage every source sees the shared media kinds only."""
    storage._locations = _all_kinds()
    set_current_user(User(user_id="member", username="member", role=role))

    info = await storage.get_info()

    assert {loc.kind for loc in info.locations} == MEMBER_KINDS
    assert {loc.usage for loc in info.locations} == {StorageUsage.MEDIA}


async def test_info_for_an_admin(storage: StorageController) -> None:
    """An admin sees every location, including local disks, folders and the server's own."""
    storage._locations = _all_kinds()
    set_current_user(User(user_id="admin", username="admin", role=UserRole.ADMIN))

    info = await storage.get_info()

    assert info.locations == storage.locations
    assert not info.can_mount_shares
    assert info.mount_backend is None
    assert info.supported_share_types == []


async def test_info_for_an_internal_caller(storage: StorageController) -> None:
    """A server-side caller (no user) is trusted with every location."""
    storage._locations = _all_kinds()

    info = await storage.get_info()

    assert info.locations == storage.locations


def test_location_for_path_is_the_most_specific(storage: StorageController) -> None:
    """A path belongs to the deepest location that contains it, never to a look-alike."""
    storage._locations = [make_location("/media"), make_location("/media/nas")]

    assert storage.get_location_for_path("/media/nas/music") is storage._locations[1]
    assert storage.get_location_for_path("/media/nas") is storage._locations[1]
    assert storage.get_location_for_path("/media/nasty") is storage._locations[0]
    assert storage.get_location_for_path("/mediafiles") is None


async def test_mount_backed_folder_needs_its_mount(
    storage: StorageController, tmp_path: Path
) -> None:
    """The empty directory an unmounted share leaves behind is not available."""
    share = tmp_path / "nas"
    (share / "music").mkdir(parents=True)
    storage._locations = [make_location(share, mountpoint=str(share))]

    with patch.object(controller_module, "read_mountinfo", return_value=_mount_line(share)):
        assert await storage.is_available(str(share))
        assert await storage.is_available(str(share / "music"))
        assert not await storage.is_available(str(share / "missing"))
    with patch.object(controller_module, "read_mountinfo", return_value=""):
        assert not await storage.is_available(str(share / "music"))


async def test_same_filesystem_bind_mount_is_available(
    storage: StorageController, tmp_path: Path
) -> None:
    """A bind mount of a folder on the filesystem it is mounted on counts as mounted."""
    volume = tmp_path / "music"
    volume.mkdir()
    storage._locations = [make_location(volume, mountpoint=str(volume))]

    # for os.path.ismount this is a plain folder: same device as its parent
    with patch.object(controller_module, "read_mountinfo", return_value=_mount_line(volume)):
        assert await storage.is_available(str(volume))


async def test_folder_without_a_mount_only_needs_to_exist(
    storage: StorageController, tmp_path: Path
) -> None:
    """A folder in a registered folder or outside every location only has to exist."""
    (tmp_path / "registered" / "music").mkdir(parents=True)
    storage._locations = [make_location(tmp_path / "registered", kind=StorageKind.MANUAL)]

    with patch.object(controller_module, "read_mountinfo", return_value="") as read_table:
        assert await storage.is_available(str(tmp_path / "registered" / "music"))
        assert await storage.is_available(str(tmp_path))
        assert not await storage.is_available(str(tmp_path / "gone"))
    read_table.assert_not_called()


async def test_refresh_builds_the_locations(
    storage: StorageController, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Discovered mounts, registered folders and the server directories make the locations."""
    dirs = {
        "/media/music": True,
        "/music.conf": False,
        "/mnt/asleep": None,
        "/srv/podcasts": True,
    }
    monkeypatch.setattr(controller_module, "_is_dir", lambda path: dirs.get(path, True))
    monkeypatch.setattr(shutil, "disk_usage", lambda _path: _DiskUsage(100 * GB, 25 * GB, 75 * GB))
    storage._in_container = True
    storage._dir_sizes = {StorageUsage.DATA: 1.5}
    storage.mass.config.set(CONF_STORAGE_FOLDERS, ["/srv/podcasts", "/media/music"])
    table = "\n".join(
        (
            _mount_line("/media/music", "ext4"),
            _mount_line("/music.conf", "ext4"),
            _mount_line("/mnt/asleep", "nfs4"),
            _mount_line("/srv/podcasts", "ext4"),
            _mount_line(storage.mass.storage_path, "ext4"),
        )
    )

    with patch.object(controller_module, "read_mountinfo", return_value=table):
        await storage.refresh()

    by_path = {loc.path: loc for loc in storage.locations}
    # a file bound into the container is no location
    assert "/music.conf" not in by_path
    # a registered folder replaces the volume discovered on the same path
    assert by_path["/media/music"].kind == StorageKind.MANUAL
    assert by_path["/media/music"].managed
    assert by_path["/srv/podcasts"].managed
    # a share that can not be reached stays listed, unavailable and without sizes
    assert by_path["/mnt/asleep"].kind == StorageKind.NETWORK_SHARE
    assert not by_path["/mnt/asleep"].available
    assert by_path["/mnt/asleep"].free_space_gb is None
    assert by_path["/srv/podcasts"].free_space_gb == 75.0
    assert by_path["/srv/podcasts"].total_space_gb == 100.0
    data, cache = storage.locations[-2:]
    assert (data.path, data.usage, data.used_space_gb) == (
        storage.mass.storage_path,
        StorageUsage.DATA,
        1.5,
    )
    assert (cache.path, cache.usage, cache.used_space_gb) == (
        storage.mass.cache_path,
        StorageUsage.CACHE,
        None,
    )
    assert data.kind == cache.kind == StorageKind.CONTAINER_VOLUME
    assert [loc.path for loc in storage.locations[:-2]] == [
        "/media/music",
        "/mnt/asleep",
        "/srv/podcasts",
    ]


async def test_periodic_refresh_survives_a_failure(storage: StorageController) -> None:
    """A failing refresh is logged and the next one is still scheduled."""
    storage.refresh = AsyncMock(side_effect=RuntimeError("boom"))  # type: ignore[method-assign]
    storage.logger = MagicMock()

    with patch.object(storage.mass, "call_later") as call_later:
        await storage._periodic_refresh()

    storage.logger.exception.assert_called_once()
    call_later.assert_called_once_with(
        REFRESH_INTERVAL, storage._periodic_refresh, task_id=REFRESH_TASK_ID
    )


async def test_directory_sizes_are_measured_at_most_every_ten_minutes(
    storage: StorageController,
) -> None:
    """Asking for the sizes again within the window measures nothing new."""
    with (
        patch.object(storage.mass, "create_task") as create_task,
        patch.object(controller_module, "time") as clock,
    ):
        clock.monotonic.return_value = 1000.0
        storage._request_dir_sizes()
        storage._request_dir_sizes()
        clock.monotonic.return_value = 1000.0 + DIR_SIZE_MAX_AGE - 1
        storage._request_dir_sizes()
        assert create_task.call_count == 1
        clock.monotonic.return_value = 1000.0 + DIR_SIZE_MAX_AGE
        storage._request_dir_sizes()

    assert create_task.call_count == 2
    assert create_task.call_args.kwargs == {"task_id": DIR_SIZES_TASK_ID}
    for call in create_task.call_args_list:
        call.args[0].close()


async def test_directory_sizes_land_on_the_server_rows(storage: StorageController) -> None:
    """Measured sizes show up on the data and cache rows, never on a media location."""
    storage._locations = [
        make_location("/media"),
        make_location(storage.mass.storage_path, usage=StorageUsage.DATA),
        make_location(storage.mass.cache_path, usage=StorageUsage.CACHE),
    ]
    sizes = {storage.mass.storage_path: 12.3456, storage.mass.cache_path: 3.1}

    with patch.object(
        controller_module, "get_folder_size", AsyncMock(side_effect=lambda path: sizes[path])
    ):
        await storage._update_dir_sizes()

    assert [loc.used_space_gb for loc in storage.locations] == [None, 12.35, 3.1]
