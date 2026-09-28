"""Tests for the storage locations, their visibility and their availability."""

from __future__ import annotations

import asyncio
from pathlib import Path
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
from music_assistant.controllers.storage.models import StorageInfo
from music_assistant.controllers.webserver.helpers.auth_middleware import set_current_user
from tests.controllers.storage.conftest import (
    FILE,
    FakeProbes,
    MountTable,
    make_location,
    mount_line,
)

# every field of the wire contract, in the order of the contract
LOCATION_FIELDS = [
    "path",
    "name",
    "usage",
    "kind",
    "available",
    "read_only",
    "managed",
    "backend",
    "fstype",
    "mountpoint",
    "share_name",
    "share_type",
    "server",
    "share",
    "username",
    "version",
    "free_space_gb",
    "total_space_gb",
    "used_space_gb",
    "error",
]


def _locations() -> list[StorageLocation]:
    """Return discovered media locations of every kind, a managed one and the server's own."""
    return [
        *(make_location(f"/{kind.value}", kind=kind, managed=False) for kind in StorageKind),
        make_location("/srv/music", kind=StorageKind.MANUAL),
        make_location("/data", usage=StorageUsage.DATA),
        make_location("/data/.cache", usage=StorageUsage.CACHE),
    ]


@pytest.mark.parametrize("role", [UserRole.USER, UserRole.SERVICE])
async def test_info_for_a_member_in_a_container(storage: StorageController, role: str) -> None:
    """Inside a container a member sees every media location, never the server's own."""
    storage._in_container = True
    storage._locations = _locations()
    set_current_user(User(user_id="member", username="member", role=role))

    info = await storage.get_info()

    assert [loc.path for loc in info.locations] == [
        loc.path for loc in storage.get_locations() if loc.usage == StorageUsage.MEDIA
    ]


@pytest.mark.parametrize("role", [UserRole.USER, UserRole.SERVICE])
async def test_info_for_a_member_on_a_host(storage: StorageController, role: str) -> None:
    """On a host a member only sees the locations Music Assistant set up, whatever their kind."""
    storage._locations = [
        *_locations(),
        make_location("/media/nas", kind=StorageKind.NETWORK_SHARE, managed=True),
    ]
    set_current_user(User(user_id="member", username="member", role=role))

    info = await storage.get_info()

    assert [loc.path for loc in info.locations] == ["/srv/music", "/media/nas"]


async def test_info_for_an_admin(storage: StorageController) -> None:
    """An admin sees every location, including local disks, folders and the server's own."""
    storage._locations = _locations()
    set_current_user(User(user_id="admin", username="admin", role=UserRole.ADMIN))

    info = await storage.get_info()

    assert info.locations == storage.get_locations()
    assert not info.can_mount_shares
    assert info.mount_backend is None
    assert info.supported_share_types == []
    assert info.supported_share_versions == {}


async def test_info_for_an_internal_caller(storage: StorageController) -> None:
    """A server-side caller (no user) is trusted with every location."""
    storage._locations = _locations()

    info = await storage.get_info()

    assert info.locations == storage.get_locations()


@pytest.mark.parametrize(("role", "measures"), [(UserRole.ADMIN, True), (UserRole.USER, False)])
async def test_info_measures_directories_for_admins_only(
    storage: StorageController, role: str, measures: bool
) -> None:
    """Only a caller that sees the data and cache rows makes the server measure them."""
    set_current_user(User(user_id="someone", username="someone", role=role))

    with patch.object(storage, "_request_dir_sizes") as request_dir_sizes:
        await storage.get_info()

    assert request_dir_sizes.called is measures


