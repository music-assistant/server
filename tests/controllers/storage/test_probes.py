"""Tests for probing the storage locations: only on demand, and never held up by a dead share."""

from __future__ import annotations

import asyncio
import errno
import os
import time
from contextlib import suppress
from pathlib import Path

import pytest
from music_assistant_models.auth import User, UserRole
from music_assistant_models.errors import ActionUnavailable

from music_assistant.constants import CONF_STORAGE_FOLDERS
from music_assistant.controllers.storage import StorageController, StorageKind, StorageLocation
from music_assistant.controllers.storage import controller as controller_module
from music_assistant.controllers.storage.constants import PROBE_MAX_AGE
from music_assistant.controllers.storage.controller import _probe_path, _ProbeResult
from music_assistant.controllers.webserver.helpers.auth_middleware import set_current_user
from tests.controllers.storage.conftest import (
    FOLDER,
    FakeProbes,
    MountTable,
    mount_line,
    wait_until,
)

DEAD_SHARE = "/mnt/dead"
NAS = "/mnt/nas"
OLD_ANSWER = _ProbeResult(is_dir=True, free_space_gb=1.0, total_space_gb=2.0)


@pytest.fixture
def short_probe_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make a caller give up on a probe quickly."""
    monkeypatch.setattr(controller_module, "PROBE_TIMEOUT", 0.2)


def _location(storage: StorageController, path: str) -> StorageLocation:
    """Return the listed location on a path."""
    return next(loc for loc in storage.get_locations() if loc.path == path)


async def _answered(storage: StorageController, path: str, age: float) -> None:
    """
    Give a path an answer of the given age, as if a caller had it probed back then.

    :param storage: The storage controller.
    :param path: The probed path.
    :param age: The age of the answer in seconds.
    """
    await storage._wait_for_probes([path])
    storage._probes[path].answer = OLD_ANSWER
    storage._probes[path].answered_at = time.monotonic() - age
    await storage.refresh()


async def test_timer_touches_no_location(
    storage: StorageController, mount_table: MountTable, probes: FakeProbes
) -> None:
    """The periodic refresh only reads the mount table, so an idle share is left alone."""
    mount_table.set(mount_line(NAS, "cifs"), mount_line("/media/archive", "autofs"))
    storage.mass.config.set(CONF_STORAGE_FOLDERS, ["/srv/music"])

    await storage._periodic_refresh()
    await storage._periodic_refresh()

    assert probes.calls == []
    assert {loc.path for loc in storage.get_locations()} >= {NAS, "/media/archive", "/srv/music"}


async def test_never_probed_locations(
    storage: StorageController, mount_table: MountTable, probes: FakeProbes
) -> None:
    """
    Before any probe the mount table decides.

    A mount counts as usable, a dormant automount trigger does not (the share is not mounted),
    and neither does a registered folder, which may be gone. A registered folder that is a mount
    goes by its mount, and the server's own folders count as usable: the server runs from them.
    """
    mount_table.set(
        mount_line(NAS, "cifs"),
        mount_line("/media/archive", "autofs"),
        mount_line("/mnt/usb", "vfat"),
    )
    storage.mass.config.set(CONF_STORAGE_FOLDERS, ["/srv/music", "/mnt/usb"])

    await storage.refresh()

    assert {loc.path: loc.available for loc in storage.get_locations()} == {
        NAS: True,
        "/media/archive": False,
        "/mnt/usb": True,
        "/srv/music": False,
        storage.mass.storage_path: True,
        storage.mass.cache_path: True,
    }
    assert all(loc.free_space_gb is None for loc in storage.get_locations())
    # an error says what a probe found
    assert all(loc.error is None and loc.error_key is None for loc in storage.get_locations())
    assert probes.calls == []


async def test_info_probes_what_is_outdated(
    storage: StorageController, mount_table: MountTable, probes: FakeProbes
) -> None:
    """The info probes a location without a recent answer, and not one with a fresh answer."""
    mount_table.set(mount_line(NAS, "cifs"), mount_line("/mnt/music", "ext4"))
    await storage.refresh()
    await _answered(storage, NAS, age=PROBE_MAX_AGE + 1)
    await _answered(storage, "/mnt/music", age=1)
    probes.calls.clear()

    info = await storage.get_info()

    assert NAS in probes.calls
    assert "/mnt/music" not in probes.calls
    by_path = {loc.path: loc for loc in info.locations}
    assert by_path[NAS].free_space_gb == FOLDER.free_space_gb
    assert by_path["/mnt/music"].free_space_gb == OLD_ANSWER.free_space_gb


async def test_member_info_probes_only_what_it_sees(
    storage: StorageController, mount_table: MountTable, probes: FakeProbes
) -> None:
    """A caller that does not manage every source makes the server probe its own view only."""
    mount_table.set(mount_line(NAS, "cifs"))
    storage.mass.config.set(CONF_STORAGE_FOLDERS, ["/srv/music"])
    set_current_user(User(user_id="member", username="member", role=UserRole.USER))

    await storage.get_info()

    assert probes.calls == ["/srv/music"]


@pytest.mark.parametrize("age", [PROBE_MAX_AGE + 1, None])
async def test_is_available_probes_what_is_outdated(
    storage: StorageController, mount_table: MountTable, probes: FakeProbes, age: float | None
) -> None:
    """A source checking its folder has the location probed when the answer is old or missing."""
    mount_table.set(mount_line(NAS, "cifs"))
    await storage.refresh()
    if age is not None:
        await _answered(storage, NAS, age=age)
    probes.calls.clear()
    probes.results[NAS] = None

    assert not await storage.is_available(f"{NAS}/music")
    assert probes.calls == [NAS]


async def test_is_available_goes_by_a_fresh_answer(
    storage: StorageController, mount_table: MountTable, probes: FakeProbes
) -> None:
    """A location with a fresh answer is not probed again."""
    mount_table.set(mount_line(NAS, "cifs"))
    await storage.refresh()
    await _answered(storage, NAS, age=1)
    probes.calls.clear()

    await storage.is_available(f"{NAS}/music")

    assert probes.calls == []


@pytest.mark.usefixtures("mount_table")
@pytest.mark.parametrize("age", [PROBE_MAX_AGE + 1, None])
async def test_list_folders_probes_what_is_outdated(
    storage: StorageController,
    probes: FakeProbes,
    tmp_path: Path,
    age: float | None,
) -> None:
    """Browsing a registered folder has it probed first when its answer is old or missing."""
    music = tmp_path / "music"
    (music / "Albums").mkdir(parents=True)
    storage.mass.config.set(CONF_STORAGE_FOLDERS, [str(music)])
    await storage.refresh()
    if age is not None:
        await _answered(storage, str(music), age=age)
    probes.calls.clear()

    assert await storage.list_folders(str(music)) == ["Albums"]
    assert probes.calls == [str(music)]


@pytest.mark.usefixtures("mount_table")
async def test_list_folders_goes_by_a_fresh_answer(
    storage: StorageController, probes: FakeProbes, tmp_path: Path
) -> None:
    """A location with a fresh answer is listed without probing it again."""
    music = tmp_path / "music"
    (music / "Albums").mkdir(parents=True)
    storage.mass.config.set(CONF_STORAGE_FOLDERS, [str(music)])
    await storage.refresh()
    await _answered(storage, str(music), age=1)
    probes.calls.clear()

    assert await storage.list_folders(str(music)) == ["Albums"]
    assert probes.calls == []


async def test_previous_answer_stays_while_a_probe_is_in_flight(
    storage: StorageController,
    mount_table: MountTable,
    probes: FakeProbes,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A location does not flicker while it is probed, until the probe is overdue."""
    monkeypatch.setattr(controller_module, "PROBE_TIMEOUT", 1.0)
    mount_table.set(mount_line(NAS, "cifs"))
    await storage.refresh()
    await _answered(storage, NAS, age=PROBE_MAX_AGE + 1)
    probes.block(NAS)
    checking = storage.mass.create_task(storage.is_available(f"{NAS}/music"))
    await wait_until(lambda: NAS in probes.calls)

    await storage.refresh()
    assert _location(storage, NAS).available
    assert _location(storage, NAS).free_space_gb == OLD_ANSWER.free_space_gb

    assert not await checking
    assert not _location(storage, NAS).available
    assert _location(storage, NAS).free_space_gb is None


