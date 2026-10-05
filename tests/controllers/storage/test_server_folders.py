"""Tests for keeping the server's own folders out, however the server was started with them."""

from __future__ import annotations

from pathlib import Path

import pytest
from music_assistant_models.errors import InvalidDataError

from music_assistant.controllers.storage import StorageController, StorageUsage
from music_assistant.controllers.storage import controller as controller_module
from tests.controllers.storage.conftest import MountTable, mount_line


@pytest.fixture(params=["relative", "symlink"])
async def real_data_folder(
    request: pytest.FixtureRequest,
    storage: StorageController,
    mount_table: MountTable,  # noqa: ARG001
    discoverable_tmp_path: None,  # noqa: ARG001
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Path:
    """
    Set up the storage controller with a data directory given as a relative path or a symlink.

    Returns the real data directory, inside a folder that also holds a music folder.

    :param request: Pytest request, whose param says how the data directory was given.
    :param storage: The storage controller.
    :param mount_table: The mount table the setup reads.
    :param discoverable_tmp_path: Lets discovery find mounts in the temporary folder.
    :param tmp_path: Temporary directory for the folders.
    :param monkeypatch: Pytest monkeypatch fixture.
    """
    real = tmp_path / "srv" / "ma-data"
    real.mkdir(parents=True)
    (tmp_path / "srv" / "music").mkdir()
    if request.param == "relative":
        monkeypatch.chdir(tmp_path / "srv")
        storage.mass.storage_path = "ma-data"
    else:
        link = tmp_path / "data-link"
        link.symlink_to(real, target_is_directory=True)
        storage.mass.storage_path = str(link)
    # outside a container, where a folder can be registered
    monkeypatch.setattr(controller_module, "CONTAINER_MARKER_FILES", (str(tmp_path / "none"),))
    await storage.setup(await storage.mass.config.get_core_config(storage.domain))
    return real


async def test_data_row_shows_the_real_folder(
    storage: StorageController, mount_table: MountTable, real_data_folder: Path
) -> None:
    """The data row shows the directory the server really uses."""
    mount_table.set()

    await storage.refresh()

    data = next(loc for loc in storage.get_locations() if loc.usage == StorageUsage.DATA)
    assert data.path == str(real_data_folder)


async def test_mount_on_the_data_folder_is_no_location(
    storage: StorageController, mount_table: MountTable, real_data_folder: Path
) -> None:
    """A volume mounted on the real data directory is left out, one next to it is not."""
    music = real_data_folder.parent / "music"
    mount_table.set(mount_line(real_data_folder, "ext4"), mount_line(music, "ext4"))

    await storage.refresh()

    media = [loc.path for loc in storage.get_locations() if loc.usage == StorageUsage.MEDIA]
    assert media == [str(music)]


async def test_data_folder_can_not_be_added(
    storage: StorageController, real_data_folder: Path
) -> None:
    """The real data directory can not be registered, neither directly nor through a link."""
    for path in {str(real_data_folder), storage.mass.storage_path}:
        if not Path(path).is_absolute():
            continue
        with pytest.raises(InvalidDataError) as exc_info:
            await storage.add_local_folder(path)
        assert exc_info.value.translation_key == "folder_is_server_folder"


async def test_data_folder_holds_no_music_source(
    storage: StorageController, real_data_folder: Path
) -> None:
    """Inside a registered folder the real data directory stays out, a folder next to it not."""
    srv = real_data_folder.parent
    await storage.add_local_folder(str(srv))

    assert not storage.can_hold_music_source(str(real_data_folder / "backups"), True)
    assert storage.can_hold_music_source(str(srv / "music"), True)
    with pytest.raises(InvalidDataError) as exc_info:
        await storage.list_folders(str(real_data_folder))
    assert exc_info.value.translation_key == "path_not_allowed"
    assert await storage.list_folders(str(srv)) == ["ma-data", "music"]