def test_info_serializes_to_the_contract() -> None:
    """The info is sent with every field of the contract, null where there is no value."""
    location = make_location("/media/nas", kind=StorageKind.NETWORK_SHARE)
    info = StorageInfo(
        locations=[location],
        can_mount_shares=False,
        mount_backend=None,
        supported_share_types=[],
        supported_share_versions={},
        can_add_local_folder=False,
    )

    data = info.to_dict()

    assert list(data) == [
        "locations",
        "can_mount_shares",
        "mount_backend",
        "supported_share_types",
        "supported_share_versions",
        "can_add_local_folder",
    ]
    assert data["supported_share_versions"] == {}
    assert list(data["locations"][0]) == LOCATION_FIELDS
    assert data["locations"][0]["kind"] == "network_share"
    assert data["locations"][0]["usage"] == "media"
    assert data["locations"][0]["backend"] is None


def test_location_for_path_is_the_most_specific(storage: StorageController) -> None:
    """A path belongs to the deepest location that contains it, never to a look-alike."""
    storage._locations = [make_location("/media"), make_location("/media/nas")]

    assert storage.get_location_for_path("/media/nas/music") is storage._locations[1]
    assert storage.get_location_for_path("/media/nas/") is storage._locations[1]
    assert storage.get_location_for_path("/media/nasty") is storage._locations[0]
    assert storage.get_location_for_path("/mediafiles") is None
    assert storage.get_location_for_path("/media/nas\0") is None


async def test_mount_backed_folder_needs_its_mount(
    storage: StorageController, tmp_path: Path, mount_table: MountTable
) -> None:
    """The empty directory an unmounted share leaves behind is not available."""
    share = tmp_path / "nas"
    (share / "music").mkdir(parents=True)
    storage._locations = [make_location(share, mountpoint=str(share))]

    mount_table.set(mount_line(share))
    assert await storage.is_available(str(share))
    assert await storage.is_available(str(share / "music"))
    assert not await storage.is_available(str(share / "missing"))
    mount_table.set()
    assert not await storage.is_available(str(share / "music"))
    # back behind its automount trigger
    mount_table.set(mount_line(share, "autofs"))
    assert not await storage.is_available(str(share / "music"))


async def test_same_filesystem_bind_mount_is_available(
    storage: StorageController, tmp_path: Path, mount_table: MountTable
) -> None:
    """A bind mount of a folder on the filesystem it is mounted on counts as mounted."""
    volume = tmp_path / "music"
    volume.mkdir()
    storage._locations = [make_location(volume, mountpoint=str(volume))]
    # for os.path.ismount this is a plain folder: same device as its parent
    mount_table.set(mount_line(volume))

    assert await storage.is_available(str(volume))


async def test_unavailable_location_is_not_touched(
    storage: StorageController, tmp_path: Path
) -> None:
    """A folder in a location whose probe did not answer is unavailable without a look."""
    storage._locations = [make_location(tmp_path, mountpoint=str(tmp_path), available=False)]

    with patch.object(controller_module, "_is_available") as is_available:
        assert not await storage.is_available(str(tmp_path / "music"))

    is_available.assert_not_called()


async def test_folder_without_a_mount_only_needs_to_exist(
    storage: StorageController, tmp_path: Path
) -> None:
    """A folder in a registered folder or outside every location only has to exist."""
    (tmp_path / "registered" / "music").mkdir(parents=True)
    storage._locations = [make_location(tmp_path / "registered", kind=StorageKind.MANUAL)]

    with patch.object(controller_module, "parse_mountpoints") as parse_mountpoints:
        assert await storage.is_available(str(tmp_path / "registered" / "music"))
        assert await storage.is_available(str(tmp_path))
        assert not await storage.is_available(str(tmp_path / "gone"))
    parse_mountpoints.assert_not_called()


