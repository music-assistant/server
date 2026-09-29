"""Tests for registering folders on the server as media locations."""

from __future__ import annotations

import os
import time
from collections.abc import Iterator
from functools import partial
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from music_assistant_models.auth import User, UserRole
from music_assistant_models.errors import ActionUnavailable, InvalidDataError
from music_assistant_models.translations import TRANSLATION_RESOLVER

from music_assistant.constants import (
    CONF_CORE,
    CONF_STORAGE_FOLDER_MOUNTS,
    CONF_STORAGE_FOLDERS,
    CONF_STORAGE_SHARES,
)
from music_assistant.controllers.storage import (
    StorageController,
    StorageKind,
    StorageLocation,
    StorageUsage,
)
from music_assistant.controllers.storage import controller as controller_module
from music_assistant.controllers.storage.backends import mountinfo
from music_assistant.controllers.storage.constants import PROBE_MAX_AGE
from music_assistant.controllers.storage.models import MountBackend, NetworkShareSpec, ShareType
from music_assistant.controllers.translations import TranslationController
from music_assistant.controllers.webserver.helpers.auth_middleware import set_current_user
from tests.controllers.storage.conftest import FakeProbes, MountTable, mount_line, store_source

MEMBER = User(user_id="member", username="member", role=UserRole.USER)
# the root filesystem: a Linux mount table is never empty
ROOT_MOUNT = mount_line("/", "ext4")


@pytest.fixture(autouse=True)
def empty_mount_table() -> Iterator[None]:
    """Keep the mount table of the machine running the tests out of the locations."""
    with patch.object(controller_module, "read_mountinfo", return_value=""):
        yield


@pytest.fixture(autouse=True)
def discoverable_tmp_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Let discovery find a mount in the temporary folder of a test.

    :param monkeypatch: Pytest monkeypatch fixture.
    """
    # the temporary folder of the tests may lie below a system path, which discovery leaves out
    monkeypatch.setattr(mountinfo, "SYSTEM_PATHS", ())


def _location(locations: list[StorageLocation], path: Path) -> StorageLocation:
    """
    Return the location on a path.

    :param locations: The locations to pick from.
    :param path: The path of the location.
    """
    return next(loc for loc in locations if loc.path == str(path))


def _outdate_answer(storage: StorageController, path: Path) -> None:
    """
    Make the last answer for a path too old to go by, so the next caller probes it again.

    :param storage: The storage controller.
    :param path: The probed path.
    """
    storage._probes[str(path)].answered_at = time.monotonic() - PROBE_MAX_AGE - 1


async def test_add_folder(storage: StorageController, tmp_path: Path) -> None:
    """A folder on this server becomes a managed location, stored outside the core config."""
    location = await storage.add_local_folder(f"{tmp_path}/")

    assert location.path == str(tmp_path)
    assert (location.kind, location.managed, location.available) == (
        StorageKind.MANUAL,
        True,
        True,
    )
    assert location in storage.get_locations()
    assert storage.mass.config.get(CONF_STORAGE_FOLDERS) == [str(tmp_path)]
    assert storage.mass.config.get(f"{CONF_CORE}/storage") is None


async def test_add_folder_twice(storage: StorageController, tmp_path: Path) -> None:
    """A folder that is registered already can not be added again."""
    await storage.add_local_folder(str(tmp_path))

    with pytest.raises(InvalidDataError) as exc_info:
        await storage.add_local_folder(f"{tmp_path}/")

    assert exc_info.value.translation_key == "folder_already_location"
    assert storage.mass.config.get(CONF_STORAGE_FOLDERS) == [str(tmp_path)]


@pytest.mark.usefixtures("probes")
async def test_add_a_discovered_location(
    storage: StorageController, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A mount that is a location already becomes a registered folder that keeps its mount."""
    table = mount_line("/mnt/music", "ext4")
    monkeypatch.setattr(controller_module, "read_mountinfo", lambda: table)
    await storage.refresh()

    location = await storage.add_local_folder("/mnt/music/")
    # a folder inside the mount is fine
    await storage.add_local_folder("/mnt/music/Albums")

    assert (location.path, location.kind, location.managed) == (
        "/mnt/music",
        StorageKind.MANUAL,
        True,
    )
    assert (location.mountpoint, location.fstype) == ("/mnt/music", "ext4")
    assert [loc.path for loc in storage.get_locations()].count("/mnt/music") == 1
    assert storage.mass.config.get(CONF_STORAGE_FOLDERS) == ["/mnt/music", "/mnt/music/Albums"]


async def test_add_a_symlink_to_a_discovered_location(
    storage: StorageController, tmp_path: Path, mount_table: MountTable
) -> None:
    """A symlink to a mount the server found registers the mount itself."""
    nas = tmp_path / "nas"
    nas.mkdir()
    (tmp_path / "link").symlink_to(nas, target_is_directory=True)
    mount_table.set(mount_line(nas, "cifs"))
    await storage.refresh()

    location = await storage.add_local_folder(str(tmp_path / "link"))

    assert (location.path, location.kind, location.mountpoint) == (
        str(nas),
        StorageKind.MANUAL,
        str(nas),
    )
    assert storage.mass.config.get(CONF_STORAGE_FOLDERS) == [str(nas)]


