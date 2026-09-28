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
from tests.controllers.storage.conftest import make_location, set_locations


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
    set_locations(storage, make_location(media_root))

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
        "{root}/Albums\0",
    ],
)
async def test_refuses_paths_outside_the_locations(
    storage: StorageController, media_root: Path, path: str
) -> None:
    """Traversal, look-alikes, relative paths, NUL bytes and escaping symlinks are refused."""
    set_locations(storage, make_location(media_root))

    with pytest.raises(InvalidDataError) as exc_info:
        await storage.list_folders(path.format(root=media_root, tmp=media_root.parent))

    assert exc_info.value.translation_key == "path_not_allowed"


@pytest.mark.parametrize("name", ["missing", "track.mp3"])
async def test_refuses_what_is_no_folder(
    storage: StorageController, media_root: Path, name: str
) -> None:
    """A path inside a location that is not an existing folder is reported as such."""
    set_locations(storage, make_location(media_root))

    with pytest.raises(InvalidDataError) as exc_info:
        await storage.list_folders(str(media_root / name))

    assert exc_info.value.translation_key == "folder_not_found"


async def test_unreadable_folder(
    storage: StorageController, media_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A folder that can not be read (no permission) gets its own error."""
    set_locations(storage, make_location(media_root))

    def _no_permission(path: str) -> list[str]:
        raise PermissionError(errno.EACCES, "Permission denied", path)

    monkeypatch.setattr(controller_module, "_list_subfolders", _no_permission)

    with pytest.raises(ActionUnavailable) as exc_info:
        await storage.list_folders(str(media_root))

    assert exc_info.value.translation_key == "folder_unreadable"


async def test_unavailable_location_is_not_touched(
    storage: StorageController, media_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A location whose probe did not answer (a share whose server is gone) is not browsed."""
    nested = media_root / "Albums"
    set_locations(storage, make_location(media_root), make_location(nested, available=False))
    touched: list[str] = []
    monkeypatch.setattr(
        controller_module, "_resolve_within", lambda path, _roots: touched.append(path)
    )

    for path in (nested, nested / "Artist"):
        with pytest.raises(ActionUnavailable) as exc_info:
            await storage.list_folders(str(path))
        assert exc_info.value.translation_key == "folder_unreadable"
    assert touched == []


async def test_listing_is_capped(storage: StorageController, tmp_path: Path) -> None:
    """A folder with very many subfolders returns the first ones only."""
    root = tmp_path / "many"
    for index in range(MAX_LISTED_FOLDERS + 100):
        (root / f"folder{index:04d}").mkdir(parents=True)
    set_locations(storage, make_location(root))

    names = await storage.list_folders(str(root))

    assert names == [f"folder{index:04d}" for index in range(MAX_LISTED_FOLDERS)]


@pytest.mark.parametrize(
    ("in_container", "kind", "managed", "member_may_browse"),
    [
        # inside a container every location was mapped in on purpose
        (True, StorageKind.CONTAINER_VOLUME, False, True),
        (True, StorageKind.REMOVABLE, False, True),
        # on a host only what Music Assistant set up itself
        (False, StorageKind.MANUAL, True, True),
        (False, StorageKind.NETWORK_SHARE, True, True),
        (False, StorageKind.NETWORK_SHARE, False, False),
        (False, StorageKind.REMOVABLE, False, False),
        (False, StorageKind.LOCAL_DISK, False, False),
    ],
)
async def test_member_browses_what_was_made_available(
    storage: StorageController,
    media_root: Path,
    in_container: bool,
    kind: StorageKind,
    managed: bool,
    member_may_browse: bool,
) -> None:
    """A caller that does not manage every source only browses the locations meant for it."""
    storage._in_container = in_container
    set_locations(storage, make_location(media_root, kind=kind, managed=managed))

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
    """The data and cache rows alone make nothing browsable, not even for an admin."""
    set_locations(storage, make_location(media_root, usage=usage))

    with pytest.raises(InvalidDataError):
        await storage.list_folders(str(media_root))


@pytest.mark.parametrize(
    ("role", "allowed"), [(UserRole.ADMIN, True), (UserRole.USER, False), (UserRole.SERVICE, False)]
)
async def test_command_applies_the_callers_visibility(
    storage: StorageController, media_root: Path, role: str, allowed: bool
) -> None:
    """The folders command browses with the visibility of the calling user."""
    set_locations(storage, make_location(media_root, kind=StorageKind.LOCAL_DISK))
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
