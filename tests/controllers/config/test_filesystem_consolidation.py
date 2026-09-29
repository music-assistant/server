"""
Tests for the conversion of the SMB and NFS music sources into Local files sources.

An SMB or NFS source mounted its share itself; a network share is a storage location now and a
Local files source reads a folder in one. The conversion stores the share where it is not a
storage location yet and rewrites the source in place, so it keeps its instance id and with
that its library, its access record, its options and a name of its own. It runs at every
start and only changes data: mounting the share is left to the storage controller.
"""

from __future__ import annotations

import copy
import json
import sqlite3
from contextlib import closing, contextmanager
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp.test_utils import TestServer
from cryptography.fernet import Fernet
from music_assistant_models.auth import UserRole
from music_assistant_models.config_entries import ProviderAccess, ProviderConfig
from music_assistant_models.enums import ProviderSharing

from music_assistant.constants import (
    CONF_PROVIDER_ACCESS_MIGRATED,
    CONF_PROVIDERS,
    CONF_STORAGE_SHARES,
    DB_TABLE_AUDIO_ANALYSIS,
    DB_TABLE_AUDIO_ANALYSIS_FAILURES,
    DB_TABLE_PROVIDER_MAPPINGS,
    DB_TABLE_TRACKS,
    DEFAULT_PROVIDER_CONFIG_ENTRIES,
    ENCRYPT_SUFFIX,
)
from music_assistant.controllers.config import filesystem_consolidation as consolidation_module
from music_assistant.controllers.config.filesystem_consolidation import (
    consolidate_filesystem_sources,
)
from music_assistant.controllers.music.constants import CONF_DELETED_PROVIDERS
from music_assistant.controllers.storage import StorageKind, StorageLocation, StorageUsage
from music_assistant.controllers.storage import controller as storage_controller_module
from music_assistant.controllers.storage.backends import local_mount as local_mount_module
from music_assistant.controllers.storage.backends import mountinfo as mountinfo_module
from music_assistant.controllers.storage.backends import supervisor as supervisor_module
from music_assistant.controllers.storage.backends.base import BackendUnavailable
from music_assistant.controllers.storage.backends.local_mount import MOUNT_ROOT
from music_assistant.controllers.storage.models import MountBackend, NetworkShareSpec, ShareType
from music_assistant.helpers import hassio
from music_assistant.helpers.json import json_dumps
from music_assistant.helpers.provider_access import visible_music_sources
from music_assistant.providers.filesystem_local import LocalFileSystemProvider
from tests.conftest import full_mass_context
from tests.controllers.storage.conftest import (
    SUPERVISOR_TOKEN,
    FakeSupervisor,
    MountTable,
    mount_line,
    wait_until,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Iterator
    from pathlib import Path

    from music_assistant.mass import MusicAssistant

SMB_ID = "filesystem_smb--Kitchen1"
SMB_ID_2 = "filesystem_smb--Study002"
NFS_ID = "filesystem_nfs--Attic003"
NFS_ID_2 = "filesystem_nfs--Cellar06"
LOCAL_ID = "filesystem_local--Plain004"
SPOTIFY_ID = "spotify--Account5"
SMB_SETUP: dict[str, Any] = {
    "content_type": "music",
    "host": "nas.local",
    "share": "Music",
    "username": "marcel",
    "password": "p@ss,word",
    "subfolder": "",
    "smb_version": "3.0",
}
NFS_SETUP: dict[str, Any] = {
    "content_type": "audiobooks",
    "host": "192.168.1.20",
    "export_path": "/volume1/books",
    "subfolder": "",
    "nfs_version": "4.1",
}
VALUES: dict[str, Any] = {
    "log_level": "DEBUG",
    "missing_album_artist_action": "folder_name",
    "library_sync_playlists": False,
}
ACCESS = ProviderAccess(owner="user-1", sharing=ProviderSharing.SELECTED, shared_users=["user-2"])


class _Killed(BaseException):
    """The server process being killed: nothing after it runs, nothing catches it."""


@pytest.fixture
def reconcile(mass: MusicAssistant, monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """
    Stand in for the mounting of the storage controller.

    :param mass: The started server.
    :param monkeypatch: Pytest monkeypatch fixture.
    """
    mock = AsyncMock()
    monkeypatch.setattr(mass.storage, "reconcile", mock)
    return mock


@pytest.fixture
async def supervisor(
    mass: MusicAssistant, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> AsyncGenerator[FakeSupervisor]:
    """
    Run the server under a fake Supervisor that lets it manage its mounts.

    :param mass: The started server.
    :param monkeypatch: Pytest monkeypatch fixture.
    :param tmp_path: Temporary directory for the media folder.
    """
    media = tmp_path / "media"
    media.mkdir()
    fake = FakeSupervisor(MountTable(), media)
    server = TestServer(fake.app)
    await server.start_server()
    monkeypatch.setattr(mass, "running_as_hass_addon", True)
    monkeypatch.setattr(hassio, "SUPERVISOR_URL", str(server.make_url("")).rstrip("/"))
    monkeypatch.setenv("SUPERVISOR_TOKEN", SUPERVISOR_TOKEN)
    monkeypatch.setattr(supervisor_module, "SUPERVISOR_MEDIA_PATH", str(media))
    try:
        yield fake
    finally:
        await server.close()


def _store_source(
    mass: MusicAssistant,
    instance_id: str,
    setup: dict[str, Any],
    *,
    name: str | None = None,
    enabled: bool = True,
    values: dict[str, Any] | None = None,
    access: ProviderAccess | None = ACCESS,
) -> None:
    """
    Store a music source the way an install that is about to be converted holds it.

    :param mass: The started server.
    :param instance_id: The instance id of the source, starting with its domain.
    :param setup: The setup values of the source, stored encrypted.
    :param name: The name the user gave the source.
    :param enabled: Whether the source is enabled.
    :param values: The option values of the source.
    :param access: The access record of the source, None for an install from before them.
    """
    mass.config.set(
        f"{CONF_PROVIDERS}/{instance_id}",
        {
            "type": "music",
            "domain": instance_id.split("--", maxsplit=1)[0],
            "instance_id": instance_id,
            "enabled": enabled,
            "name": name,
            "default_name": "Filesystem (remote share) [Music]",
            "values": copy.deepcopy(VALUES if values is None else values),
            "setup_data": {
                key: mass.config.encrypt_string(value) if isinstance(value, str) else value
                for key, value in setup.items()
            },
            "access": access.to_dict() if access is not None else None,
            "last_error": {"error_code": 999, "message": "Unable to mount the share"},
        },
    )


def _config(mass: MusicAssistant, instance_id: str) -> dict[str, Any]:
    """Return the raw config of a source."""
    raw_conf: dict[str, Any] = mass.config.get(f"{CONF_PROVIDERS}/{instance_id}")
    return raw_conf


def _setup_values(mass: MusicAssistant, instance_id: str) -> dict[str, Any]:
    """Return the setup values of a source, decrypted."""
    return {
        key: mass.config.decrypt_string(value)
        for key, value in _config(mass, instance_id)["setup_data"].items()
    }


def _path(mass: MusicAssistant, instance_id: str) -> str:
    """Return the folder a converted source reads."""
    return str(_setup_values(mass, instance_id)["path"])


def _default_name_on_load(mass: MusicAssistant, instance_id: str) -> str:
    """Return the default name the Local files provider gives a converted source on its load."""
    config = cast(
        "ProviderConfig",
        ProviderConfig.parse(DEFAULT_PROVIDER_CONFIG_ENTRIES, _config(mass, instance_id)),
    )
    manifest = mass.get_provider_manifest("filesystem_local")
    return LocalFileSystemProvider(mass, manifest, config).default_name


def _records(mass: MusicAssistant) -> dict[str, NetworkShareSpec]:
    """Return the stored network shares by name."""
    return {
        name: NetworkShareSpec.from_dict(record)
        for name, record in mass.config.get(CONF_STORAGE_SHARES, {}).items()
    }


async def _store_mappings(mass: MusicAssistant, *rows: tuple[str, str, str]) -> None:
    """
    Store library rows of music sources.

    :param mass: The started server.
    :param rows: The (media type, provider instance, provider item id) of each row.
    """
    for index, (media_type, instance_id, item_id) in enumerate(rows):
        await mass.music.database.insert(
            DB_TABLE_PROVIDER_MAPPINGS,
            {
                "media_type": media_type,
                "item_id": index + 1,
                "provider_domain": instance_id.split("--")[0],
                "provider_instance": instance_id,
                "provider_item_id": item_id,
                "available": True,
                "in_library": True,
            },
        )


async def _mappings(mass: MusicAssistant) -> list[tuple[str, str, str, str]]:
    """Return every library row as (provider instance, domain, media type, item id)."""
    rows = await mass.music.database.get_rows(DB_TABLE_PROVIDER_MAPPINGS, limit=0)
    return sorted(
        (
            str(row["provider_instance"]),
            str(row["provider_domain"]),
            str(row["media_type"]),
            str(row["provider_item_id"]),
        )
        for row in rows
    )


def _credentials_warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    """Return the logged warnings about sources on one share with different credentials."""
    return [
        record.getMessage()
        for record in caplog.records
        if "different credentials" in record.getMessage()
    ]


def _leaving_warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    """Return the logged warnings about sources left as they are."""
    return [
        record.getMessage()
        for record in caplog.records
        if record.levelname == "WARNING" and record.getMessage().startswith("Leaving music source")
    ]


def _consolidation_records(caplog: pytest.LogCaptureFixture) -> list[str]:
    """Return every message the conversion logged."""
    return [
        record.getMessage()
        for record in caplog.records
        if record.name == consolidation_module.LOGGER.name
    ]


def _watch_writes(mass: MusicAssistant, monkeypatch: pytest.MonkeyPatch) -> list[MagicMock]:
    """
    Watch every way to write the settings or the library, and return the watchers.

    :param mass: The started server.
    :param monkeypatch: Pytest monkeypatch fixture.
    """
    watchers: list[MagicMock] = []
    for target, name, mock_type in (
        (mass.config, "set", MagicMock),
        (mass.config, "remove", MagicMock),
        (mass.config, "save", MagicMock),
        (mass.config, "async_save", AsyncMock),
        (mass.music.database, "execute_write", AsyncMock),
        (mass.music.database, "insert", AsyncMock),
        (mass.music.database, "update", AsyncMock),
    ):
        watcher = mock_type(wraps=getattr(target, name))
        monkeypatch.setattr(target, name, watcher)
        watchers.append(watcher)
    return watchers


def _location_for_the_owner(mass: MusicAssistant, instance_id: str) -> StorageLocation:
    """
    Return the storage location a converted source reads, asserting its owner may use it.

    The owner in ACCESS is a member, who does not manage every music source; an admin manages
    a source without an owner.

    :param mass: The started server.
    :param instance_id: The instance id of the converted source.
    """
    path = _path(mass, instance_id)
    assert mass.storage.can_hold_music_source(path, manages_all_sources=False)
    assert mass.storage.can_hold_music_source(path, manages_all_sources=True)
    location = mass.storage.get_location_for_path(path)
    assert location is not None
    assert location.usage == StorageUsage.MEDIA
    return location


async def _store_tracks(mass: MusicAssistant, *rows: tuple[str, str]) -> None:
    """
    Store library tracks, each held by one music source.

    :param mass: The started server.
    :param rows: The (provider instance, provider item id) of each track.
    """
    for instance_id, item_id in rows:
        name = item_id.rsplit("/", 1)[-1]
        db_id = await mass.music.database.insert(
            DB_TABLE_TRACKS,
            {
                "name": name,
                "sort_name": name,
                "metadata": "{}",
                "search_name": name,
                "search_sort_name": name,
                "duration": 180,
            },
        )
        await mass.music.database.insert(
            DB_TABLE_PROVIDER_MAPPINGS,
            {
                "media_type": "track",
                "item_id": db_id,
                "provider_domain": instance_id.split("--")[0],
                "provider_instance": instance_id,
                "provider_item_id": item_id,
                "available": True,
                "in_library": True,
            },
        )


async def _prepare_install_to_convert(tmp_path: Path) -> None:
    """
    Leave an install with an SMB source to convert and a Local files source, both with tracks.

    :param tmp_path: The directory the server keeps its data in.
    """
    folder = tmp_path / "music"
    folder.mkdir()
    with _without_mount_backends():
        async with full_mass_context(tmp_path) as mass:
            _store_source(mass, SMB_ID, {**SMB_SETUP, "subfolder": "albums"})
            mass.config.set(
                f"{CONF_PROVIDERS}/{LOCAL_ID}",
                {
                    "type": "music",
                    "domain": "filesystem_local",
                    "instance_id": LOCAL_ID,
                    "enabled": True,
                    "values": {},
                    "setup_data": {"path": mass.config.encrypt_string(str(folder))},
                },
            )
            await _store_tracks(
                mass,
                (SMB_ID, "Artist/Album/01.flac"),
                (SMB_ID, "Artist/Album/02.flac"),
                (LOCAL_ID, "Other/Album/01.flac"),
                (SPOTIFY_ID, "spotify-track"),
            )


async def _prepare_install_with_a_restricted_user(tmp_path: Path) -> str:
    """
    Leave an install from before the access records: an SMB source and a user restricted to it.

    Returns the user id of the restricted user.

    :param tmp_path: The directory the server keeps its data in.
    """
    with _without_mount_backends():
        async with full_mass_context(tmp_path) as mass:
            user = await mass.webserver.auth.create_user(username="alice", role=UserRole.USER)
            await mass.webserver.auth.database.update(
                "users", {"user_id": user.user_id}, {"provider_filter": json_dumps([SMB_ID])}
            )
            _store_source(mass, SMB_ID, SMB_SETUP, access=None)
            # the boot already made the access records
            mass.config.remove(CONF_PROVIDER_ACCESS_MIGRATED)
    return user.user_id


async def _run_library_maintenance(mass: MusicAssistant) -> None:
    """
    Run what reads or tidies the library on its own: background analysis and the cleanups.

    Asserts that the SMB source is among the analysis candidates, as its rows carry the Local
    files domain, and that it got neither an analysis nor a failure for them.

    :param mass: The started server, with a Local files source loaded.
    """
    assert mass.get_provider(LOCAL_ID) is not None
    analysis = mass.streams.audio_analysis
    versions = {provider.domain: provider.analysis_version for provider in analysis.providers}
    candidates = await analysis._find_candidates_missing_analysis(versions, limit=0)
    assert SMB_ID in {candidate["provider_instance"] for candidate in candidates}
    await analysis._run_background_scan()
    await mass.music.correct_multi_instance_provider_mappings()
    await mass.music._cleanup_database()
    for table in (DB_TABLE_AUDIO_ANALYSIS, DB_TABLE_AUDIO_ANALYSIS_FAILURES):
        assert await mass.music.database.get_rows(table, {"provider": SMB_ID}) == []


def _library_on_disk(storage_path: Path) -> tuple[list[tuple[str, str, str, str]], int]:
    """
    Return the library rows as a stopped server left them, and the number of tracks.

    :param storage_path: The data directory of the server.
    """
    with closing(sqlite3.connect(storage_path / "library.db")) as db:
        rows = db.execute(
            "SELECT provider_instance, provider_domain, media_type, provider_item_id "
            f"FROM {DB_TABLE_PROVIDER_MAPPINGS}"
        ).fetchall()
        (tracks,) = db.execute(f"SELECT count(*) FROM {DB_TABLE_TRACKS}").fetchone()
    mappings = sorted(
        (str(instance_id), str(domain), str(media_type), str(item_id))
        for instance_id, domain, media_type, item_id in rows
    )
    return mappings, int(tracks)


def _deleted_providers(mass: MusicAssistant) -> list[str]:
    """Return the removed providers whose library is still to be cleaned up."""
    return list(mass.config.get_raw_core_config_value("music", CONF_DELETED_PROVIDERS, []))


@contextmanager
def _without_mount_backends() -> Iterator[None]:
    """Keep a server booted in a test from mounting anything: it finds no mount backend."""
    unavailable = AsyncMock(side_effect=BackendUnavailable("not in this test"))
    with (
        patch.object(storage_controller_module, "create_local_mounter", unavailable),
        patch.object(storage_controller_module, "create_supervisor_mounter", unavailable),
    ):
        yield


async def test_smb_and_nfs_sources_become_local_files_sources(
    mass: MusicAssistant, reconcile: AsyncMock
) -> None:
    """Each source becomes a Local files source on its share, with its library and settings."""
    _store_source(mass, SMB_ID, SMB_SETUP, values={**VALUES, "cache_mode": "strict"})
    _store_source(mass, NFS_ID, NFS_SETUP, name="Audiobooks on the NAS")
    mass.config.set(
        f"{CONF_PROVIDERS}/{LOCAL_ID}",
        {
            "type": "music",
            "domain": "filesystem_local",
            "instance_id": LOCAL_ID,
            "values": {},
            "setup_data": {"path": mass.config.encrypt_string("/media")},
        },
    )
    local_before = copy.deepcopy(_config(mass, LOCAL_ID))
    await _store_mappings(
        mass,
        ("track", SMB_ID, "Artist/Album/01.flac"),
        ("album", SMB_ID, "Artist/Album"),
        ("audiobook", NFS_ID, "Book/01.mp3"),
        ("track", LOCAL_ID, "local.flac"),
        ("track", SPOTIFY_ID, "spotify-track"),
    )
    deleted_before = _deleted_providers(mass)

    await consolidate_filesystem_sources(mass)

    smb = _config(mass, SMB_ID)
    assert smb["domain"] == "filesystem_local"
    assert smb["instance_id"] == SMB_ID
    assert smb["enabled"] is True
    assert smb["access"] == ACCESS.to_dict()
    assert smb["last_error"] is None
    # without a name of its own it shows the default name of Local files, as one of three
    assert smb["name"] is None
    assert smb["default_name"] == "Local files [music]"
    # the options Local files has too are kept, the cache mode of the SMB mount goes
    assert smb["values"] == VALUES
    assert set(smb["setup_data"]) == {"content_type", "path"}
    assert all(value.startswith(ENCRYPT_SUFFIX) for value in smb["setup_data"].values())
    assert _setup_values(mass, SMB_ID) == {"content_type": "music", "path": f"{MOUNT_ROOT}/music"}
    nfs = _config(mass, NFS_ID)
    assert nfs["domain"] == "filesystem_local"
    assert nfs["name"] == "Audiobooks on the NAS"
    assert nfs["default_name"] == "Local files [books]"
    assert nfs["access"] == ACCESS.to_dict()
    assert _setup_values(mass, NFS_ID) == {
        "content_type": "audiobooks",
        "path": f"{MOUNT_ROOT}/books",
    }
    assert _config(mass, LOCAL_ID) == local_before
    # without a Supervisor the server mounts the shares itself
    records = _records(mass)
    assert set(records) == {"music", "books"}
    music = records["music"]
    assert (music.share_type, music.server, music.share) == (ShareType.CIFS, "nas.local", "Music")
    assert (music.backend, music.path) == (MountBackend.LOCAL_MOUNT, f"{MOUNT_ROOT}/music")
    assert music.username == "marcel"
    assert music.password is not None
    assert music.password.startswith(ENCRYPT_SUFFIX)
    assert mass.config.decrypt_string(music.password) == "p@ss,word"
    assert music.version == "3.0"
    books = records["books"]
    assert (books.share_type, books.server, books.share) == (
        ShareType.NFS,
        "192.168.1.20",
        "/volume1/books",
    )
    assert (books.username, books.password, books.version) == (None, None, "4.1")
    assert await _mappings(mass) == [
        (LOCAL_ID, "filesystem_local", "track", "local.flac"),
        (NFS_ID, "filesystem_local", "audiobook", "Book/01.mp3"),
        (SMB_ID, "filesystem_local", "album", "Artist/Album"),
        (SMB_ID, "filesystem_local", "track", "Artist/Album/01.flac"),
        (SPOTIFY_ID, "spotify", "track", "spotify-track"),
    ]
    assert _deleted_providers(mass) == deleted_before
    reconcile.assert_awaited_once()


@pytest.mark.usefixtures("reconcile")
async def test_sources_without_a_name_of_their_own_show_the_default_name_of_local_files(
    mass: MusicAssistant,
) -> None:
    """
    Sources converted in one start hold no name and the default name Local files gives them.

    Local files names each after the folder it reads, also a source on the root of an export,
    which the NFS provider numbered: its share is mounted on a folder of its own.
    """
    _store_source(mass, SMB_ID, {**SMB_SETUP, "subfolder": "albums\\A-K"})
    _store_source(mass, SMB_ID_2, SMB_SETUP, name="")
    _store_source(mass, NFS_ID, NFS_SETUP)
    _store_source(mass, NFS_ID_2, {**NFS_SETUP, "host": "192.168.1.21", "export_path": "/"})
    instance_ids = (SMB_ID, SMB_ID_2, NFS_ID, NFS_ID_2)

    await consolidate_filesystem_sources(mass)

    assert {
        instance_id: (
            _config(mass, instance_id)["name"],
            _config(mass, instance_id)["default_name"],
        )
        for instance_id in instance_ids
    } == {
        SMB_ID: (None, "Local files [A-K]"),
        SMB_ID_2: (None, "Local files [music]"),
        NFS_ID: (None, "Local files [books]"),
        NFS_ID_2: (None, "Local files [share]"),
    }
    for instance_id in instance_ids:
        assert _config(mass, instance_id)["default_name"] == _default_name_on_load(
            mass, instance_id
        )


@pytest.mark.usefixtures("reconcile")
async def test_a_source_with_a_name_of_its_own_keeps_it(mass: MusicAssistant) -> None:
    """A name the user gave a source stays exactly as it was."""
    _store_source(mass, SMB_ID, SMB_SETUP, name="Music on the NAS")
    _store_source(mass, NFS_ID, NFS_SETUP)

    await consolidate_filesystem_sources(mass)

    assert _config(mass, SMB_ID)["name"] == "Music on the NAS"
    assert _config(mass, SMB_ID)["default_name"] == "Local files [music]"


@pytest.mark.usefixtures("reconcile")
async def test_a_source_left_as_it_is_does_not_count_for_the_default_names(
    mass: MusicAssistant,
) -> None:
    """A source that is not converted is no Local files source, so the only one has no postfix."""
    _store_source(mass, SMB_ID, SMB_SETUP)
    _store_source(mass, SMB_ID_2, {**SMB_SETUP, "share": "music/albums"})

    await consolidate_filesystem_sources(mass)

    assert _config(mass, SMB_ID)["default_name"] == "Local files"
    assert _default_name_on_load(mass, SMB_ID) == "Local files"
    assert _config(mass, SMB_ID_2)["domain"] == "filesystem_smb"


@pytest.mark.parametrize(
    ("subfolder", "tail"),
    [
        ("albums/A-K", "/albums/A-K"),
        ("/albums/", "/albums"),
        ("albums\\A-K", "/albums/A-K"),
        ("\\albums\\", "/albums"),
        # a name that starts with two dots is a plain folder name
        ("..music", "/..music"),
        ("..music\\A-K", "/..music/A-K"),
        ("", ""),
    ],
)
@pytest.mark.usefixtures("reconcile")
async def test_smb_subfolder_becomes_the_end_of_the_path(
    mass: MusicAssistant, subfolder: str, tail: str
) -> None:
    """The subfolder the SMB source mounted is a folder in the share now, the share is whole."""
    _store_source(mass, SMB_ID, {**SMB_SETUP, "subfolder": subfolder})

    await consolidate_filesystem_sources(mass)

    assert _path(mass, SMB_ID) == f"{MOUNT_ROOT}/music{tail}"
    assert _records(mass)["music"].share == "Music"


@pytest.mark.parametrize(
    ("subfolder", "tail"),
    [
        ("albums/A-K", "/albums/A-K"),
        ("/albums", "/albums"),
        (" albums/ ", "/albums"),
        ("..music", "/..music"),
        ("", ""),
    ],
)
@pytest.mark.usefixtures("reconcile")
async def test_nfs_subfolder_becomes_the_end_of_the_path(
    mass: MusicAssistant, subfolder: str, tail: str
) -> None:
    """The subfolder an NFS source read inside its mount is the end of its path."""
    _store_source(
        mass, NFS_ID, {**NFS_SETUP, "export_path": "/volume1/books/", "subfolder": subfolder}
    )

    await consolidate_filesystem_sources(mass)

    assert _path(mass, NFS_ID) == f"{MOUNT_ROOT}/books{tail}"
    assert _records(mass)["books"].share == "/volume1/books"


@pytest.mark.usefixtures("reconcile")
async def test_two_sources_on_one_share_share_one_location(mass: MusicAssistant) -> None:
    """Music and audiobooks in two folders of one share end up on one network share."""
    _store_source(mass, SMB_ID, {**SMB_SETUP, "subfolder": "music"})
    _store_source(
        mass,
        SMB_ID_2,
        {
            **SMB_SETUP,
            "content_type": "audiobooks",
            "host": "NAS.local",
            "share": "music",
            "subfolder": "audiobooks",
        },
    )

    await consolidate_filesystem_sources(mass)

    assert list(_records(mass)) == ["music"]
    assert _path(mass, SMB_ID) == f"{MOUNT_ROOT}/music/music"
    assert _path(mass, SMB_ID_2) == f"{MOUNT_ROOT}/music/audiobooks"


async def test_a_stored_network_share_is_used(mass: MusicAssistant, reconcile: AsyncMock) -> None:
    """A share that is a network share already is not stored a second time."""
    stored = NetworkShareSpec(
        name="nas_music",
        share_type=ShareType.CIFS,
        server="nas.local",
        share="music",
        backend=MountBackend.LOCAL_MOUNT,
        path=f"{MOUNT_ROOT}/nas_music",
    )
    mass.config.set(f"{CONF_STORAGE_SHARES}/nas_music", stored.to_dict())
    _store_source(mass, SMB_ID, {**SMB_SETUP, "subfolder": "albums"})

    await consolidate_filesystem_sources(mass)

    assert _records(mass) == {"nas_music": stored}
    assert _path(mass, SMB_ID) == f"{MOUNT_ROOT}/nas_music/albums"
    reconcile.assert_not_awaited()


@pytest.mark.usefixtures("reconcile")
async def test_a_new_share_leaves_stored_shares_alone(mass: MusicAssistant) -> None:
    """A new share with the name of a stored share gets a name of its own."""
    stored = NetworkShareSpec(
        name="music",
        share_type=ShareType.CIFS,
        server="other.local",
        share="music",
        backend=MountBackend.LOCAL_MOUNT,
        path=f"{MOUNT_ROOT}/music",
    )
    mass.config.set(f"{CONF_STORAGE_SHARES}/music", stored.to_dict())
    # a record that can not be read keeps its name as well
    mass.config.set(f"{CONF_STORAGE_SHARES}/music_2", {"name": "music_2"})
    _store_source(mass, SMB_ID, SMB_SETUP)

    await consolidate_filesystem_sources(mass)

    assert mass.config.get(f"{CONF_STORAGE_SHARES}/music") == stored.to_dict()
    assert mass.config.get(f"{CONF_STORAGE_SHARES}/music_2") == {"name": "music_2"}
    assert mass.config.get(f"{CONF_STORAGE_SHARES}/music_3")["server"] == "nas.local"
    assert _path(mass, SMB_ID) == f"{MOUNT_ROOT}/music_3"


@pytest.mark.usefixtures("reconcile")
async def test_a_user_without_a_password_mounts_as_guest(mass: MusicAssistant) -> None:
    """The mount backends take a user only together with a password."""
    _store_source(mass, SMB_ID, {**SMB_SETUP, "password": ""})
    _store_source(mass, SMB_ID_2, {**SMB_SETUP, "share": "books", "username": "Guest"})

    await consolidate_filesystem_sources(mass)

    records = _records(mass)
    assert (records["music"].username, records["music"].password) == (None, None)
    assert (records["books"].username, records["books"].password) == (None, None)


@pytest.mark.usefixtures("reconcile")
async def test_a_share_of_two_sources_takes_the_credentials_over_guest(
    mass: MusicAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """A share that a guest source and a source with credentials read is mounted with those."""
    _store_source(mass, SMB_ID, {**SMB_SETUP, "username": "guest", "subfolder": "music"})
    _store_source(mass, SMB_ID_2, {**SMB_SETUP, "subfolder": "books"})

    await consolidate_filesystem_sources(mass)

    record = _records(mass)["music"]
    assert record.username == "marcel"
    assert record.password is not None
    assert mass.config.decrypt_string(record.password) == "p@ss,word"
    assert _path(mass, SMB_ID) == f"{MOUNT_ROOT}/music/music"
    assert _path(mass, SMB_ID_2) == f"{MOUNT_ROOT}/music/books"
    assert _credentials_warnings(caplog) == [
        f"Music sources {SMB_ID} and {SMB_ID_2} read the same network share with different "
        f"credentials; it is mounted with those of {SMB_ID_2}"
    ]
    assert "p@ss,word" not in caplog.text


@pytest.mark.usefixtures("reconcile")
async def test_a_share_of_two_sources_keeps_the_first_credentials(
    mass: MusicAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """Two sources on one share with different credentials: the share keeps those it got first."""
    _store_source(mass, SMB_ID, {**SMB_SETUP, "subfolder": "music"})
    _store_source(
        mass, SMB_ID_2, {**SMB_SETUP, "username": "anna", "password": "an0ther", "subfolder": "x"}
    )

    await consolidate_filesystem_sources(mass)

    record = _records(mass)["music"]
    assert record.username == "marcel"
    assert record.password is not None
    assert mass.config.decrypt_string(record.password) == "p@ss,word"
    assert _credentials_warnings(caplog) == [
        f"Music sources {SMB_ID} and {SMB_ID_2} read the same network share with different "
        f"credentials; it is mounted with those of {SMB_ID}"
    ]
    assert "an0ther" not in caplog.text
    assert "p@ss,word" not in caplog.text
    assert "anna" not in _credentials_warnings(caplog)[0]


@pytest.mark.usefixtures("reconcile")
async def test_two_sources_with_the_same_credentials_log_nothing(
    mass: MusicAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """Two sources on one share with the same credentials share them without a warning."""
    _store_source(mass, SMB_ID, {**SMB_SETUP, "subfolder": "music"})
    _store_source(mass, SMB_ID_2, {**SMB_SETUP, "subfolder": "books"})

    await consolidate_filesystem_sources(mass)

    assert _records(mass)["music"].username == "marcel"
    assert _credentials_warnings(caplog) == []


async def test_under_a_supervisor_its_mount_of_the_share_is_used(
    mass: MusicAssistant, reconcile: AsyncMock, supervisor: FakeSupervisor
) -> None:
    """A share added in Home Assistant is a storage location as it is, and stays the user's."""
    supervisor.add_mount("nas_music", type="cifs", server="NAS.local", share="music")
    supervisor.add_mount("books", type="nfs", server="192.168.1.20", path="/volume1/books")
    _store_source(mass, SMB_ID, {**SMB_SETUP, "subfolder": "albums"})
    _store_source(mass, NFS_ID, NFS_SETUP)

    await consolidate_filesystem_sources(mass)

    assert _path(mass, SMB_ID) == f"{supervisor.path('nas_music')}/albums"
    assert _path(mass, NFS_ID) == supervisor.path("books")
    assert _records(mass) == {}
    assert [request[0] for request in supervisor.requests] == ["GET"] * len(supervisor.requests)
    assert _config(mass, NFS_ID)["domain"] == "filesystem_local"
    reconcile.assert_not_awaited()


async def test_under_a_supervisor_a_new_share_gets_a_free_name(
    mass: MusicAssistant, reconcile: AsyncMock, supervisor: FakeSupervisor
) -> None:
    """A new share is named apart from the Supervisor's mounts and the media folder."""
    supervisor.add_mount("music", type="cifs", server="other.local", share="music")
    (supervisor.media / "music_2").mkdir()
    _store_source(mass, SMB_ID, {**SMB_SETUP, "subfolder": "albums"})

    await consolidate_filesystem_sources(mass)

    records = _records(mass)
    assert list(records) == ["music_3"]
    assert records["music_3"].backend == MountBackend.SUPERVISOR
    assert records["music_3"].path == supervisor.path("music_3")
    assert _path(mass, SMB_ID) == f"{supervisor.path('music_3')}/albums"
    # nothing is mounted yet: the storage controller does that
    assert [request[0] for request in supervisor.requests] == ["GET"] * len(supervisor.requests)
    assert "music_3" not in supervisor.mounts
    reconcile.assert_awaited_once()


@pytest.mark.usefixtures("reconcile")
async def test_the_owner_may_use_the_network_share_the_server_mounts(mass: MusicAssistant) -> None:
    """A converted source reads a folder of the network share stored for it, which it may use."""
    _store_source(mass, SMB_ID, {**SMB_SETUP, "subfolder": "albums"})

    await consolidate_filesystem_sources(mass)

    location = _location_for_the_owner(mass, SMB_ID)
    assert (location.path, location.share_name, location.managed) == (
        f"{MOUNT_ROOT}/music",
        "music",
        True,
    )
    assert location.backend == MountBackend.LOCAL_MOUNT


@pytest.mark.usefixtures("reconcile")
async def test_the_owner_may_use_the_network_share_the_supervisor_mounts(
    mass: MusicAssistant, supervisor: FakeSupervisor
) -> None:
    """Under a Supervisor the stored network share is the location of the converted source."""
    _store_source(mass, SMB_ID, {**SMB_SETUP, "subfolder": "albums"})

    await consolidate_filesystem_sources(mass)

    location = _location_for_the_owner(mass, SMB_ID)
    assert (location.path, location.share_name, location.managed) == (
        supervisor.path("music"),
        "music",
        True,
    )
    assert location.backend == MountBackend.SUPERVISOR


@pytest.mark.usefixtures("reconcile")
async def test_the_owner_may_use_the_mount_home_assistant_has(
    mass: MusicAssistant, supervisor: FakeSupervisor, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A source on a share added in Home Assistant reads the location of that mount."""
    supervisor.add_mount("nas_music", type="cifs", server="nas.local", share="Music")
    # the app sees the mounts of the Supervisor in its own mount table, and runs in a container;
    # the temporary folder of the test may lie below /tmp, which discovery leaves out
    monkeypatch.setattr(
        storage_controller_module, "read_mountinfo", lambda: supervisor.mount_table.text
    )
    monkeypatch.setattr(
        mountinfo_module,
        "SYSTEM_PATHS",
        tuple(path for path in mountinfo_module.SYSTEM_PATHS if path != "/tmp"),  # noqa: S108
    )
    monkeypatch.setattr(mass.storage, "_in_container", True)
    # the mount is there when the server starts
    await mass.storage.refresh()
    _store_source(mass, SMB_ID, {**SMB_SETUP, "subfolder": "albums"})

    await consolidate_filesystem_sources(mass)

    location = _location_for_the_owner(mass, SMB_ID)
    assert (location.path, location.kind, location.managed) == (
        supervisor.path("nas_music"),
        StorageKind.NETWORK_SHARE,
        False,
    )
    assert _records(mass) == {}


@pytest.mark.usefixtures("reconcile", "supervisor")
async def test_under_a_supervisor_only_versions_it_can_pin_are_kept(mass: MusicAssistant) -> None:
    """The Supervisor pins SMB 1.0 and 2.0 only and no NFS version; it negotiates the others."""
    _store_source(mass, SMB_ID, SMB_SETUP)
    _store_source(mass, SMB_ID_2, {**SMB_SETUP, "share": "books", "smb_version": "2.0"})
    _store_source(mass, NFS_ID, NFS_SETUP)

    await consolidate_filesystem_sources(mass)

    records = _records(mass)
    assert records["music"].version is None
    assert records["books"].version == "2.0"
    assert records["books_2"].version is None
    assert records["books_2"].share_type == ShareType.NFS


async def test_the_share_of_a_converted_source_mounts_without_a_known_backend(
    mass: MusicAssistant, supervisor: FakeSupervisor, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    The share stored for a converted source is mounted by the reconcile the conversion starts.

    Also when the storage controller found no mount backend before, e.g. because the Supervisor
    did not answer yet at its start: the reconcile looks for the backends again.
    """
    supervisor.mount_table.set(mount_line("/", "ext4"))
    monkeypatch.setattr(
        storage_controller_module, "read_mountinfo", lambda: supervisor.mount_table.text
    )
    monkeypatch.setattr(mass.storage, "_mounters", {})
    _store_source(mass, SMB_ID, SMB_SETUP)

    await consolidate_filesystem_sources(mass)

    def _available() -> bool:
        location = mass.storage.get_location_for_path(_path(mass, SMB_ID))
        return location is not None and location.available

    await wait_until(_available)
    assert _records(mass)["music"].backend == MountBackend.SUPERVISOR
    assert MountBackend.SUPERVISOR in mass.storage._mounters
    assert ("POST", "/mounts") in [request[:2] for request in supervisor.requests]
    assert (supervisor.mounts["music"]["server"], supervisor.mounts["music"]["share"]) == (
        "nas.local",
        "Music",
    )


@pytest.mark.parametrize("problem", ["no_manager_role", "gone", "slow"])
async def test_a_supervisor_that_can_not_be_asked_converts_nothing(
    mass: MusicAssistant,
    reconcile: AsyncMock,
    supervisor: FakeSupervisor,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    problem: str,
) -> None:
    """Without the Supervisor's mounts nothing changes, and the next start tries again."""
    _store_source(mass, SMB_ID, SMB_SETUP)
    await _store_mappings(mass, ("track", SMB_ID, "Artist/Album/01.flac"))
    before = copy.deepcopy(mass.config.get(CONF_PROVIDERS))
    supervisor_url = hassio.SUPERVISOR_URL
    if problem == "no_manager_role":
        supervisor.refuse_access = True
    elif problem == "gone":
        monkeypatch.setattr(hassio, "SUPERVISOR_URL", "http://127.0.0.1:9")
    else:
        monkeypatch.setattr(consolidation_module, "SUPERVISOR_TIMEOUT", 0.2)
        supervisor.list_delay = 2

    await consolidate_filesystem_sources(mass)

    assert mass.config.get(CONF_PROVIDERS) == before
    assert _records(mass) == {}
    assert await _mappings(mass) == [
        (SMB_ID, "filesystem_smb", "track", "Artist/Album/01.flac"),
    ]
    [warning] = _leaving_warnings(caplog)
    assert warning.startswith(
        f"Leaving music sources {SMB_ID} as they are, the Supervisor can not tell which network "
        "shares it has mounted ("
    )
    assert warning.endswith("The next start tries again.")
    reconcile.assert_not_awaited()

    # the next start finds the Supervisor
    supervisor.refuse_access = False
    supervisor.list_delay = 0
    monkeypatch.setattr(hassio, "SUPERVISOR_URL", supervisor_url)
    await consolidate_filesystem_sources(mass)

    assert _config(mass, SMB_ID)["domain"] == "filesystem_local"
    assert _records(mass)["music"].backend == MountBackend.SUPERVISOR


@pytest.mark.usefixtures("reconcile")
async def test_a_source_with_unreadable_settings_is_left_as_it_is(
    mass: MusicAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """A source whose settings can not be decrypted stays byte for byte, the others convert."""
    _store_source(mass, SMB_ID, SMB_SETUP)
    _store_source(mass, SMB_ID_2, {**SMB_SETUP, "share": "books"})
    # encrypted with a key this install does not have
    other_key = Fernet(Fernet.generate_key())
    mass.config.set(
        f"{CONF_PROVIDERS}/{SMB_ID_2}/setup_data/password",
        ENCRYPT_SUFFIX + other_key.encrypt(b"secret").decode(),
    )
    unreadable = json.dumps(_config(mass, SMB_ID_2), sort_keys=True)
    await _store_mappings(mass, ("track", SMB_ID, "one.flac"), ("track", SMB_ID_2, "two.flac"))

    await consolidate_filesystem_sources(mass)

    assert json.dumps(_config(mass, SMB_ID_2), sort_keys=True) == unreadable
    assert _config(mass, SMB_ID)["domain"] == "filesystem_local"
    assert await _mappings(mass) == [
        (SMB_ID, "filesystem_local", "track", "one.flac"),
        (SMB_ID_2, "filesystem_smb", "track", "two.flac"),
    ]
    assert list(_records(mass)) == ["music"]
    assert _leaving_warnings(caplog) == [
        f"Leaving music source {SMB_ID_2} as it is, its settings can not be decrypted. "
        "The next start tries again."
    ]
    assert "secret" not in caplog.text


@pytest.mark.parametrize(
    ("instance_id", "setup", "reason"),
    [
        # the SMB provider refused a share name with a folder in it
        (SMB_ID, {**SMB_SETUP, "share": "music/albums"}, "its share name is not valid"),
        (SMB_ID, {**SMB_SETUP, "share": ""}, "its share name is not valid"),
        (SMB_ID, {**SMB_SETUP, "host": ""}, "it names no server"),
        # a step up may lead out of the share, also where it looks as if it stays inside
        (SMB_ID, {**SMB_SETUP, "subfolder": "../music"}, "its subfolder goes up a folder"),
        (SMB_ID, {**SMB_SETUP, "subfolder": "..\\music"}, "its subfolder goes up a folder"),
        (SMB_ID, {**SMB_SETUP, "subfolder": "a/../../b"}, "its subfolder goes up a folder"),
        (SMB_ID, {**SMB_SETUP, "subfolder": "a/../b"}, "its subfolder goes up a folder"),
        # the NFS provider refused an export path that is not absolute
        (NFS_ID, {**NFS_SETUP, "export_path": "volume1/books"}, "its export path is not valid"),
        (NFS_ID, {**NFS_SETUP, "subfolder": "../other"}, "its subfolder goes up a folder"),
    ],
)
@pytest.mark.usefixtures("reconcile")
async def test_a_source_that_could_not_mount_is_left_as_it_is(
    mass: MusicAssistant,
    caplog: pytest.LogCaptureFixture,
    instance_id: str,
    setup: dict[str, Any],
    reason: str,
) -> None:
    """Settings that never mounted a share are not guessed at, and the log says why."""
    _store_source(mass, instance_id, setup)
    before = copy.deepcopy(_config(mass, instance_id))

    await consolidate_filesystem_sources(mass)

    assert _config(mass, instance_id) == before
    assert _records(mass) == {}
    assert _leaving_warnings(caplog) == [
        f"Leaving music source {instance_id} as it is, {reason}. The next start tries again."
    ]
    assert "p@ss,word" not in caplog.text


@pytest.mark.usefixtures("reconcile")
async def test_a_source_without_settings_or_with_odd_ones_is_left_as_it_is(
    mass: MusicAssistant, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A source with no settings, or settings that fail in an unforeseen way, waits too."""
    _store_source(mass, SMB_ID, SMB_SETUP)
    mass.config.remove(f"{CONF_PROVIDERS}/{SMB_ID}/setup_data")
    _store_source(mass, NFS_ID, NFS_SETUP)
    read = consolidation_module._read_removed_source

    def _read(mass: MusicAssistant, raw_conf: dict[str, Any]) -> Any:
        if raw_conf["instance_id"] == NFS_ID:
            raise ValueError("an odd value")
        return read(mass, raw_conf)

    monkeypatch.setattr(consolidation_module, "_read_removed_source", _read)

    await consolidate_filesystem_sources(mass)

    assert _config(mass, SMB_ID)["domain"] == "filesystem_smb"
    assert _config(mass, NFS_ID)["domain"] == "filesystem_nfs"
    assert _leaving_warnings(caplog) == [
        f"Leaving music source {SMB_ID} as it is, it has no settings. The next start tries again.",
        f"Leaving music source {NFS_ID} as it is, its settings can not be read (ValueError). "
        "The next start tries again.",
    ]


@pytest.mark.usefixtures("reconcile")
async def test_a_disabled_source_converts_and_stays_disabled(mass: MusicAssistant) -> None:
    """A disabled source is converted like any other, and the user enables it when they want."""
    _store_source(mass, NFS_ID, NFS_SETUP, enabled=False)

    await consolidate_filesystem_sources(mass)

    assert _config(mass, NFS_ID)["domain"] == "filesystem_local"
    assert _config(mass, NFS_ID)["enabled"] is False


async def test_a_second_start_with_nothing_new_changes_nothing(
    mass: MusicAssistant,
    reconcile: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A start after the one that converted everything finds nothing to do, and does nothing."""
    _store_source(mass, SMB_ID, SMB_SETUP)
    await _store_mappings(mass, ("track", SMB_ID, "one.flac"))
    await consolidate_filesystem_sources(mass)
    converted = copy.deepcopy(mass.config.get(CONF_PROVIDERS))
    records = copy.deepcopy(mass.config.get(CONF_STORAGE_SHARES))
    writes = _watch_writes(mass, monkeypatch)
    caplog.clear()

    await consolidate_filesystem_sources(mass)

    assert mass.config.get(CONF_PROVIDERS) == converted
    assert mass.config.get(CONF_STORAGE_SHARES) == records
    assert await _mappings(mass) == [(SMB_ID, "filesystem_local", "track", "one.flac")]
    assert [mock.call_count for mock in writes] == [0] * len(writes)
    assert _consolidation_records(caplog) == []
    reconcile.assert_awaited_once()


@pytest.mark.usefixtures("reconcile")
async def test_a_start_without_smb_or_nfs_sources_does_nothing(
    mass: MusicAssistant,
    supervisor: FakeSupervisor,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Without such a source nothing is asked, decrypted, logged or written, at every start."""
    mass.config.set(
        f"{CONF_PROVIDERS}/{LOCAL_ID}",
        {
            "type": "music",
            "domain": "filesystem_local",
            "instance_id": LOCAL_ID,
            "values": {},
            "setup_data": {"path": mass.config.encrypt_string("/media")},
        },
    )
    writes = _watch_writes(mass, monkeypatch)
    decrypt = MagicMock(side_effect=mass.config.decrypt_string)
    monkeypatch.setattr(mass.config, "decrypt_string", decrypt)
    caplog.clear()

    await consolidate_filesystem_sources(mass)
    await consolidate_filesystem_sources(mass)

    assert supervisor.requests == []
    decrypt.assert_not_called()
    assert [mock.call_count for mock in writes] == [0] * len(writes)
    assert _consolidation_records(caplog) == []


@pytest.mark.usefixtures("reconcile")
async def test_sources_that_can_not_be_converted_do_not_ask_the_supervisor(
    mass: MusicAssistant,
    supervisor: FakeSupervisor,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """
    With only sources that can not be converted the Supervisor is not asked.

    A refusing Supervisor would otherwise add its own warning to theirs on every start.
    """
    supervisor.refuse_access = True
    ask = AsyncMock(wraps=supervisor_module.create_supervisor_mounter)
    monkeypatch.setattr(consolidation_module, "create_supervisor_mounter", ask)
    _store_source(mass, SMB_ID, {**SMB_SETUP, "share": "music/albums"})
    before = copy.deepcopy(_config(mass, SMB_ID))

    await consolidate_filesystem_sources(mass)

    ask.assert_not_awaited()
    assert _config(mass, SMB_ID) == before
    assert _leaving_warnings(caplog) == [
        f"Leaving music source {SMB_ID} as it is, its share name is not valid. "
        "The next start tries again."
    ]


@pytest.mark.usefixtures("reconcile")
async def test_a_source_that_can_not_be_converted_is_tried_again_at_every_start(
    mass: MusicAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """A source that waits is tried at every start, with one warning, until it converts."""
    _store_source(mass, SMB_ID, {**SMB_SETUP, "share": "music/albums"})

    await consolidate_filesystem_sources(mass)
    await consolidate_filesystem_sources(mass)

    assert _config(mass, SMB_ID)["domain"] == "filesystem_smb"
    assert len(_leaving_warnings(caplog)) == 2

    # the reason is gone, e.g. the settings were fixed in an older version
    _store_source(mass, SMB_ID, SMB_SETUP)
    caplog.clear()
    await consolidate_filesystem_sources(mass)

    assert _config(mass, SMB_ID)["domain"] == "filesystem_local"
    assert _leaving_warnings(caplog) == []


@pytest.mark.usefixtures("reconcile")
async def test_a_source_that_appears_after_an_earlier_start_is_converted(
    mass: MusicAssistant,
) -> None:
    """
    A source that comes back later, e.g. after a downgrade, converts at the next start.

    Also on an install that ran a build which marked the conversion done in its settings.
    """
    mass.config.set("filesystem_sources_consolidated", True)
    await consolidate_filesystem_sources(mass)

    _store_source(mass, SMB_ID, SMB_SETUP)
    await consolidate_filesystem_sources(mass)

    assert _config(mass, SMB_ID)["domain"] == "filesystem_local"
    assert _path(mass, SMB_ID) == f"{MOUNT_ROOT}/music"


@pytest.mark.usefixtures("reconcile")
async def test_a_later_source_on_the_share_of_a_converted_one_shares_its_share(
    mass: MusicAssistant,
) -> None:
    """A source that appears after another on the same share was converted reads that share."""
    _store_source(mass, SMB_ID, {**SMB_SETUP, "subfolder": "music"})
    await consolidate_filesystem_sources(mass)
    converted = copy.deepcopy(_config(mass, SMB_ID))
    records = copy.deepcopy(mass.config.get(CONF_STORAGE_SHARES))

    _store_source(mass, SMB_ID_2, {**SMB_SETUP, "share": "music", "subfolder": "books"})
    await consolidate_filesystem_sources(mass)

    assert mass.config.get(CONF_STORAGE_SHARES) == records
    assert _config(mass, SMB_ID) == converted
    assert _path(mass, SMB_ID_2) == f"{MOUNT_ROOT}/music/books"


@pytest.mark.usefixtures("reconcile")
async def test_a_source_that_appears_after_the_access_records_keeps_its_own(
    mass: MusicAssistant,
) -> None:
    """
    A source added after the access records were made keeps the record it was created with.

    An admin creates it without a record, as a Local files source, which makes it a source of
    the whole home; a record set on it later stays as it is.
    """
    assert mass.config.get(CONF_PROVIDER_ACCESS_MIGRATED) is True
    _store_source(mass, SMB_ID, SMB_SETUP, access=None)
    _store_source(mass, SMB_ID_2, {**SMB_SETUP, "share": "books"})
    member = await mass.webserver.auth.create_user(username="bob", role=UserRole.USER)

    await consolidate_filesystem_sources(mass)

    assert _config(mass, SMB_ID)["domain"] == "filesystem_local"
    assert _config(mass, SMB_ID)["access"] is None
    assert _config(mass, SMB_ID_2)["access"] == ACCESS.to_dict()
    visible = visible_music_sources(mass, member)
    assert visible is not None
    assert SMB_ID in visible
    assert SMB_ID_2 not in visible


async def test_a_failing_listing_after_the_conversion_is_logged_as_such(
    mass: MusicAssistant,
    reconcile: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The conversion is done when the new shares can not be listed, and they still get mounted."""
    monkeypatch.setattr(mass.storage, "refresh", AsyncMock(side_effect=OSError("no mount table")))
    _store_source(mass, SMB_ID, SMB_SETUP)

    await consolidate_filesystem_sources(mass)

    assert _config(mass, SMB_ID)["domain"] == "filesystem_local"
    assert "Unable to list the network shares of the converted music sources" in caplog.text
    assert "Unable to convert" not in caplog.text
    reconcile.assert_awaited_once()


async def test_a_failing_library_update_changes_nothing(
    mass: MusicAssistant, reconcile: AsyncMock, caplog: pytest.LogCaptureFixture
) -> None:
    """When the library can not be updated the settings stay as they were, for the next start."""
    _store_source(mass, SMB_ID, SMB_SETUP)
    await _store_mappings(mass, ("track", SMB_ID, "one.flac"))
    before = copy.deepcopy(mass.config.get(CONF_PROVIDERS))
    locked = AsyncMock(side_effect=sqlite3.OperationalError("database is locked"))

    with patch.object(mass.music.database, "execute_write", locked):
        await consolidate_filesystem_sources(mass)

    assert mass.config.get(CONF_PROVIDERS) == before
    assert _records(mass) == {}
    assert await _mappings(mass) == [(SMB_ID, "filesystem_smb", "track", "one.flac")]
    assert f"Unable to convert music sources {SMB_ID}, the next start tries again" in caplog.text
    assert "database is locked" in caplog.text
    reconcile.assert_not_awaited()

    await consolidate_filesystem_sources(mass)

    assert _config(mass, SMB_ID)["domain"] == "filesystem_local"
    assert await _mappings(mass) == [(SMB_ID, "filesystem_local", "track", "one.flac")]


async def test_settings_that_do_not_reach_the_disk_are_converted_again(
    mass: MusicAssistant, reconcile: AsyncMock, caplog: pytest.LogCaptureFixture
) -> None:
    """
    A failed save leaves the library converted and the settings on disk as they were.

    The library update can be repeated as it is, so the next start, which reads the old
    settings again, converts them and ends where a run without the failure ends.
    """
    _store_source(mass, SMB_ID, {**SMB_SETUP, "subfolder": "albums"})
    await _store_mappings(mass, ("track", SMB_ID, "one.flac"))
    on_disk = copy.deepcopy(mass.config.get(CONF_PROVIDERS))
    disk_full = AsyncMock(side_effect=OSError("disk full"))

    with patch.object(mass.config, "async_save", disk_full):
        await consolidate_filesystem_sources(mass)

    assert "disk full" in caplog.text
    assert await _mappings(mass) == [(SMB_ID, "filesystem_local", "track", "one.flac")]
    reconcile.assert_not_awaited()

    # the next start reads the settings from before the conversion
    mass.config.set(CONF_PROVIDERS, on_disk)
    mass.config.remove(CONF_STORAGE_SHARES)
    await consolidate_filesystem_sources(mass)

    assert _config(mass, SMB_ID)["domain"] == "filesystem_local"
    assert _path(mass, SMB_ID) == f"{MOUNT_ROOT}/music/albums"
    assert list(_records(mass)) == ["music"]
    assert await _mappings(mass) == [(SMB_ID, "filesystem_local", "track", "one.flac")]


@pytest.mark.usefixtures("reconcile")
async def test_the_conversion_never_mounts_or_touches_a_share(
    mass: MusicAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only data changes: no mount command runs and no share is probed, awake or not."""
    probe = MagicMock()
    mount_command = AsyncMock()
    monkeypatch.setattr(storage_controller_module, "_probe_path", probe)
    monkeypatch.setattr(local_mount_module, "check_output", mount_command)
    _store_source(mass, SMB_ID, SMB_SETUP)
    _store_source(mass, NFS_ID, NFS_SETUP)

    await consolidate_filesystem_sources(mass)

    assert _config(mass, SMB_ID)["domain"] == "filesystem_local"
    probe.assert_not_called()
    mount_command.assert_not_called()
    # the new shares are listed right away
    locations = {location.path: location for location in mass.storage.get_locations()}
    assert locations[f"{MOUNT_ROOT}/music"].managed is True
    assert locations[f"{MOUNT_ROOT}/books"].managed is True


async def test_a_source_converts_on_the_first_start_before_the_providers_load(
    tmp_path: Path,
) -> None:
    """
    A source converts on the first start, before the providers load.

    It gets the access record of a Local files source, and the load that follows is the one
    of Local files, which finds its share not mounted.
    """
    storage_path = tmp_path / "data"
    storage_path.mkdir(parents=True)
    (storage_path / "settings.json").write_text(
        json.dumps(
            {
                CONF_PROVIDERS: {
                    SMB_ID: {
                        "type": "music",
                        "domain": "filesystem_smb",
                        "instance_id": SMB_ID,
                        "enabled": True,
                        "values": {},
                        "setup_data": {
                            "host": "nas.local",
                            "share": "music",
                            "subfolder": "albums",
                        },
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    with _without_mount_backends():
        async with full_mass_context(tmp_path) as mass:
            raw_conf = _config(mass, SMB_ID)
            assert raw_conf["domain"] == "filesystem_local"
            assert _path(mass, SMB_ID) == f"{MOUNT_ROOT}/music/albums"
            assert mass.config.get(CONF_PROVIDER_ACCESS_MIGRATED) is True
            assert raw_conf["access"] == ProviderAccess(sharing=ProviderSharing.EVERYONE).to_dict()
            assert raw_conf["last_error"]["translation_key"] == "storage_location_unavailable"


async def test_a_converted_source_that_can_not_load_shows_the_default_name_of_local_files(
    tmp_path: Path,
) -> None:
    """A source whose share is not mounted shows the default name of Local files, not the old one."""
    storage_path = tmp_path / "data"
    storage_path.mkdir(parents=True)
    shares = {SMB_ID: "music", SMB_ID_2: "books"}
    providers = {
        instance_id: {
            "type": "music",
            "domain": "filesystem_smb",
            "instance_id": instance_id,
            "enabled": True,
            "name": None,
            "default_name": f"Filesystem (remote share) [{share}]",
            "values": {},
            "setup_data": {"host": "nas.local", "share": share},
        }
        for instance_id, share in shares.items()
    }
    (storage_path / "settings.json").write_text(
        json.dumps({CONF_PROVIDERS: providers}), encoding="utf-8"
    )

    with _without_mount_backends():
        async with full_mass_context(tmp_path) as mass:
            locations = {location.path: location for location in mass.storage.get_locations()}
            for instance_id, share in shares.items():
                raw_conf = _config(mass, instance_id)
                assert raw_conf["last_error"]["translation_key"] == "storage_location_unavailable"
                assert raw_conf["name"] is None
                assert raw_conf["default_name"] == f"Local files [{share}]"
                assert locations[f"{MOUNT_ROOT}/{share}"].used_by == [f"Local files [{share}]"]


async def test_a_conversion_killed_after_the_library_update_converts_on_the_next_start(
    tmp_path: Path,
) -> None:
    """
    A conversion killed between its library update and its save converts on the next start.

    The kill leaves the library rows of the source on the Local files domain, while the
    settings on disk still hold the SMB source. Until a start converts it, the
    source is not listed and not loaded, as its provider is gone; neither that, the library
    maintenance nor background audio analysis touches its rows. The next start converts it as
    if the first one had not been cut short.
    """
    storage_path = tmp_path / "data"
    await _prepare_install_to_convert(tmp_path)
    library_before, tracks_before = _library_on_disk(storage_path)
    killed_library = [
        (instance_id, "filesystem_local" if instance_id == SMB_ID else domain, *rest)
        for instance_id, domain, *rest in library_before
    ]

    real_update = consolidation_module._update_library

    async def _update_then_kill(mass: MusicAssistant, instance_ids: list[str]) -> None:
        await real_update(mass, instance_ids)
        raise _Killed

    with (
        _without_mount_backends(),
        patch.object(consolidation_module, "_update_library", _update_then_kill),
        pytest.raises(_Killed),
    ):
        async with full_mass_context(tmp_path):
            pass

    settings = json.loads((storage_path / "settings.json").read_text(encoding="utf-8"))
    assert settings[CONF_PROVIDERS][SMB_ID]["domain"] == "filesystem_smb"
    assert CONF_STORAGE_SHARES not in settings
    assert _library_on_disk(storage_path) == (killed_library, tracks_before)

    # a start that can not convert: the Supervisor does not answer
    no_supervisor = AsyncMock(side_effect=BackendUnavailable("the Supervisor did not answer"))
    with (
        _without_mount_backends(),
        patch.object(consolidation_module, "_get_mounter", no_supervisor),
    ):
        async with full_mass_context(tmp_path) as mass:
            assert _config(mass, SMB_ID)["domain"] == "filesystem_smb"
            assert mass.get_provider(SMB_ID, return_unavailable=True) is None
            listed = {config.instance_id for config in await mass.config.get_provider_configs()}
            assert SMB_ID not in listed
            await _run_library_maintenance(mass)
            assert _deleted_providers(mass) == []
    assert _library_on_disk(storage_path) == (killed_library, tracks_before)

    with _without_mount_backends():
        async with full_mass_context(tmp_path) as mass:
            raw_conf = _config(mass, SMB_ID)
            assert raw_conf["domain"] == "filesystem_local"
            assert raw_conf["access"] == ACCESS.to_dict()
            assert _path(mass, SMB_ID) == f"{MOUNT_ROOT}/music/albums"
            assert list(_records(mass)) == ["music"]
    assert _library_on_disk(storage_path) == (killed_library, tracks_before)


async def test_a_source_converted_on_a_later_start_gets_the_access_of_local_files(
    tmp_path: Path,
) -> None:
    """
    A source whose conversion waits for a later start gets the access record it gets otherwise.

    The access records are made on the first start also when the Supervisor keeps the SMB
    source from being converted then, while its provider is gone. The source gets the record
    a Local files source gets, not a hidden one, and the conversion on the next start keeps it.
    """
    deferred_path, first_path = tmp_path / "deferred", tmp_path / "first"
    alice = await _prepare_install_with_a_restricted_user(deferred_path)
    no_supervisor = AsyncMock(side_effect=BackendUnavailable("the Supervisor did not answer"))
    with (
        _without_mount_backends(),
        patch.object(consolidation_module, "_get_mounter", no_supervisor),
    ):
        async with full_mass_context(deferred_path) as mass:
            assert _config(mass, SMB_ID)["domain"] == "filesystem_smb"
            assert mass.config.get(CONF_PROVIDER_ACCESS_MIGRATED) is True
            deferred = _config(mass, SMB_ID)["access"]
    with _without_mount_backends():
        async with full_mass_context(deferred_path) as mass:
            assert _config(mass, SMB_ID)["domain"] == "filesystem_local"
            assert _config(mass, SMB_ID)["access"] == deferred

    # the same install, converted on its first start
    alice_first = await _prepare_install_with_a_restricted_user(first_path)
    with _without_mount_backends():
        async with full_mass_context(first_path) as mass:
            assert _config(mass, SMB_ID)["domain"] == "filesystem_local"
            first = _config(mass, SMB_ID)["access"]

    assert first == ProviderAccess(owner=alice_first, sharing=ProviderSharing.EVERYONE).to_dict()
    assert deferred == ProviderAccess(owner=alice, sharing=ProviderSharing.EVERYONE).to_dict()
