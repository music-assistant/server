"""
Tests for the one-shot conversion of the SMB and NFS music sources into Local files sources.

An SMB or NFS source mounted its share itself; a network share is a storage location now and a
Local files source reads a folder in one. The conversion stores the share where it is not a
storage location yet and rewrites the source in place, so it keeps its instance id and with
that its library, its access record, its options and the name it was shown with. It only
changes data: mounting the share is left to the storage controller.
"""

from __future__ import annotations

import copy
import json
import sqlite3
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp.test_utils import TestServer
from cryptography.fernet import Fernet
from music_assistant_models.config_entries import ProviderAccess
from music_assistant_models.enums import ProviderSharing

from music_assistant.constants import (
    CONF_FILESYSTEM_SOURCES_CONSOLIDATED,
    CONF_PROVIDER_ACCESS_MIGRATED,
    CONF_PROVIDERS,
    CONF_STORAGE_SHARES,
    DB_TABLE_PROVIDER_MAPPINGS,
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
from tests.conftest import full_mass_context
from tests.controllers.storage.conftest import SUPERVISOR_TOKEN, FakeSupervisor, MountTable

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


@pytest.fixture
def reconcile(mass: MusicAssistant, monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """
    Stand in for the mounting of the storage controller, and undo the conversion of the boot.

    :param mass: The started server.
    :param monkeypatch: Pytest monkeypatch fixture.
    """
    # the full boot of the fixture already ran (and marked) the conversion
    mass.config.remove(CONF_FILESYSTEM_SOURCES_CONSOLIDATED)
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
) -> None:
    """
    Store a music source the way an install that is about to be converted holds it.

    :param mass: The started server.
    :param instance_id: The instance id of the source, starting with its domain.
    :param setup: The setup values of the source, stored encrypted.
    :param name: The name the user gave the source.
    :param enabled: Whether the source is enabled.
    :param values: The option values of the source.
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
            "access": ACCESS.to_dict(),
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
    # a single source of its provider was shown with the name of the provider
    assert smb["name"] == "Filesystem (remote share)"
    # the options Local files has too are kept, the cache mode of the SMB mount goes
    assert smb["values"] == VALUES
    assert set(smb["setup_data"]) == {"content_type", "path"}
    assert all(value.startswith(ENCRYPT_SUFFIX) for value in smb["setup_data"].values())
    assert _setup_values(mass, SMB_ID) == {"content_type": "music", "path": f"{MOUNT_ROOT}/music"}
    nfs = _config(mass, NFS_ID)
    assert nfs["domain"] == "filesystem_local"
    assert nfs["name"] == "Audiobooks on the NAS"
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
    assert mass.config.get(CONF_FILESYSTEM_SOURCES_CONSOLIDATED) is True
    reconcile.assert_awaited_once()


@pytest.mark.usefixtures("reconcile")
async def test_sources_keep_the_name_their_provider_showed(mass: MusicAssistant) -> None:
    """Next to another source of its provider a source was named after its subfolder or share."""
    _store_source(mass, SMB_ID, {**SMB_SETUP, "subfolder": "albums\\A-K"})
    _store_source(mass, SMB_ID_2, SMB_SETUP)
    _store_source(mass, NFS_ID, {**NFS_SETUP, "export_path": "/"})

    await consolidate_filesystem_sources(mass)

    assert _config(mass, SMB_ID)["name"] == "Filesystem (remote share) [albums\\A-K]"
    assert _config(mass, SMB_ID_2)["name"] == "Filesystem (remote share) [Music]"
    assert _config(mass, NFS_ID)["name"] == "Filesystem (NFS share)"


@pytest.mark.usefixtures("reconcile")
async def test_sources_without_a_subfolder_or_folder_name_were_numbered(
    mass: MusicAssistant,
) -> None:
    """NFS sources on the root of their export and without a subfolder were named by number."""
    _store_source(mass, NFS_ID, {**NFS_SETUP, "export_path": "/"})
    _store_source(mass, NFS_ID_2, {**NFS_SETUP, "host": "192.168.1.21", "export_path": "/"})

    await consolidate_filesystem_sources(mass)

    assert _config(mass, NFS_ID)["name"] == "Filesystem (NFS share) [1]"
    assert _config(mass, NFS_ID_2)["name"] == "Filesystem (NFS share) [2]"


@pytest.mark.usefixtures("reconcile")
async def test_a_source_left_as_it_is_counts_for_the_names(mass: MusicAssistant) -> None:
    """A source that is not converted was shown next to the others, so the names count it."""
    _store_source(mass, SMB_ID, SMB_SETUP)
    _store_source(mass, SMB_ID_2, {**SMB_SETUP, "share": "music/albums"})

    await consolidate_filesystem_sources(mass)

    assert _config(mass, SMB_ID)["name"] == "Filesystem (remote share) [Music]"
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
    assert mass.config.get(CONF_FILESYSTEM_SOURCES_CONSOLIDATED) is True
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
    assert mass.config.get(CONF_FILESYSTEM_SOURCES_CONSOLIDATED) is None
    assert "Unable to convert the SMB and NFS music sources" in caplog.text
    reconcile.assert_not_awaited()

    # the next start finds the Supervisor
    supervisor.refuse_access = False
    supervisor.list_delay = 0
    monkeypatch.setattr(hassio, "SUPERVISOR_URL", supervisor_url)
    await consolidate_filesystem_sources(mass)

    assert _config(mass, SMB_ID)["domain"] == "filesystem_local"
    assert _records(mass)["music"].backend == MountBackend.SUPERVISOR
    assert mass.config.get(CONF_FILESYSTEM_SOURCES_CONSOLIDATED) is True


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
    assert f"Leaving music source {SMB_ID_2} as it is" in caplog.text
    assert "secret" not in caplog.text
    assert mass.config.get(CONF_FILESYSTEM_SOURCES_CONSOLIDATED) is True


@pytest.mark.parametrize(
    ("instance_id", "setup"),
    [
        # the SMB provider refused a share name with a folder in it
        (SMB_ID, {**SMB_SETUP, "share": "music/albums"}),
        (SMB_ID, {**SMB_SETUP, "host": ""}),
        # a step up may lead out of the share, also where it looks as if it stays inside
        (SMB_ID, {**SMB_SETUP, "subfolder": "../music"}),
        (SMB_ID, {**SMB_SETUP, "subfolder": "..\\music"}),
        (SMB_ID, {**SMB_SETUP, "subfolder": "a/../../b"}),
        (SMB_ID, {**SMB_SETUP, "subfolder": "a/../b"}),
        # the NFS provider refused an export path that is not absolute
        (NFS_ID, {**NFS_SETUP, "export_path": "volume1/books"}),
        (NFS_ID, {**NFS_SETUP, "subfolder": "../other"}),
    ],
)
@pytest.mark.usefixtures("reconcile")
async def test_a_source_that_could_not_mount_is_left_as_it_is(
    mass: MusicAssistant, instance_id: str, setup: dict[str, Any]
) -> None:
    """Settings that never mounted a share are not guessed at."""
    _store_source(mass, instance_id, setup)
    before = copy.deepcopy(_config(mass, instance_id))

    await consolidate_filesystem_sources(mass)

    assert _config(mass, instance_id) == before
    assert _records(mass) == {}
    assert mass.config.get(CONF_FILESYSTEM_SOURCES_CONSOLIDATED) is True


@pytest.mark.usefixtures("reconcile")
async def test_a_disabled_source_converts_and_stays_disabled(mass: MusicAssistant) -> None:
    """A disabled source is converted like any other, and the user enables it when they want."""
    _store_source(mass, NFS_ID, NFS_SETUP, enabled=False)

    await consolidate_filesystem_sources(mass)

    assert _config(mass, NFS_ID)["domain"] == "filesystem_local"
    assert _config(mass, NFS_ID)["enabled"] is False


async def test_a_second_run_is_a_no_op(mass: MusicAssistant, reconcile: AsyncMock) -> None:
    """Once marked, the conversion does not run again."""
    await consolidate_filesystem_sources(mass)
    assert mass.config.get(CONF_FILESYSTEM_SOURCES_CONSOLIDATED) is True

    _store_source(mass, SMB_ID, SMB_SETUP)
    await consolidate_filesystem_sources(mass)

    assert _config(mass, SMB_ID)["domain"] == "filesystem_smb"
    assert _records(mass) == {}
    reconcile.assert_not_awaited()


@pytest.mark.usefixtures("reconcile")
async def test_an_install_without_smb_or_nfs_sources_is_marked(
    mass: MusicAssistant, supervisor: FakeSupervisor
) -> None:
    """With nothing to convert the conversion is marked done, without asking the Supervisor."""
    await consolidate_filesystem_sources(mass)

    assert mass.config.get(CONF_FILESYSTEM_SOURCES_CONSOLIDATED) is True
    assert supervisor.requests == []


@pytest.mark.usefixtures("reconcile")
async def test_sources_that_can_not_be_converted_do_not_wait_for_the_supervisor(
    mass: MusicAssistant, supervisor: FakeSupervisor, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    Sources that can not be converted leave the Supervisor unasked, and mark the conversion.

    A refusing Supervisor would otherwise keep the conversion, and its warning, coming back on
    every start without anything it could convert.
    """
    supervisor.refuse_access = True
    ask = AsyncMock(wraps=supervisor_module.create_supervisor_mounter)
    monkeypatch.setattr(consolidation_module, "create_supervisor_mounter", ask)
    _store_source(mass, SMB_ID, {**SMB_SETUP, "share": "music/albums"})
    before = copy.deepcopy(_config(mass, SMB_ID))

    await consolidate_filesystem_sources(mass)

    ask.assert_not_awaited()
    assert _config(mass, SMB_ID) == before
    assert mass.config.get(CONF_FILESYSTEM_SOURCES_CONSOLIDATED) is True


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
    assert mass.config.get(CONF_FILESYSTEM_SOURCES_CONSOLIDATED) is True
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
    assert mass.config.get(CONF_FILESYSTEM_SOURCES_CONSOLIDATED) is None
    assert "database is locked" in caplog.text
    reconcile.assert_not_awaited()

    await consolidate_filesystem_sources(mass)

    assert _config(mass, SMB_ID)["domain"] == "filesystem_local"
    assert await _mappings(mass) == [(SMB_ID, "filesystem_local", "track", "one.flac")]
    assert mass.config.get(CONF_FILESYSTEM_SOURCES_CONSOLIDATED) is True


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
    assert mass.config.get(CONF_FILESYSTEM_SOURCES_CONSOLIDATED) is None
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
    assert mass.config.get(CONF_FILESYSTEM_SOURCES_CONSOLIDATED) is True


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
