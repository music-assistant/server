"""Tests for browsing the folders of the media locations."""

from __future__ import annotations

import errno
from pathlib import Path

import pytest
from music_assistant_models.auth import User, UserRole
from music_assistant_models.errors import ActionUnavailable, InvalidDataError

from music_assistant.controllers.storage import StorageController, StorageKind, StorageUsage
from music_assistant.controllers.storage import controller as controller_module
from music_assistant.controllers.storage.constants import MAX_LISTED_FOLDERS
from music_assistant.controllers.webserver.helpers.auth_middleware import set_current_user
from tests.controllers.storage.conftest import make_location


@pytest.fixture
def media_root(tmp_path: Path) -> Path:
    """
    Provide a media location with folders, a file, a hidden folder and symlinks.

    :param tmp_path: Temporary directory for the tree.
    """
    root = tmp_path / "media"
    for name in ("zebra", "Albums", "audiobooks", "Podcasts", ".hidden", "Albums/Artist"):
        (root / name).mkdir(parents=True)
    (root / "track.mp3").write_bytes(b"")
    (tmp_path / "outside" / "secret").mkdir(parents=True)
    (tmp_path / "media-evil").mkdir()
    (root / "escape").symlink_to(tmp_path / "outside", target_is_directory=True)
    (root / "shortcut").symlink_to(root / "Albums", target_is_directory=True)
    return root


async def test_lists_subfolders_sorted(storage: StorageController, media_root: Path) -> None:
    """Only real, visible folders are listed, sorted without regard to case."""
    storage._locations = [make_location(media_root)]

    assert await storage.list_folders(str(media_root)) == [
        "Albums",
        "audiobooks",
        "Podcasts",
        "zebra",
    ]
    assert await storage.list_folders(str(media_root / "Albums")) == ["Artist"]


@pytest.mark.parametrize(
    "path",
    [
        "{root}/../outside",
        "{root}/Albums/../../outside/secret",
        "{tmp}/outside",
        "{tmp}/media-evil",
        "{root}/escape",
        "{root}/escape/secret",
        "media",
        "",
    ],
)
async def test_refuses_paths_outside_the_locations(
    storage: StorageController, media_root: Path, path: str
) -> None:
    """Traversal, look-alike prefixes, relative paths and escaping symlinks are refused."""
    storage._locations = [make_location(media_root)]

    with pytest.raises(InvalidDataError) as exc_info:
        await storage.list_folders(path.format(root=media_root, tmp=media_root.parent))

    assert exc_info.value.translation_key == "path_not_allowed"


@pytest.mark.parametrize("name", ["missing", "track.mp3"])
async def test_refuses_what_is_no_folder(
    storage: StorageController, media_root: Path, name: str
) -> None:
    """A path inside a location that is not an existing folder is reported as such."""
    storage._locations = [make_location(media_root)]

    with pytest.raises(InvalidDataError) as exc_info:
        await storage.list_folders(str(media_root / name))

    assert exc_info.value.translation_key == "folder_not_found"


async def test_unreadable_folder(
    storage: StorageController, media_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A folder that can not be read (a share whose server is gone) gets its own error."""
    storage._locations = [make_location(media_root)]

    def _host_down(path: str) -> list[str]:
        raise OSError(errno.EHOSTDOWN, "Host is down", path)

    monkeypatch.setattr(controller_module, "_list_subfolders", _host_down)

    with pytest.raises(ActionUnavailable) as exc_info:
        await storage.list_folders(str(media_root))

    assert exc_info.value.translation_key == "folder_unreadable"


async def test_listing_is_capped(storage: StorageController, tmp_path: Path) -> None:
    """A folder with very many subfolders returns the first ones only."""
    root = tmp_path / "many"
    for index in range(MAX_LISTED_FOLDERS + 100):
        (root / f"folder{index:04d}").mkdir(parents=True)
    storage._locations = [make_location(root)]

    names = await storage.list_folders(str(root))

    assert names == [f"folder{index:04d}" for index in range(MAX_LISTED_FOLDERS)]


@pytest.mark.parametrize(
    ("kind", "member_may_browse"),
    [
        (StorageKind.BUILTIN_MEDIA, True),
        (StorageKind.CONTAINER_VOLUME, True),
        (StorageKind.NETWORK_SHARE, True),
        (StorageKind.REMOVABLE, True),
        (StorageKind.LOCAL_DISK, False),
        (StorageKind.MANUAL, False),
    ],
)
async def test_member_browses_only_shared_kinds(
    storage: StorageController, media_root: Path, kind: StorageKind, member_may_browse: bool
) -> None:
    """A caller that does not manage every source only browses the kinds it may use."""
    storage._locations = [make_location(media_root, kind=kind)]

    assert await storage.list_folders(str(media_root), manages_all_sources=True)
    if member_may_browse:
        assert await storage.list_folders(str(media_root), manages_all_sources=False)
        return
    with pytest.raises(InvalidDataError):
        await storage.list_folders(str(media_root), manages_all_sources=False)


@pytest.mark.parametrize("usage", [StorageUsage.DATA, StorageUsage.CACHE])
async def test_server_directories_are_not_browsable(
    storage: StorageController, media_root: Path, usage: StorageUsage
) -> None:
    """The data and cache directories are never browsable, not even by an admin."""
    storage._locations = [make_location(media_root, usage=usage)]

    with pytest.raises(InvalidDataError):
        await storage.list_folders(str(media_root))


@pytest.mark.parametrize(
    ("role", "allowed"), [(UserRole.ADMIN, True), (UserRole.USER, False), (UserRole.SERVICE, False)]
)
async def test_command_applies_the_callers_visibility(
    storage: StorageController, media_root: Path, role: str, allowed: bool
) -> None:
    """The folders command browses with the visibility of the calling user."""
    storage._locations = [make_location(media_root, kind=StorageKind.LOCAL_DISK)]
    set_current_user(User(user_id="someone", username="someone", role=role))

    if allowed:
        assert await storage.get_folders(str(media_root)) == [
            "Albums",
            "audiobooks",
            "Podcasts",
            "zebra",
        ]
        return
    with pytest.raises(InvalidDataError):
        await storage.get_folders(str(media_root))
