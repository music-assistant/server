"""Tests for probing the storage locations, including shares whose server is gone."""

from __future__ import annotations

import asyncio
import errno
import os
import time
from contextlib import suppress
from pathlib import Path

import pytest
from music_assistant_models.errors import ActionUnavailable

from music_assistant.constants import CONF_STORAGE_FOLDERS
from music_assistant.controllers.storage import StorageController, StorageKind
from music_assistant.controllers.storage import controller as controller_module
from music_assistant.controllers.storage.controller import _probe_path, _ProbeResult
from tests.controllers.storage.conftest import (
    FakeProbes,
    MountTable,
    mount_line,
    wait_until,
)

DEAD_SHARE = "/mnt/dead"


@pytest.fixture
def short_probe_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make a refresh give up on a probe quickly."""
    monkeypatch.setattr(controller_module, "PROBE_TIMEOUT", 0.2)


@pytest.mark.usefixtures("short_probe_timeout")
async def test_dead_share_does_not_hold_up_the_others(
    storage: StorageController, mount_table: MountTable, probes: FakeProbes
) -> None:
    """A share whose server is gone is listed as unavailable, the others as they are."""
    mount_table.set(mount_line(DEAD_SHARE, "nfs4"), mount_line("/mnt/music", "ext4"))
    probes.block(DEAD_SHARE)

    started = time.monotonic()
    await storage.refresh()

    assert time.monotonic() - started < 2
    by_path = {loc.path: loc for loc in storage.get_locations()}
    assert not by_path[DEAD_SHARE].available
    assert by_path[DEAD_SHARE].free_space_gb is None
    assert by_path["/mnt/music"].available


@pytest.mark.usefixtures("short_probe_timeout")
async def test_late_answer_is_applied(
    storage: StorageController, mount_table: MountTable, probes: FakeProbes
) -> None:
    """A probe that answers after the refresh stopped waiting still updates its location."""
    mount_table.set(mount_line(DEAD_SHARE, "nfs4"))
    probes.block(DEAD_SHARE)
    await storage.refresh()

    probes.release()

    await wait_until(lambda: storage.get_locations()[0].available)
    assert storage.get_locations()[0].free_space_gb == 75.0


@pytest.mark.usefixtures("short_probe_timeout")
async def test_one_probe_in_flight_per_path(
    storage: StorageController, mount_table: MountTable, probes: FakeProbes
) -> None:
    """A blocked probe is waited for again rather than joined by a second one."""
    mount_table.set(mount_line(DEAD_SHARE, "nfs4"))
    probes.block(DEAD_SHARE)

    await storage.refresh()
    await storage.refresh()

    assert probes.calls.count(DEAD_SHARE) == 1
    # every other path is probed on each refresh
    assert probes.calls.count(storage.mass.storage_path) == 2


async def test_folder_commands_do_not_wait_for_a_dead_share(
    storage: StorageController, mount_table: MountTable, probes: FakeProbes, tmp_path: Path
) -> None:
    """Adding and removing a folder answer while another location's probe is blocked."""
    mount_table.set(mount_line(DEAD_SHARE, "nfs4"))
    probes.block(DEAD_SHARE)
    refresh = storage.mass.create_task(storage.refresh())

    async with asyncio.timeout(2):
        location = await storage.add_local_folder(str(tmp_path))
        await storage.remove_local_folder(str(tmp_path))

    assert location.available
    assert storage.mass.config.get(CONF_STORAGE_FOLDERS) == []
    refresh.cancel()
    with suppress(asyncio.CancelledError):
        await refresh


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
    """Probing a share nobody accessed yet mounts it, and the real mount is listed."""
    storage._in_container = True
    trigger = mount_line("/media/archive", "autofs")
    mount_table.set(trigger)
    # the probe accesses the share, so the Supervisor mounts it on top of the trigger
    probes.side_effects["/media/archive"] = lambda: mount_table.set(
        trigger, mount_line("/media/archive", "cifs")
    )

    await storage.refresh()

    location = storage.get_locations()[0]
    assert (location.path, location.kind, location.fstype) == (
        "/media/archive",
        StorageKind.NETWORK_SHARE,
        "cifs",
    )
    assert location.available
    assert location.free_space_gb == 75.0


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

    await storage.refresh()

    location = storage.get_locations()[0]
    assert (location.path, location.fstype, location.available) == (
        "/media/archive",
        "autofs",
        False,
    )
    assert location.free_space_gb is None


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
