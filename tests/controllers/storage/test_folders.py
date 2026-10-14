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


@pytest.fixture
def home_with_server_folders(storage: StorageController, tmp_path: Path) -> Path:
    """
    Provide a registered folder that holds the data and cache folders of the server.

    :param storage: The storage controller.
    :param tmp_path: Temporary directory, which also holds the server's folders.
    """
    data_path, cache_path = Path(storage.mass.storage_path), Path(storage.mass.cache_path)
    assert data_path.parent == cache_path.parent == tmp_path
    for folder in (data_path / "backups", tmp_path / "music" / "Albums", tmp_path / "data-old"):
        folder.mkdir(parents=True)
    (tmp_path / "link").symlink_to(data_path, target_is_directory=True)
    set_locations(
        storage,
        make_location(tmp_path, kind=StorageKind.MANUAL),
        make_location(data_path, usage=StorageUsage.DATA),
        make_location(cache_path, usage=StorageUsage.CACHE),
    )
    return tmp_path


@pytest.mark.parametrize("manages_all_sources", [True, False])
@pytest.mark.parametrize("folder", ["data", "data/backups", "cache", "link", "link/backups"])
async def test_server_folders_inside_a_location_are_not_listed(
    storage: StorageController,
    home_with_server_folders: Path,
    manages_all_sources: bool,
    folder: str,
) -> None:
    """The server's own folders stay out of a media location that holds them, also via a link."""
    with pytest.raises(InvalidDataError) as exc_info:
        await storage.list_folders(
            str(home_with_server_folders / folder), manages_all_sources=manages_all_sources
        )

    assert exc_info.value.translation_key == "path_not_allowed"


@pytest.mark.parametrize("manages_all_sources", [True, False])
async def test_folders_next_to_the_server_folders_are_listed(
    storage: StorageController, home_with_server_folders: Path, manages_all_sources: bool
) -> None:
    """A folder next to the data folder, even one with a look-alike name, is listed."""
    home = home_with_server_folders

    assert await storage.list_folders(str(home / "music"), manages_all_sources) == ["Albums"]
    assert await storage.list_folders(str(home / "data-old"), manages_all_sources) == []
    # the parent still lists the server's folders by name, it only can not be browsed into
    assert "data" in await storage.list_folders(str(home), manages_all_sources)


@pytest.mark.parametrize(
    ("path", "manages_all_sources", "expected"),
    [
        ("{home}", True, True),
        ("{home}/music/Albums", False, True),
        ("{home}/data-old", True, True),
        ("{home}/data", True, False),
        ("{home}/data/backups", False, False),
        ("{home}/cache/", True, False),
        ("{home}/../elsewhere", True, False),
        ("{home}/music\0", True, False),
        ("music", True, False),
        ("/", True, False),
    ],
)
def test_can_hold_music_source(
    storage: StorageController,
    home_with_server_folders: Path,
    path: str,
    manages_all_sources: bool,
    expected: bool,
) -> None:
    """A music source may go inside a visible media location, never in the server's folders."""
    resolved = path.format(home=home_with_server_folders)

    assert storage.can_hold_music_source(resolved, manages_all_sources) is expected


@pytest.mark.parametrize(("manages_all_sources", "expected"), [(True, True), (False, False)])
def test_can_hold_music_source_follows_visibility(
    storage: StorageController, media_root: Path, manages_all_sources: bool, expected: bool
) -> None:
    """A location a caller may not see holds no music source for it."""
    set_locations(storage, make_location(media_root, kind=StorageKind.LOCAL_DISK))

    assert (
        storage.can_hold_music_source(str(media_root / "Albums"), manages_all_sources) is expected
    )


@pytest.fixture
def nested_private_location(storage: StorageController, tmp_path: Path) -> Path:
    """
    Provide a registered folder with a discovered mount inside it, on a host without a container.

    Members see the registered folder, only admins see the mount inside it.

    :param storage: The storage controller.
    :param tmp_path: Temporary directory for the tree.
    """
    music = tmp_path / "music"
    for folder in ("private/Albums", "public", "privateer"):
        (music / folder).mkdir(parents=True)
    (music / "shortcut").symlink_to(music / "private", target_is_directory=True)
    set_locations(
        storage,
        make_location(music, kind=StorageKind.MANUAL),
        make_location(music / "private", kind=StorageKind.LOCAL_DISK),
    )
    return music


@pytest.mark.parametrize(
    ("folder", "for_admin", "for_member"),
    [
        ("", True, True),
        ("public", True, True),
        # a look-alike of the nested location is no part of it
        ("privateer", True, True),
        ("private", True, False),
        ("private/Albums", True, False),
    ],
)
def test_nested_location_goes_by_its_own_visibility(
    storage: StorageController,
    nested_private_location: Path,
    folder: str,
    for_admin: bool,
    for_member: bool,
) -> None:
    """The most specific location decides where a music source may go."""
    path = str(nested_private_location / folder)

    assert storage.can_hold_music_source(path, manages_all_sources=True) is for_admin
    assert storage.can_hold_music_source(path, manages_all_sources=False) is for_member


async def test_nested_location_is_left_out_of_a_members_listing(
    storage: StorageController, nested_private_location: Path
) -> None:
    """A member does not see, browse or reach through a link the location only admins see."""
    music = nested_private_location

    assert await storage.list_folders(str(music), manages_all_sources=True) == [
        "private",
        "privateer",
        "public",
    ]
    assert await storage.list_folders(str(music), manages_all_sources=False) == [
        "privateer",
        "public",
    ]
    assert await storage.list_folders(str(music / "private"), manages_all_sources=True) == [
        "Albums"
    ]
    for folder in ("private", "private/Albums", "shortcut", "shortcut/Albums"):
        with pytest.raises(InvalidDataError) as exc_info:
            await storage.list_folders(str(music / folder), manages_all_sources=False)
        assert exc_info.value.translation_key == "path_not_allowed"


async def test_unavailable_locations_below_a_folder(
    storage: StorageController, media_root: Path
) -> None:
    """Only the media locations strictly below the folder that can not be used are returned."""
    gone = media_root / "zebra"
    set_locations(
        storage,
        make_location(media_root, available=False),
        make_location(media_root / "Albums"),
        make_location(gone, available=False),
        make_location(media_root / "Podcasts", usage=StorageUsage.CACHE, available=False),
        make_location(media_root.parent / "outside", available=False),
    )

    assert await storage.get_unavailable_locations(str(media_root)) == [str(gone)]