async def test_refresh_builds_the_locations(
    storage: StorageController, mount_table: MountTable, probes: FakeProbes
) -> None:
    """Discovered mounts, registered folders and the server directories make the locations."""
    data_path, cache_path = storage.mass.storage_path, storage.mass.cache_path
    probes.results.update({"/music.conf": FILE, "/mnt/asleep": None})
    storage._in_container = True
    storage._dir_sizes = {StorageUsage.DATA: 1.5}
    storage.mass.config.set(CONF_STORAGE_FOLDERS, ["/srv/podcasts", "/media/music"])
    mount_table.set(
        mount_line("/media/music", "ext4"),
        mount_line("/music.conf", "ext4"),
        mount_line("/mnt/asleep", "nfs4"),
        mount_line(data_path, "ext4"),
    )

    await storage.refresh()

    locations = storage.get_locations()
    by_path = {loc.path: loc for loc in locations}
    # a file bound into the container is no location
    assert "/music.conf" not in by_path
    # a registered folder replaces the volume discovered on the same path, and keeps its mount
    assert by_path["/media/music"].kind == StorageKind.MANUAL
    assert by_path["/media/music"].managed
    assert (by_path["/media/music"].mountpoint, by_path["/media/music"].fstype) == (
        "/media/music",
        "ext4",
    )
    assert by_path["/srv/podcasts"].managed
    assert by_path["/srv/podcasts"].mountpoint is None
    # a share that can not be reached stays listed, unavailable and without sizes
    assert by_path["/mnt/asleep"].kind == StorageKind.NETWORK_SHARE
    assert not by_path["/mnt/asleep"].available
    assert by_path["/mnt/asleep"].free_space_gb is None
    assert by_path["/srv/podcasts"].free_space_gb == 75.0
    assert by_path["/srv/podcasts"].total_space_gb == 100.0
    data, cache = locations[-2:]
    assert (data.path, data.usage, data.used_space_gb) == (data_path, StorageUsage.DATA, 1.5)
    assert (cache.path, cache.usage, cache.used_space_gb) == (cache_path, StorageUsage.CACHE, None)
    assert data.kind == cache.kind == StorageKind.CONTAINER_VOLUME
    assert [loc.path for loc in locations[:-2]] == ["/media/music", "/mnt/asleep", "/srv/podcasts"]


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


@pytest.mark.usefixtures("mount_table")
async def test_close_stops_the_refreshes(storage: StorageController, probes: FakeProbes) -> None:
    """Closing cancels the refresh in flight and the timer of the next one."""
    await storage._periodic_refresh()
    assert REFRESH_TASK_ID in storage.mass._tracked_timers
    probes.block(storage.mass.storage_path)
    task = storage.mass.create_task(storage.refresh(), task_id=REFRESH_TASK_ID)

    await storage.close()

    assert REFRESH_TASK_ID not in storage.mass._tracked_timers
    with pytest.raises(asyncio.CancelledError):
        await task


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
        controller_module,
        "get_folder_size",
        AsyncMock(side_effect=lambda path, _exclude=(): sizes[path]),
    ):
        await storage._update_dir_sizes()

    assert [loc.used_space_gb for loc in storage.get_locations()] == [None, 12.35, 3.1]


@pytest.mark.parametrize(("cache_inside_data", "excluded"), [(True, True), (False, False)])
async def test_cache_is_not_counted_twice(
    storage: StorageController, tmp_path: Path, cache_inside_data: bool, excluded: bool
) -> None:
    """The data size leaves out a cache directory that lies inside the data directory."""
    storage.mass.storage_path = str(tmp_path / "data")
    storage.mass.cache_path = str(tmp_path / ("data/.cache" if cache_inside_data else "data-cache"))
    get_folder_size = AsyncMock(return_value=1.0)

    with patch.object(controller_module, "get_folder_size", get_folder_size):
        await storage._update_dir_sizes()

    assert get_folder_size.await_args_list[0].args == (
        storage.mass.storage_path,
        (storage.mass.cache_path,) if excluded else (),
    )


@pytest.mark.usefixtures("mount_table")
async def test_sizes_measured_during_a_refresh_are_kept(
    storage: StorageController, probes: FakeProbes
) -> None:
    """A measurement that finishes while a refresh waits for its probes shows up after it."""
    probes.block(storage.mass.cache_path)
    refresh = storage.mass.create_task(storage.refresh())

    with patch.object(controller_module, "get_folder_size", AsyncMock(return_value=2.0)):
        await storage._update_dir_sizes()
    probes.release()
    await refresh

    assert [loc.used_space_gb for loc in storage.get_locations()] == [2.0, 2.0]
