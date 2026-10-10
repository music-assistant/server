"""Tests for when a location below a folder counts as gone for now, and when for good."""

from __future__ import annotations

from pathlib import Path

import pytest
from music_assistant_models.errors import ActionUnavailable

from music_assistant.controllers.storage import StorageController
from music_assistant.controllers.storage import controller as controller_module
from music_assistant.controllers.storage.backends.local_mount import MOUNT_ROOT
from music_assistant.controllers.storage.models import ShareType
from tests.controllers.storage.conftest import FakeSupervisor, MountTable, mount_line

pytestmark = pytest.mark.usefixtures("probes", "discoverable_tmp_path")

ROOT_MOUNT = mount_line("/", "ext4")


async def test_folder_left_behind_counts_until_it_is_gone(
    storage: StorageController, tmp_path: Path, mount_table: MountTable
) -> None:
    """A share seen since the start counts while the empty folder it left behind is there."""
    nas = tmp_path / "media" / "nas_music"
    nas.mkdir(parents=True)
    mount_table.set(ROOT_MOUNT, mount_line(nas, "cifs"))
    await storage.refresh()
    mount_table.set(ROOT_MOUNT)
    assert await storage.get_unavailable_locations(str(tmp_path / "media")) == [str(nas)]

    nas.rmdir()

    assert await storage.get_unavailable_locations(str(tmp_path / "media")) == []


async def test_registered_mount_counts_until_removed(
    storage: StorageController, tmp_path: Path, mount_table: MountTable
) -> None:
    """A registered folder whose drive is gone counts until the folder is removed again."""
    usb = tmp_path / "usb"
    usb.mkdir()
    mount_table.set(ROOT_MOUNT, mount_line(usb, "ext4"))
    await storage.add_local_folder(str(usb))
    mount_table.set(ROOT_MOUNT)
    assert await storage.get_unavailable_locations(str(tmp_path)) == [str(usb)]

    await storage.remove_local_folder(str(usb))

    assert usb.is_dir()
    assert await storage.get_unavailable_locations(str(tmp_path)) == []


@pytest.mark.usefixtures("mounter")
async def test_managed_share_counts_until_removed(
    storage: StorageController, mount_table: MountTable
) -> None:
    """A share added in Music Assistant counts while it is not mounted, also without a folder."""
    location = await storage.add_network_share(ShareType.CIFS, "nas.local", "Music")
    mount_table.unmount(location.path)
    assert not Path(location.path).exists()
    assert await storage.get_unavailable_locations(MOUNT_ROOT) == [location.path]

    await storage.remove_network_share("music")

    assert await storage.get_unavailable_locations(MOUNT_ROOT) == []


async def test_share_of_home_assistant_counts_until_removed_there(
    storage: StorageController, mount_table: MountTable, supervisor: FakeSupervisor
) -> None:
    """
    A share added in Home Assistant counts while it is down, also when it was never mounted.

    That is the case when the server started while the share was down: only the empty folder
    is left, and the mount table does not show the share.
    """
    supervisor.add_mount("nas_music", state="inactive", type="cifs", server="nas.local")
    nas = Path(supervisor.path("nas_music"))
    mount_table.unmount(str(nas))
    (nas / "share.txt").unlink()
    await storage._probe_backends()
    assert await storage.get_unavailable_locations(str(supervisor.media)) == [str(nas)]

    del supervisor.mounts["nas_music"]

    assert nas.is_dir()
    assert await storage.get_unavailable_locations(str(supervisor.media)) == []


async def test_supervisor_that_did_not_answer_at_the_start_is_asked_again(
    storage: StorageController, mount_table: MountTable, supervisor: FakeSupervisor
) -> None:
    """A Supervisor that was not reachable when the server started is asked for its shares."""
    supervisor.add_mount("nas_music", state="inactive", type="cifs", server="nas.local")
    nas = supervisor.path("nas_music")
    mount_table.unmount(nas)
    supervisor.refuse_access = True
    await storage._probe_backends()
    supervisor.refuse_access = False

    assert await storage.get_unavailable_locations(str(supervisor.media)) == [nas]


async def test_supervisor_that_does_not_answer_leaves_it_unknown(
    storage: StorageController,
    mount_table: MountTable,
    supervisor: FakeSupervisor,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Supervisor that does not list its shares in time leaves unknown which ones are down."""
    supervisor.add_mount("nas_music", state="inactive", type="cifs", server="nas.local")
    mount_table.unmount(supervisor.path("nas_music"))
    await storage._probe_backends()
    monkeypatch.setattr(controller_module, "SHARE_STATES_TIMEOUT", 0.05)
    supervisor.list_released.clear()

    try:
        with pytest.raises(ActionUnavailable) as exc_info:
            await storage.get_unavailable_locations(str(supervisor.media))
    finally:
        supervisor.list_released.set()

    assert exc_info.value.translation_key == "shares_not_listed"