@pytest.mark.usefixtures("probes")
async def test_registered_mount_is_available_to_members_until_removed(
    storage: StorageController, mount_table: MountTable
) -> None:
    """
    A mounted share on a host reaches members once registered.

    Removing the folder again lists the mount as found, for callers that manage every source.
    """
    mount_table.set(mount_line("/mnt/nas", "cifs"))
    await storage.refresh()
    assert storage.get_locations(manages_all_sources=False) == []

    await storage.add_local_folder("/mnt/nas")
    member_paths = [loc.path for loc in storage.get_locations(manages_all_sources=False)]
    await storage.remove_local_folder("/mnt/nas")

    assert member_paths == ["/mnt/nas"]
    assert storage.get_locations(manages_all_sources=False) == []
    location = _location(storage.get_locations(), Path("/mnt/nas"))
    assert (location.kind, location.managed, location.mountpoint) == (
        StorageKind.NETWORK_SHARE,
        False,
        "/mnt/nas",
    )
    assert storage.mass.config.get(CONF_STORAGE_FOLDERS) == []


@pytest.mark.usefixtures("probes")
async def test_registered_mount_needs_its_mount(
    storage: StorageController, tmp_path: Path, mount_table: MountTable
) -> None:
    """
    A registered mountpoint is only available while the mount is there.

    It comes back with the next refresh, the one the server runs on its timer.
    """
    nas = tmp_path / "nas"
    (nas / "Albums").mkdir(parents=True)
    mount_table.set(ROOT_MOUNT, mount_line(nas, "cifs"))
    await storage.add_local_folder(str(nas))
    assert await storage.is_available(str(nas / "Albums"))

    mount_table.set(ROOT_MOUNT)
    await storage.refresh()
    assert not _location(storage.get_locations(), nas).available
    assert not await storage.is_available(str(nas / "Albums"))

    mount_table.set(ROOT_MOUNT, mount_line(nas, "cifs"))
    await storage.refresh()
    location = _location(storage.get_locations(), nas)
    assert (location.available, location.error, location.fstype) == (True, None, "cifs")
    assert await storage.is_available(str(nas / "Albums"))


async def test_unmounted_registered_mount_is_listed_unavailable(
    storage: StorageController, tmp_path: Path, mount_table: MountTable, probes: FakeProbes
) -> None:
    """
    A registered mountpoint whose mount is gone says so, and is not probed.

    It keeps its path as mountpoint, and shows no space: the folder left behind lies on
    another disk. A caller that does not manage every source gets the same reason.
    """
    nas = tmp_path / "nas"
    nas.mkdir()
    mount_table.set(ROOT_MOUNT, mount_line(nas, "cifs"))
    await storage.add_local_folder(str(nas))
    mount_table.set(ROOT_MOUNT)
    probes.calls.clear()

    location = _location((await storage.get_info()).locations, nas)
    set_current_user(MEMBER)
    member_view = _location((await storage.get_info()).locations, nas)

    assert (location.available, location.error_key, location.mountpoint, location.fstype) == (
        False,
        "folder_not_mounted",
        str(nas),
        None,
    )
    assert location.error is not None
    assert (location.free_space_gb, location.total_space_gb) == (None, None)
    assert (location.kind, location.managed) == (StorageKind.MANUAL, True)
    assert member_view == location
    assert str(nas) not in probes.calls


async def test_registered_mount_down_after_a_restart(
    storage: StorageController, tmp_path: Path, mount_table: MountTable, probes: FakeProbes
) -> None:
    """
    A server that starts while the mount of a registered folder is gone never uses the folder.

    A music source inside it finds the folder as its location, which is not available, so it
    is told the storage is not available rather than that its folder is not allowed.
    """
    nas = tmp_path / "nas"
    (nas / "Albums").mkdir(parents=True)
    mount_table.set(ROOT_MOUNT, mount_line(nas, "cifs"))
    await storage.add_local_folder(str(nas))
    mount_table.set(ROOT_MOUNT)
    probes.calls.clear()
    restarted = StorageController(storage.mass)
    try:
        await restarted.refresh()
        listed = _location(restarted.get_locations(), nas)
        available = [await restarted.is_available(str(path)) for path in (nas, nas / "Albums")]
        location = restarted.get_location_for_path(str(nas / "Albums"))
        may_hold = restarted.can_hold_music_source(str(nas / "Albums"), manages_all_sources=False)
        with pytest.raises(ActionUnavailable) as exc_info:
            await restarted.list_folders(str(nas), manages_all_sources=False)
    finally:
        await restarted.close()

    assert (listed.available, listed.error_key) == (False, "folder_not_mounted")
    assert available == [False, False]
    assert location == listed
    assert may_hold
    assert exc_info.value.translation_key == "folder_unreadable"
    assert probes.calls == []