@pytest.mark.usefixtures("short_probe_timeout")
async def test_dead_share_does_not_hold_up_the_others(
    storage: StorageController, mount_table: MountTable, probes: FakeProbes
) -> None:
    """A share whose server is gone is listed as unavailable, the others as they are."""
    mount_table.set(mount_line(DEAD_SHARE, "nfs4"), mount_line("/mnt/music", "ext4"))
    probes.block(DEAD_SHARE)

    started = time.monotonic()
    await storage.get_info()

    assert time.monotonic() - started < 2
    assert not _location(storage, DEAD_SHARE).available
    assert _location(storage, DEAD_SHARE).free_space_gb is None
    assert _location(storage, "/mnt/music").available


async def test_overdue_probe_is_not_waited_for_again(
    storage: StorageController,
    mount_table: MountTable,
    probes: FakeProbes,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Once a probe did not answer in time, the next caller does not wait for it again."""
    monkeypatch.setattr(controller_module, "PROBE_TIMEOUT", 1.0)
    mount_table.set(mount_line(DEAD_SHARE, "nfs4"))
    probes.block(DEAD_SHARE)
    await storage.get_info()

    started = time.monotonic()
    await storage.get_info()

    assert time.monotonic() - started < 0.5
    assert probes.calls.count(DEAD_SHARE) == 1


@pytest.mark.usefixtures("short_probe_timeout")
async def test_location_that_does_not_respond_says_so(
    storage: StorageController, mount_table: MountTable, probes: FakeProbes
) -> None:
    """A mount or registered folder whose probe does not answer in time says so, until it does."""
    mount_table.set(mount_line(DEAD_SHARE, "nfs4"), mount_line("/mnt/music", "ext4"))
    storage.mass.config.set(CONF_STORAGE_FOLDERS, ["/srv/dead"])
    probes.block(DEAD_SHARE)
    probes.block("/srv/dead")

    await storage.get_info()

    for path in (DEAD_SHARE, "/srv/dead"):
        location = _location(storage, path)
        assert (location.available, location.error_key) == (False, "storage_not_responding")
        assert location.error is not None
    assert _location(storage, "/mnt/music").error is None

    probes.release()

    await wait_until(
        lambda: (
            _location(storage, DEAD_SHARE).available and _location(storage, "/srv/dead").available
        )
    )
    for path in (DEAD_SHARE, "/srv/dead"):
        assert (_location(storage, path).error, _location(storage, path).error_key) == (None, None)


@pytest.mark.usefixtures("short_probe_timeout")
async def test_late_answer_is_applied(
    storage: StorageController, mount_table: MountTable, probes: FakeProbes
) -> None:
    """A probe that answers after its caller stopped waiting still updates its location."""
    mount_table.set(mount_line(DEAD_SHARE, "nfs4"))
    probes.block(DEAD_SHARE)
    await storage.get_info()

    probes.release()

    await wait_until(lambda: _location(storage, DEAD_SHARE).free_space_gb is not None)
    assert _location(storage, DEAD_SHARE).available


async def test_one_probe_in_flight_per_path(
    storage: StorageController, mount_table: MountTable, probes: FakeProbes
) -> None:
    """Callers that need the same location at once share one probe."""
    mount_table.set(mount_line(NAS, "cifs"))
    await storage.refresh()
    probes.block(NAS)
    checks = [storage.mass.create_task(storage.is_available(NAS)) for _ in range(3)]
    await wait_until(lambda: NAS in probes.calls)

    probes.release()
    await asyncio.gather(*checks)

    assert probes.calls.count(NAS) == 1


async def test_folder_commands_do_not_wait_for_a_dead_share(
    storage: StorageController, mount_table: MountTable, probes: FakeProbes, tmp_path: Path
) -> None:
    """Adding and removing a folder answer while another location's probe is blocked."""
    mount_table.set(mount_line(DEAD_SHARE, "nfs4"))
    probes.block(DEAD_SHARE)
    info = storage.mass.create_task(storage.get_info())
    await wait_until(lambda: DEAD_SHARE in probes.calls)

    async with asyncio.timeout(2):
        location = await storage.add_local_folder(str(tmp_path))
        await storage.remove_local_folder(str(tmp_path))

    assert location.available
    assert storage.mass.config.get(CONF_STORAGE_FOLDERS) == []
    info.cancel()
    with suppress(asyncio.CancelledError):
        await info


@pytest.mark.usefixtures("short_probe_timeout", "mount_table")
async def test_folder_on_a_dead_share_is_not_added(
    storage: StorageController, probes: FakeProbes
) -> None:
    """A folder whose probe does not answer in time is refused, and nothing is stored."""
    probes.block("/srv/dead/music")

    with pytest.raises(ActionUnavailable) as exc_info:
        await storage.add_local_folder("/srv/dead/music")

    assert exc_info.value.translation_key == "folder_unreadable"
    assert storage.mass.config.get(CONF_STORAGE_FOLDERS) is None


async def test_dormant_automount_trigger_wakes_up(
    storage: StorageController, mount_table: MountTable, probes: FakeProbes
) -> None:
    """Opening the storage view probes a share nobody accessed yet, which mounts it."""
    trigger = mount_line("/media/archive", "autofs")
    mount_table.set(trigger)
    # the probe accesses the share, so the Supervisor mounts it on top of the trigger
    probes.side_effects["/media/archive"] = lambda: mount_table.set(
        trigger, mount_line("/media/archive", "cifs")
    )

    await storage.get_info()

    location = _location(storage, "/media/archive")
    assert (location.kind, location.fstype, location.available) == (
        StorageKind.NETWORK_SHARE,
        "cifs",
        True,
    )
    assert location.free_space_gb == FOLDER.free_space_gb


@pytest.mark.parametrize("answer", [_ProbeResult(is_dir=True, free_space_gb=0.0), None])
async def test_trigger_that_does_not_mount_is_unavailable(
    storage: StorageController,
    mount_table: MountTable,
    probes: FakeProbes,
    answer: _ProbeResult | None,
) -> None:
    """A share still behind its trigger after the probe, or whose probe failed, is unavailable."""
    mount_table.set(mount_line("/media/archive", "autofs"))
    probes.results["/media/archive"] = answer

    await storage.get_info()

    location = _location(storage, "/media/archive")
    assert (location.fstype, location.available, location.free_space_gb) == ("autofs", False, None)
    assert location.error_key == "storage_not_responding"


def test_probe_of_a_folder(tmp_path: Path) -> None:
    """A folder is a directory with the space of its filesystem."""
    result = _probe_path(str(tmp_path))

    assert result is not None
    assert result.is_dir
    assert result.total_space_gb is not None
    assert result.total_space_gb > 0


@pytest.mark.parametrize("name", ["file.txt", "missing", "file.txt/below", "nul\0byte"])
def test_probe_of_what_is_no_folder(tmp_path: Path, name: str) -> None:
    """A file, a missing path or an invalid path is no directory."""
    (tmp_path / "file.txt").write_text("not a folder")

    result = _probe_path(f"{tmp_path}/{name}")

    assert result is not None
    assert not result.is_dir


def test_probe_asks_the_filesystem_for_its_space_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The probe starts with statvfs, which wakes an automount trigger, and fails with it."""
    asked: list[str] = []

    def _host_down(path: str) -> os.statvfs_result:
        asked.append(path)
        raise OSError(errno.EHOSTDOWN, "Host is down", path)

    monkeypatch.setattr(os, "statvfs", _host_down)

    assert _probe_path(str(tmp_path)) is None
    assert asked == [str(tmp_path)]


async def test_waiter_of_a_replaced_probe_changes_nothing(
    storage: StorageController,
    mount_table: MountTable,
    probes: FakeProbes,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    The caller of a probe that a fresh one replaced does not mark the path as not answering.

    The old probe hangs, a fresh one answers, then the old caller gives up: the location stays
    available, is probed again once its answer is outdated, and the late answer of the old
    probe is dropped.
    """
    monkeypatch.setattr(controller_module, "PROBE_TIMEOUT", 0.3)
    mount_table.set(mount_line(NAS, "cifs"))
    await storage.refresh()
    probes.block(NAS)
    old_waiter = storage.mass.create_task(storage._wait_for_probes([NAS]))
    await wait_until(lambda: NAS in probes.calls)
    old_probe = storage._probes[NAS].probe
    assert old_probe is not None
    gate = probes.blocked.pop(NAS)

    try:
        assert await storage._wait_for_probes([NAS], fresh=True) == {NAS: FOLDER}
        assert await old_waiter == {NAS: None}
    finally:
        # the old probe fails in the end, about what was mounted before
        probes.results[NAS] = None
        gate.set()

    await wait_until(old_probe.done)
    await storage.refresh()
    assert not storage._probes[NAS].overdue
    assert _location(storage, NAS).available
    assert _location(storage, NAS).free_space_gb == FOLDER.free_space_gb
    storage._probes[NAS].answered_at = time.monotonic() - PROBE_MAX_AGE - 1
    probes.calls.clear()
    probes.results.pop(NAS)
    await storage.get_info()
    assert NAS in probes.calls
    assert _location(storage, NAS).available


@pytest.mark.usefixtures("short_probe_timeout")
async def test_fresh_probe_that_hangs_is_overdue(
    storage: StorageController, mount_table: MountTable, probes: FakeProbes
) -> None:
    """The caller of the current probe still marks a path that does not answer."""
    mount_table.set(mount_line(NAS, "cifs"))
    await storage.refresh()
    probes.block(NAS)

    assert await storage._wait_for_probes([NAS], fresh=True) == {NAS: None}

    assert storage._probes[NAS].overdue
    await storage.refresh()
    assert not _location(storage, NAS).available
