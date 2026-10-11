"""Tests for the sync of a Local files source that reads a network share in its folder."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.enums import MediaType
from music_assistant_models.errors import ActionUnavailable

from music_assistant.controllers.storage import StorageController
from music_assistant.controllers.storage import controller as controller_module
from music_assistant.controllers.storage.backends import mountinfo
from music_assistant.mass import MusicAssistant
from music_assistant.providers.filesystem_local import LocalFileSystemProvider
from music_assistant.providers.filesystem_local.constants import (
    CONF_ENTRY_LIBRARY_SYNC_PLAYLISTS,
    CONF_ENTRY_LIBRARY_SYNC_TRACKS,
)
from music_assistant.providers.filesystem_local.cue import make_cue_track_id
from tests.controllers.storage.conftest import MountTable, mount_line
from tests.providers.filesystem_local.conftest import make_provider

# a file in the folder of the source itself, and the files the source found on the share
LOCAL_FILE = "Local/Artist/01 - Track.mp3"
SHARE_FILES = {"nas_music/Artist/01 - Track.mp3", make_cue_track_id("nas_music/Live/live.cue", 1)}
# files the source found before that are gone, next to the share or in a folder named like it
GONE_FILES = {"Local/Artist/02 - Gone.mp3", "nas_music_old/Artist/01 - Track.mp3"}


@pytest.fixture
def mount_table(monkeypatch: pytest.MonkeyPatch) -> MountTable:
    """
    Provide the mount table the storage controller reads, with only the root filesystem.

    :param monkeypatch: Pytest monkeypatch fixture.
    """
    table = MountTable()
    table.set(mount_line("/", "ext4"))
    monkeypatch.setattr(controller_module, "read_mountinfo", lambda: table.text)
    # the temporary folder of the tests may lie below a system path, which discovery leaves out
    monkeypatch.setattr(mountinfo, "SYSTEM_PATHS", ())
    return table


@pytest.fixture
def media(tmp_path: Path) -> Path:
    """
    Provide the folder of the source: a file of its own, and the empty folder of a share.

    :param tmp_path: Temporary directory for the folder.
    """
    media = tmp_path / "media"
    (media / "nas_music").mkdir(parents=True)
    local_file = media / LOCAL_FILE
    local_file.parent.mkdir(parents=True)
    local_file.touch()
    return media


def _source(mass: MusicAssistant, media: Path) -> LocalFileSystemProvider:
    """
    Return a music source on a folder, whose last sync found its own file and those of the share.

    :param mass: The server the source belongs to.
    :param media: The folder of the source.
    """
    provider = make_provider(mass, media)
    sync_options = {
        CONF_ENTRY_LIBRARY_SYNC_TRACKS.key: True,
        CONF_ENTRY_LIBRARY_SYNC_PLAYLISTS.key: True,
    }
    provider.config.get_value = MagicMock(  # type: ignore[method-assign]
        side_effect=lambda key, default=None: sync_options.get(key, default)
    )
    checksum = str(int((media / LOCAL_FILE).stat().st_mtime))
    mass.music = MagicMock()
    mass.music.database.get_rows_from_query = AsyncMock(
        return_value=[
            {"provider_item_id": LOCAL_FILE, "details": checksum},
            *({"provider_item_id": file, "details": "1"} for file in SHARE_FILES | GONE_FILES),
        ]
    )
    provider._process_deletions = AsyncMock()  # type: ignore[method-assign]
    provider._process_orphaned_albums_and_artists = AsyncMock()  # type: ignore[method-assign]
    return provider


async def test_files_of_a_share_that_is_down_are_kept(
    mass_minimal: MusicAssistant, storage: StorageController, mount_table: MountTable, media: Path
) -> None:
    """
    A share that dropped leaves its empty folder behind, its files stay in the library.

    The files that are gone from elsewhere in the folder of the source are removed.
    """
    nas = str(media / "nas_music")
    mount_table.mount(nas)
    await storage.refresh()
    mount_table.unmount(nas)
    provider = _source(mass_minimal, media)

    await provider.sync_library(MediaType.TRACK)

    provider._process_deletions.assert_awaited_once_with(GONE_FILES)  # type: ignore[attr-defined]


@pytest.mark.usefixtures("storage")
async def test_files_of_an_empty_share_are_removed(
    mass_minimal: MusicAssistant, mount_table: MountTable, media: Path
) -> None:
    """A share that is mounted and empty no longer holds the files."""
    mount_table.mount(str(media / "nas_music"))
    provider = _source(mass_minimal, media)

    await provider.sync_library(MediaType.TRACK)

    provider._process_deletions.assert_awaited_once_with(  # type: ignore[attr-defined]
        SHARE_FILES | GONE_FILES
    )


async def test_files_of_a_share_that_is_gone_are_removed(
    mass_minimal: MusicAssistant, storage: StorageController, mount_table: MountTable, media: Path
) -> None:
    """A share whose folder is gone too is gone for good."""
    nas = media / "nas_music"
    mount_table.mount(str(nas))
    await storage.refresh()
    mount_table.unmount(str(nas))
    nas.rmdir()
    provider = _source(mass_minimal, media)

    await provider.sync_library(MediaType.TRACK)

    provider._process_deletions.assert_awaited_once_with(  # type: ignore[attr-defined]
        SHARE_FILES | GONE_FILES
    )


@pytest.mark.usefixtures("mount_table")
async def test_nothing_is_removed_when_the_shares_are_unknown(
    mass_minimal: MusicAssistant,
    storage: StorageController,
    media: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A sync that can not tell which shares are down removes nothing."""
    monkeypatch.setattr(
        storage,
        "get_unavailable_locations",
        AsyncMock(side_effect=ActionUnavailable("The network shares could not be listed")),
    )
    provider = _source(mass_minimal, media)

    await provider.sync_library(MediaType.TRACK)

    provider._process_deletions.assert_awaited_once_with(set())  # type: ignore[attr-defined]