async def test_dormant_automount_counts_as_mounted(
    storage: StorageController, tmp_path: Path, mount_table: MountTable, probes: FakeProbes
) -> None:
    """A registered mountpoint behind its automount trigger is probed, which wakes it."""
    nas = tmp_path / "nas"
    nas.mkdir()
    mount_table.set(ROOT_MOUNT, mount_line(nas, "cifs"))
    await storage.add_local_folder(str(nas))
    mount_table.set(ROOT_MOUNT, mount_line(nas, "autofs"))
    probes.results[str(nas)] = None
    _outdate_answer(storage, nas)

    asleep = _location((await storage.get_info()).locations, nas)
    del probes.results[str(nas)]
    probes.side_effects[str(nas)] = partial(mount_table.mount, str(nas), "cifs")
    _outdate_answer(storage, nas)
    awake = _location((await storage.get_info()).locations, nas)

    assert (asleep.available, asleep.error_key, asleep.mountpoint) == (
        False,
        "storage_not_responding",
        str(nas),
    )
    assert (awake.available, awake.error_key, awake.fstype) == (True, None, "cifs")
    assert await storage.is_available(str(nas))


@pytest.mark.usefixtures("probes")
@pytest.mark.parametrize("inside_a_mount", [False, True])
async def test_only_a_mountpoint_is_recorded(
    storage: StorageController, tmp_path: Path, mount_table: MountTable, inside_a_mount: bool
) -> None:
    """A plain folder, or one inside a mount, is not recorded as a mount and goes by its probe."""
    folder = tmp_path / "nas" / "music"
    folder.mkdir(parents=True)
    if inside_a_mount:
        mount_table.set(ROOT_MOUNT, mount_line(tmp_path / "nas", "cifs"))
    else:
        mount_table.set(ROOT_MOUNT)
    await storage.add_local_folder(str(folder))

    mount_table.set(ROOT_MOUNT)
    await storage.refresh()

    assert storage.mass.config.get(CONF_STORAGE_FOLDER_MOUNTS) == []
    location = _location(storage.get_locations(), folder)
    assert (location.available, location.error_key, location.mountpoint) == (True, None, None)


@pytest.mark.usefixtures("probes")
@pytest.mark.parametrize(
    ("folder", "fstype", "system_path"),
    [
        # like /var/mnt/nas, where /mnt links to /var/mnt as on Fedora Atomic
        pytest.param("var/mnt/nas", "cifs", "var", id="below_a_system_path"),
        pytest.param("nas", "ceph", None, id="other_filesystem"),
    ],
)
async def test_mount_left_out_by_discovery(
    storage: StorageController,
    tmp_path: Path,
    mount_table: MountTable,
    monkeypatch: pytest.MonkeyPatch,
    folder: str,
    fstype: str,
    system_path: str | None,
) -> None:
    """
    A mount discovery leaves out is no location by itself, but is a mount once registered.

    It is not available while nothing is mounted on it, also after a restart, and comes back
    with its mount, probed like any other location.
    """
    nas = tmp_path / folder
    nas.mkdir(parents=True)
    if system_path is not None:
        monkeypatch.setattr(mountinfo, "SYSTEM_PATHS", (str(tmp_path / system_path),))
    mount_table.set(ROOT_MOUNT, mount_line(nas, fstype))
    await storage.refresh()
    assert str(nas) not in [loc.path for loc in storage.get_locations()]

    location = await storage.add_local_folder(str(nas))
    assert storage.mass.config.get(CONF_STORAGE_FOLDER_MOUNTS) == [str(nas)]
    assert (location.available, location.mountpoint, location.fstype) == (True, str(nas), fstype)
    assert location.free_space_gb == 75.0

    mount_table.set(ROOT_MOUNT)
    await storage.refresh()
    assert _location(storage.get_locations(), nas).error_key == "folder_not_mounted"
    restarted = StorageController(storage.mass)
    try:
        await restarted.refresh()
        assert _location(restarted.get_locations(), nas).error_key == "folder_not_mounted"
        assert not await restarted.is_available(str(nas))

        mount_table.set(ROOT_MOUNT, mount_line(nas, fstype))
        await restarted.refresh()
        assert await restarted.is_available(str(nas))
        location = _location(restarted.get_locations(), nas)
    finally:
        await restarted.close()
    assert (location.available, location.error_key, location.fstype) == (True, None, fstype)
    assert location.free_space_gb == 75.0


