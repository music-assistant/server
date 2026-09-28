"""Tests for registering folders on the server as media locations."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from music_assistant_models.errors import ActionUnavailable, InvalidDataError

from music_assistant.constants import CONF_CORE, CONF_STORAGE_FOLDERS
from music_assistant.controllers.storage import StorageController, StorageKind
from music_assistant.controllers.storage import controller as controller_module
from tests.controllers.storage.conftest import mount_line


@pytest.fixture(autouse=True)
def empty_mount_table() -> Iterator[None]:
    """Keep the mount table of the machine running the tests out of the locations."""
    with patch.object(controller_module, "read_mountinfo", return_value=""):
        yield


def _source(domain: str, base_path: str, name: str = "My music") -> MagicMock:
    """
    Return a stand-in for a loaded music source reading its files from a path.

    :param domain: The provider domain of the source.
    :param base_path: The folder the source reads its files from.
    :param name: The name of the source.
    """
    source = MagicMock()
    source.domain = domain
    source.base_path = base_path
    source.name = name
    return source


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


async def test_add_refuses_a_discovered_location(
    storage: StorageController, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A mount that is a location already keeps its mount check instead of becoming a folder."""
    volume = tmp_path / "music"
    (volume / "Albums").mkdir(parents=True)
    table = mount_line(volume, "ext4")
    monkeypatch.setattr(controller_module, "read_mountinfo", lambda: table)
    await storage.refresh()

    with pytest.raises(InvalidDataError) as exc_info:
        await storage.add_local_folder(str(volume))
    # a folder inside the mount is fine
    await storage.add_local_folder(str(volume / "Albums"))

    assert exc_info.value.translation_key == "folder_already_location"
    assert storage.mass.config.get(CONF_STORAGE_FOLDERS) == [str(volume / "Albums")]


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
    storage.mass._providers["filesystem_local--abc"] = _source(
        "filesystem_local", f"{tmp_path}{subfolder}"
    )

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
    storage.mass._providers["other--abc"] = _source(
        domain, base_path.format(folder=folder, parent=tmp_path)
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
