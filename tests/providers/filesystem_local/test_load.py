"""Tests for loading a Local files source: a storage location that is away versus a missing folder."""

from __future__ import annotations

from pathlib import Path

import pytest
from music_assistant_models.errors import SetupFailedError

from music_assistant.controllers.storage import StorageController, StorageKind
from music_assistant.controllers.storage import controller as controller_module
from music_assistant.mass import MusicAssistant
from tests.controllers.storage.conftest import (
    MountTable,
    make_location,
    mount_line,
    set_locations,
)
from tests.providers.filesystem_local.conftest import make_provider


async def _load_error(mass: MusicAssistant, base_path: Path) -> SetupFailedError:
    """
    Return the error that loading a source on a folder fails with.

    :param mass: The server the source belongs to.
    :param base_path: The folder of the source.
    """
    with pytest.raises(SetupFailedError) as exc_info:
        await make_provider(mass, base_path).handle_async_init()
    return exc_info.value


async def test_an_unavailable_location_is_named(
    mass_minimal: MusicAssistant, storage: StorageController, tmp_path: Path
) -> None:
    """A source on a location that is away says so, not that its folder is missing."""
    (tmp_path / "nas" / "Music").mkdir(parents=True)
    set_locations(
        storage,
        make_location(tmp_path, kind=StorageKind.LOCAL_DISK),
        make_location(tmp_path / "nas", kind=StorageKind.NETWORK_SHARE, available=False),
    )

    error = await _load_error(mass_minimal, tmp_path / "nas" / "Music")

    assert error.translation_key == "storage_location_unavailable"
    assert error.translation_owner == "provider.filesystem_local"
    assert error.translation_args == [str(tmp_path / "nas")]


async def test_a_registered_folder_whose_drive_is_not_mounted_is_unavailable(
    mass_minimal: MusicAssistant, unmounted_folder: Path
) -> None:
    """A source in a registered folder whose drive is gone says the location is away."""
    error = await _load_error(mass_minimal, unmounted_folder / "Music")

    assert error.translation_key == "storage_location_unavailable"
    assert error.translation_args == [str(unmounted_folder)]


@pytest.mark.parametrize(
    "locations", [["{tmp}"], []], ids=["in_an_available_location", "outside_every_location"]
)
async def test_a_missing_folder_is_reported_as_missing(
    mass_minimal: MusicAssistant, storage: StorageController, tmp_path: Path, locations: list[str]
) -> None:
    """
    A folder that is not there, in an available location or none, is reported as missing.

    :param locations: The paths of the media locations.
    """
    set_locations(
        storage,
        *(make_location(path.format(tmp=tmp_path), kind=StorageKind.MANUAL) for path in locations),
    )

    error = await _load_error(mass_minimal, tmp_path / "missing")

    assert error.translation_key == "music_directory_not_found"
    assert error.translation_args == [str(tmp_path / "missing")]


async def test_the_folder_a_vanished_mount_leaves_behind_does_not_load(
    mass_minimal: MusicAssistant,
    storage: StorageController,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The empty folder an unmounted drive leaves behind is not taken for the drive."""
    usb = tmp_path / "usb"
    usb.mkdir()
    mount_table = MountTable()
    monkeypatch.setattr(controller_module, "read_mountinfo", lambda: mount_table.text)
    mount_table.set(mount_line(usb, fstype="exfat"))
    await storage.refresh()
    # the drive is unplugged: its mount and its location are gone, its folder stays
    mount_table.set()
    set_locations(storage)

    error = await _load_error(mass_minimal, usb)

    assert error.translation_key == "music_directory_not_found"


@pytest.mark.parametrize(
    "locations", [["{tmp}"], []], ids=["in_an_available_location", "outside_every_location"]
)
async def test_an_existing_folder_loads(
    mass_minimal: MusicAssistant, storage: StorageController, tmp_path: Path, locations: list[str]
) -> None:
    """
    A source on an existing folder loads, also one outside every location.

    :param locations: The paths of the media locations.
    """
    (tmp_path / "Music").mkdir()
    set_locations(
        storage,
        *(make_location(path.format(tmp=tmp_path), kind=StorageKind.MANUAL) for path in locations),
    )
    provider = make_provider(mass_minimal, tmp_path / "Music")

    await provider.handle_async_init()

    assert provider.write_access is True


@pytest.mark.parametrize("target_is_location", [False, True], ids=["outside", "other_location"])
async def test_a_folder_that_leads_out_of_its_location_does_not_load(
    mass_minimal: MusicAssistant,
    storage: StorageController,
    tmp_path: Path,
    target_is_location: bool,
) -> None:
    """
    A folder that is a symlink into another location, or outside every one, does not load.

    :param target_is_location: Whether the symlink leads into another media location.
    """
    (tmp_path / "media").mkdir()
    (tmp_path / "other").mkdir()
    folder = tmp_path / "media" / "Music"
    folder.symlink_to(tmp_path / "other", target_is_directory=True)
    locations = [make_location(tmp_path / "media", kind=StorageKind.MANUAL)]
    if target_is_location:
        locations.append(make_location(tmp_path / "other", kind=StorageKind.MANUAL))
    set_locations(storage, *locations)

    error = await _load_error(mass_minimal, folder)

    assert error.translation_key == "folder_not_allowed"
    assert error.translation_args == [str(folder)]


async def test_a_folder_that_is_a_symlink_within_its_location_loads(
    mass_minimal: MusicAssistant, storage: StorageController, tmp_path: Path
) -> None:
    """A folder that is a symlink to another folder in the same location loads."""
    (tmp_path / "media" / "Albums").mkdir(parents=True)
    folder = tmp_path / "media" / "Music"
    folder.symlink_to(tmp_path / "media" / "Albums", target_is_directory=True)
    set_locations(storage, make_location(tmp_path / "media", kind=StorageKind.MANUAL))
    provider = make_provider(mass_minimal, folder)

    await provider.handle_async_init()

    assert provider.write_access is True