@pytest.mark.usefixtures("probes")
async def test_folder_on_a_mount_below_a_system_path_is_not_recorded(
    storage: StorageController,
    tmp_path: Path,
    mount_table: MountTable,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A folder inside a mount below a system path is a plain folder, like any other."""
    var = tmp_path / "var"
    (var / "music").mkdir(parents=True)
    monkeypatch.setattr(mountinfo, "SYSTEM_PATHS", (str(var),))
    mount_table.set(ROOT_MOUNT, mount_line(var, "ext4"))

    location = await storage.add_local_folder(str(var / "music"))

    assert storage.mass.config.get(CONF_STORAGE_FOLDER_MOUNTS) == []
    assert (location.available, location.mountpoint) == (True, None)


@pytest.mark.usefixtures("probes")
async def test_removal_drops_the_record(
    storage: StorageController, tmp_path: Path, mount_table: MountTable
) -> None:
    """
    Removing a registered mountpoint drops its record.

    A record left without its folder, as a removal that stopped halfway leaves it, does not
    come back when the path is registered again as a plain folder.
    """
    nas = tmp_path / "nas"
    nas.mkdir()
    mount_table.set(ROOT_MOUNT, mount_line(nas, "cifs"))
    await storage.add_local_folder(str(nas))
    assert storage.mass.config.get(CONF_STORAGE_FOLDER_MOUNTS) == [str(nas)]

    await storage.remove_local_folder(str(nas))
    assert storage.mass.config.get(CONF_STORAGE_FOLDER_MOUNTS) == []

    storage.mass.config.set(CONF_STORAGE_FOLDER_MOUNTS, [str(nas)])
    mount_table.set(ROOT_MOUNT)
    location = await storage.add_local_folder(str(nas))

    assert storage.mass.config.get(CONF_STORAGE_FOLDER_MOUNTS) == []
    assert (location.available, location.error_key, location.mountpoint) == (True, None, None)


@pytest.mark.usefixtures("probes")
@pytest.mark.parametrize(
    "record",
    [
        None,
        "/mnt/nas",
        {"/mnt/nas": True},
        [1, None, {"path": "/mnt/nas"}, ["/mnt/nas"]],
        ["/mnt/other"],
    ],
)
async def test_malformed_record_is_ignored(
    storage: StorageController, mount_table: MountTable, record: object
) -> None:
    """A record that is missing, no list, or holds no registered folder counts for nothing."""
    storage.mass.config.set(CONF_STORAGE_FOLDERS, ["/mnt/nas"])
    storage.mass.config.set(CONF_STORAGE_FOLDER_MOUNTS, record)
    mount_table.set(ROOT_MOUNT)

    location = _location((await storage.get_info()).locations, Path("/mnt/nas"))
    await storage.remove_local_folder("/mnt/nas")

    assert (location.available, location.error_key, location.mountpoint) == (True, None, None)
    assert storage.mass.config.get(CONF_STORAGE_FOLDER_MOUNTS) == []


@pytest.mark.usefixtures("probes")
async def test_registered_mount_in_use_stays(
    storage: StorageController, mount_table: MountTable
) -> None:
    """A music source on a registered mountpoint keeps it registered."""
    mount_table.set(mount_line("/mnt/nas", "cifs"))
    store_source(storage, "/mnt/nas/music")
    await storage.refresh()

    location = await storage.add_local_folder("/mnt/nas")
    with pytest.raises(ActionUnavailable) as exc_info:
        await storage.remove_local_folder("/mnt/nas")

    assert location.used_by == ["My music"]
    assert exc_info.value.translation_key == "location_in_use"
    assert storage.mass.config.get(CONF_STORAGE_FOLDERS) == ["/mnt/nas"]


@pytest.mark.usefixtures("probes", "mounter")
async def test_add_refuses_a_managed_network_share(storage: StorageController) -> None:
    """The mountpoint of a network share Music Assistant added can not be added as a folder."""
    share = await storage.add_network_share(ShareType.CIFS, "nas.local", "music")

    with pytest.raises(InvalidDataError) as exc_info:
        await storage.add_local_folder(f"{share.path}/")

    assert exc_info.value.translation_key == "folder_already_location"
    assert storage.mass.config.get(CONF_STORAGE_FOLDERS) is None


@pytest.mark.parametrize(
    ("path", "translation_key"),
    [
        ("/", "folder_is_root"),
        ("//", "folder_is_root"),
        ("/..", "folder_is_root"),
        ("{data}", "folder_is_server_folder"),
        ("{data}/", "folder_is_server_folder"),
        ("{cache}", "folder_is_server_folder"),
    ],
)
async def test_add_refuses_the_root_and_the_server_folders(
    storage: StorageController, path: str, translation_key: str
) -> None:
    """Neither the whole server nor its own data and cache folders can be added."""
    with pytest.raises(InvalidDataError) as exc_info:
        await storage.add_local_folder(
            path.format(data=storage.mass.storage_path, cache=storage.mass.cache_path)
        )

    assert exc_info.value.translation_key == translation_key
    assert storage.mass.config.get(CONF_STORAGE_FOLDERS) is None


@pytest.mark.parametrize(
    ("target", "translation_key"),
    [
        ("{data}", "folder_is_server_folder"),
        ("{cache}", "folder_is_server_folder"),
        ("/", "folder_is_root"),
        ("{registered}", "folder_already_location"),
        ("{share}", "folder_already_location"),
    ],
)
async def test_add_refuses_a_symlink_to_a_refused_folder(
    storage: StorageController, tmp_path: Path, target: str, translation_key: str
) -> None:
    """A symlink is refused when the folder it points to would be."""
    share = tmp_path / "share"
    share.mkdir()
    storage.mass.config.set(
        f"{CONF_STORAGE_SHARES}/music",
        NetworkShareSpec(
            name="music",
            share_type=ShareType.CIFS,
            server="nas.local",
            share="music",
            backend=MountBackend.LOCAL_MOUNT,
            path=str(share),
        ).to_dict(),
    )
    registered = tmp_path / "registered"
    registered.mkdir()
    await storage.add_local_folder(str(registered))
    link = tmp_path / "link"
    link.symlink_to(
        target.format(
            data=storage.mass.storage_path,
            cache=storage.mass.cache_path,
            registered=registered,
            share=share,
        ),
        target_is_directory=True,
    )

    with pytest.raises(InvalidDataError) as exc_info:
        await storage.add_local_folder(str(link))

    assert exc_info.value.translation_key == translation_key
    assert storage.mass.config.get(CONF_STORAGE_FOLDERS) == [str(registered)]


async def test_add_accepts_a_symlink_to_a_folder(
    storage: StorageController, tmp_path: Path
) -> None:
    """A symlink to an ordinary folder adds that folder, and the folder can not be added twice."""
    (tmp_path / "music").mkdir()
    link = tmp_path / "link"
    link.symlink_to(tmp_path / "music", target_is_directory=True)

    location = await storage.add_local_folder(f"{link}/")
    with pytest.raises(InvalidDataError) as exc_info:
        await storage.add_local_folder(str(tmp_path / "music"))

    assert location.path == str(tmp_path / "music")
    assert location.available
    assert storage.mass.config.get(CONF_STORAGE_FOLDERS) == [str(tmp_path / "music")]
    assert exc_info.value.translation_key == "folder_already_location"


async def test_folder_behind_a_symlinked_parent(storage: StorageController, tmp_path: Path) -> None:
    """
    A folder reached through a symlinked parent is registered as the folder it is.

    Like /tmp/music on macOS, where /tmp links to /private/tmp: a folder inside it can be listed
    and a music source can be put on it, with the links resolved the way the folder picker
    checks a path.
    """
    (tmp_path / "private" / "music" / "Albums").mkdir(parents=True)
    (tmp_path / "tmp").symlink_to(tmp_path / "private", target_is_directory=True)
    real = tmp_path / "private" / "music"

    location = await storage.add_local_folder(str(tmp_path / "tmp" / "music"))

    assert location.path == str(real)
    assert storage.mass.config.get(CONF_STORAGE_FOLDERS) == [str(real)]
    assert await storage.list_folders(str(real)) == ["Albums"]
    albums = os.path.realpath(tmp_path / "tmp" / "music" / "Albums")
    assert storage.can_hold_music_source(albums, manages_all_sources=True)
    assert storage.can_hold_music_source(albums, manages_all_sources=False)


async def test_add_allows_a_folder_inside_the_data_folder(
    storage: StorageController,
) -> None:
    """Only the data folder itself is refused, not a folder below it."""
    music = Path(storage.mass.storage_path) / "music"
    music.mkdir()

    assert (await storage.add_local_folder(str(music))).path == str(music)


@pytest.mark.parametrize(
    ("path", "translation_key"),
    [
        ("music", "folder_path_not_absolute"),
        ("./music", "folder_path_not_absolute"),
        ("{tmp}/missing", "folder_not_found"),
        ("{tmp}/file.txt", "folder_not_found"),
    ],
)
async def test_add_refuses_what_is_no_folder(
    storage: StorageController, tmp_path: Path, path: str, translation_key: str
) -> None:
    """Only the absolute path of an existing folder can be added."""
    (tmp_path / "file.txt").write_text("not a folder")

    with pytest.raises(InvalidDataError) as exc_info:
        await storage.add_local_folder(path.format(tmp=tmp_path))

    assert exc_info.value.translation_key == translation_key
    assert storage.mass.config.get(CONF_STORAGE_FOLDERS) is None


@pytest.mark.parametrize(
    ("hass_addon", "container_marker"),
    [(True, False), (False, True), (True, True)],
)
async def test_add_refused_in_a_container(
    storage: StorageController,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    hass_addon: bool,
    container_marker: bool,
) -> None:
    """Inside a container or under a Supervisor a folder has to come in as a volume."""
    marker = tmp_path / ".dockerenv"
    if container_marker:
        marker.touch()
    monkeypatch.setattr(controller_module, "CONTAINER_MARKER_FILES", (str(marker),))
    storage.mass.running_as_hass_addon = hass_addon
    await storage.setup(await storage.mass.config.get_core_config(storage.domain))

    assert not storage.can_add_local_folder
    with pytest.raises(ActionUnavailable) as exc_info:
        await storage.add_local_folder(str(tmp_path))
    assert exc_info.value.translation_key == "local_folder_not_allowed"
    assert storage.mass.config.get(CONF_STORAGE_FOLDERS) is None


async def test_add_allowed_on_bare_metal(
    storage: StorageController, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without a container and without a Supervisor a folder can be added."""
    monkeypatch.setattr(controller_module, "CONTAINER_MARKER_FILES", (str(tmp_path / "none"),))
    await storage.setup(await storage.mass.config.get_core_config(storage.domain))

    assert storage.can_add_local_folder
    assert (await storage.add_local_folder(str(tmp_path))).path == str(tmp_path)


async def test_remove_folder(storage: StorageController, tmp_path: Path) -> None:
    """A registered folder that no music source uses can be removed."""
    await storage.add_local_folder(str(tmp_path))

    await storage.remove_local_folder(f"{tmp_path}/")

    assert storage.mass.config.get(CONF_STORAGE_FOLDERS) == []
    assert not any(loc.path == str(tmp_path) for loc in storage.get_locations())


@pytest.mark.parametrize("subfolder", ["", "/Albums", "/Albums/Artist"])
async def test_remove_refused_while_in_use(
    storage: StorageController, tmp_path: Path, subfolder: str
) -> None:
    """A folder a music source reads from, directly or below it, stays registered."""
    await storage.add_local_folder(str(tmp_path))
    store_source(storage, f"{tmp_path}{subfolder}")

    with pytest.raises(ActionUnavailable) as exc_info:
        await storage.remove_local_folder(str(tmp_path))

    assert exc_info.value.translation_key == "location_in_use"
    assert exc_info.value.translation_args == ["My music"]
    assert storage.mass.config.get(CONF_STORAGE_FOLDERS) == [str(tmp_path)]


@pytest.mark.parametrize(
    ("domain", "base_path"),
    [
        # a sibling folder that only starts with the same name
        ("filesystem_local", "{folder}-old"),
        ("filesystem_local", "{parent}"),
        # a source that does not read from local folders
        ("webdav", "{folder}"),
    ],
)
async def test_remove_allowed_when_no_source_reads_from_it(
    storage: StorageController, tmp_path: Path, domain: str, base_path: str
) -> None:
    """Sources outside the folder, or that are no local files source, do not block removal."""
    folder = tmp_path / "music"
    folder.mkdir()
    await storage.add_local_folder(str(folder))
    store_source(
        storage,
        base_path.format(folder=folder, parent=tmp_path),
        instance_id=f"{domain}--abc",
        domain=domain,
    )

    await storage.remove_local_folder(str(folder))

    assert storage.mass.config.get(CONF_STORAGE_FOLDERS) == []


async def test_remove_refuses_an_unregistered_folder(
    storage: StorageController, tmp_path: Path
) -> None:
    """Only a registered folder can be removed."""
    await storage.add_local_folder(str(tmp_path))

    with pytest.raises(InvalidDataError) as exc_info:
        await storage.remove_local_folder(str(tmp_path / "music"))

    assert exc_info.value.translation_key == "folder_not_registered"


async def test_used_by(storage: StorageController, tmp_path: Path) -> None:
    """
    A location names the sources that read from it or from a folder inside it, sorted.

    A source without a name of its own shows its default name. A source on a sibling folder
    whose name only starts the same way, or one that reads no local folder, is no user of it.
    """
    music = tmp_path / "music"
    (music / "Albums").mkdir(parents=True)
    (tmp_path / "music-old").mkdir()
    await storage.add_local_folder(str(music))
    store_source(storage, music, "filesystem_local--a", "Music")
    store_source(storage, f"{music}/Albums/", "filesystem_local--b", "albums")
    store_source(storage, music, "filesystem_local--c", None)
    store_source(storage, f"{music}-old", "filesystem_local--d", "Old music")
    store_source(storage, music, "webdav--e", "Cloud", domain="webdav")

    info = await storage.get_info()

    assert _location(info.locations, music).used_by == ["albums", "Local files", "Music"]
    assert all(loc.used_by == [] for loc in info.locations if loc.path != str(music))


async def test_used_by_every_location_around_the_source(
    storage: StorageController, tmp_path: Path
) -> None:
    """A source in a nested location uses the outer location too, so neither can be removed."""
    music = tmp_path / "music"
    classical = music / "classical"
    (classical / "Bach").mkdir(parents=True)
    await storage.add_local_folder(str(music))
    await storage.add_local_folder(str(classical))
    store_source(storage, classical / "Bach")

    info = await storage.get_info()

    assert _location(info.locations, music).used_by == ["My music"]
    assert _location(info.locations, classical).used_by == ["My music"]
    for folder in (classical, music):
        with pytest.raises(ActionUnavailable) as exc_info:
            await storage.remove_local_folder(str(folder))
        assert exc_info.value.translation_key == "location_in_use"
    assert storage.mass.config.get(CONF_STORAGE_FOLDERS) == [str(music), str(classical)]


async def test_source_that_failed_to_load_uses_its_location(
    storage: StorageController, tmp_path: Path
) -> None:
    """A source that did not load, e.g. because its share is down, still keeps its location."""
    await storage.add_local_folder(str(tmp_path))
    store_source(storage, tmp_path)

    info = await storage.get_info()

    assert _location(info.locations, tmp_path).used_by == ["My music"]
    with pytest.raises(ActionUnavailable) as exc_info:
        await storage.remove_local_folder(str(tmp_path))
    assert exc_info.value.translation_key == "location_in_use"
    assert storage.mass.config.get(CONF_STORAGE_FOLDERS) == [str(tmp_path)]


async def test_disabled_source_uses_no_location(storage: StorageController, tmp_path: Path) -> None:
    """A disabled source does not keep its location from removal."""
    await storage.add_local_folder(str(tmp_path))
    store_source(storage, tmp_path, enabled=False)

    info = await storage.get_info()

    assert _location(info.locations, tmp_path).used_by == []
    await storage.remove_local_folder(str(tmp_path))
    assert storage.mass.config.get(CONF_STORAGE_FOLDERS) == []


async def test_loaded_source_is_listed_once(storage: StorageController, tmp_path: Path) -> None:
    """A loaded source and its stored config are one source."""
    await storage.add_local_folder(str(tmp_path))
    store_source(storage, tmp_path)
    loaded = MagicMock(domain="filesystem_local", base_path=str(tmp_path))
    loaded.name = "My music"
    storage.mass._providers["filesystem_local--abc"] = loaded

    info = await storage.get_info()

    assert _location(info.locations, tmp_path).used_by == ["My music"]


async def test_refused_removal_probes_nothing(
    storage: StorageController, tmp_path: Path, probes: FakeProbes
) -> None:
    """Refusing to remove a folder in use touches no location."""
    await storage.add_local_folder(str(tmp_path))
    store_source(storage, tmp_path)
    probes.calls.clear()

    with pytest.raises(ActionUnavailable):
        await storage.remove_local_folder(str(tmp_path))

    assert probes.calls == []


async def test_member_does_not_see_the_sources(storage: StorageController, tmp_path: Path) -> None:
    """A caller that does not manage every source never learns which sources use a location."""
    await storage.add_local_folder(str(tmp_path))
    store_source(storage, tmp_path)
    set_current_user(MEMBER)

    info = await storage.get_info()

    assert _location(info.locations, tmp_path).used_by == []
    assert _location(storage.get_locations(), tmp_path).used_by == ["My music"]


@pytest.mark.parametrize("base_path", ["{parent}", "{parent}/", "/"])
async def test_read_by_a_source_around_the_location(
    storage: StorageController, tmp_path: Path, base_path: str
) -> None:
    """A source whose folder contains a location reads it, and does not keep it from removal."""
    folder = tmp_path / "music"
    folder.mkdir()
    await storage.add_local_folder(str(folder))
    store_source(storage, base_path.format(parent=tmp_path))

    location = _location((await storage.get_info()).locations, folder)
    await storage.remove_local_folder(str(folder))

    assert (location.used_by, location.read_by) == ([], ["My music"])
    assert storage.mass.config.get(CONF_STORAGE_FOLDERS) == []


@pytest.mark.parametrize("base_path", ["{folder}", "{folder}/", "{folder}/Albums"])
async def test_source_in_the_location_is_no_reader(
    storage: StorageController, tmp_path: Path, base_path: str
) -> None:
    """A source whose folder is a location or lies inside it uses the location, nothing more."""
    folder = tmp_path / "music"
    folder.mkdir()
    await storage.add_local_folder(str(folder))
    store_source(storage, base_path.format(folder=folder))

    location = _location((await storage.get_info()).locations, folder)
    with pytest.raises(ActionUnavailable) as exc_info:
        await storage.remove_local_folder(str(folder))

    assert (location.used_by, location.read_by) == (["My music"], [])
    assert exc_info.value.translation_key == "location_in_use"
    assert storage.mass.config.get(CONF_STORAGE_FOLDERS) == [str(folder)]


@pytest.mark.parametrize(
    ("location_name", "source_name"), [("mediafiles", "media"), ("media", "mediafiles")]
)
async def test_look_alike_source_neither_uses_nor_reads(
    storage: StorageController, tmp_path: Path, location_name: str, source_name: str
) -> None:
    """A source on a folder whose name only starts like that of a location is not listed."""
    folder = tmp_path / location_name
    folder.mkdir()
    await storage.add_local_folder(str(folder))
    store_source(storage, tmp_path / source_name)

    location = _location((await storage.get_info()).locations, folder)

    assert (location.used_by, location.read_by) == ([], [])


@pytest.mark.parametrize("base_path", ["{parent}", "{folder}"])
async def test_disabled_source_reads_no_location(
    storage: StorageController, tmp_path: Path, base_path: str
) -> None:
    """A disabled source is listed on no location, around it or in it."""
    folder = tmp_path / "music"
    folder.mkdir()
    await storage.add_local_folder(str(folder))
    store_source(storage, base_path.format(parent=tmp_path, folder=folder), enabled=False)

    location = _location((await storage.get_info()).locations, folder)

    assert (location.used_by, location.read_by) == ([], [])


async def test_location_used_and_read_by_other_sources(
    storage: StorageController, tmp_path: Path
) -> None:
    """
    A location lists the sources in it apart from the sources around it, each sorted.

    Only a source in it keeps the location from removal.
    """
    folder = tmp_path / "music" / "extra"
    (folder / "Albums").mkdir(parents=True)
    await storage.add_local_folder(str(folder))
    store_source(storage, folder / "Albums", "filesystem_local--a", "Inside")
    store_source(storage, tmp_path / "music", "filesystem_local--b", "zeta")
    store_source(storage, "/", "filesystem_local--c", "Alpha")

    location = _location((await storage.get_info()).locations, folder)
    with pytest.raises(ActionUnavailable) as exc_info:
        await storage.remove_local_folder(str(folder))

    assert (location.used_by, location.read_by) == (["Inside"], ["Alpha", "zeta"])
    assert exc_info.value.translation_args == ["Inside"]
    assert storage.mass.config.get(CONF_STORAGE_FOLDERS) == [str(folder)]


async def test_server_folders_are_read_by_no_source(
    storage: StorageController, tmp_path: Path
) -> None:
    """The data and cache rows list no source, also when a source's folder contains them."""
    folder = tmp_path / "music"
    folder.mkdir()
    await storage.add_local_folder(str(folder))
    store_source(storage, tmp_path)

    info = await storage.get_info()

    server_rows = [loc for loc in info.locations if loc.usage != StorageUsage.MEDIA]
    assert [loc.usage for loc in server_rows] == [StorageUsage.DATA, StorageUsage.CACHE]
    assert all((loc.used_by, loc.read_by) == ([], []) for loc in server_rows)
    assert _location(info.locations, folder).read_by == ["My music"]


async def test_member_does_not_see_the_readers(storage: StorageController, tmp_path: Path) -> None:
    """A caller that does not manage every source never learns which sources read a location."""
    folder = tmp_path / "music"
    folder.mkdir()
    await storage.add_local_folder(str(folder))
    store_source(storage, tmp_path)
    set_current_user(MEMBER)

    info = await storage.get_info()

    assert _location(info.locations, folder).read_by == []
    assert _location(storage.get_locations(), folder).read_by == ["My music"]


async def test_removed_folder_says_so(storage: StorageController, tmp_path: Path) -> None:
    """
    A registered folder removed from disk says it does not exist, until it is back.

    A caller that does not manage every source gets the same reason: it names nothing more than
    the location itself.
    """
    music = tmp_path / "music"
    music.mkdir()
    await storage.add_local_folder(str(music))
    music.rmdir()
    _outdate_answer(storage, music)

    location = _location((await storage.get_info()).locations, music)
    set_current_user(MEMBER)
    member_view = _location((await storage.get_info()).locations, music)

    assert (location.available, location.error_key, location.error_args) == (
        False,
        "folder_missing",
        [],
    )
    assert location.error is not None
    assert member_view == location
    music.mkdir()
    _outdate_answer(storage, music)
    location = _location((await storage.get_info()).locations, music)
    assert (location.available, location.error, location.error_key) == (True, None, None)


@pytest.mark.parametrize(
    ("error_key", "text"),
    [
        ("folder_missing", "This folder does not exist."),
        ("folder_not_mounted", "The drive or share of this folder is not mounted."),
        ("storage_not_responding", "The storage of this location does not respond."),
    ],
)
async def test_location_errors_are_translated(
    storage: StorageController, error_key: str, text: str
) -> None:
    """The reason a location is not available reads from the strings of the server."""
    translations = TranslationController(storage.mass)
    await translations.setup(MagicMock())
    location = StorageLocation(
        path="/srv/music",
        name="music",
        usage=StorageUsage.MEDIA,
        kind=StorageKind.MANUAL,
        available=False,
        error="English fallback",
        error_key=error_key,
    )

    token = TRANSLATION_RESOLVER.set(partial(translations.get_translation, locale="en"))
    try:
        serialized = location.to_dict()
    finally:
        TRANSLATION_RESOLVER.reset(token)

    assert serialized["error"].startswith(text)
