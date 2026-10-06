"""Tests for the Yandex Disk provider's cloud hooks."""

from __future__ import annotations

import sys
from typing import Any, cast
from unittest import mock

import pytest
from music_assistant_models.errors import SetupFailedError

import music_assistant.providers.filesystem_yandex_disk as provider_package
from music_assistant.providers.filesystem_cloud.base import CloudFileSystemProvider
from music_assistant.providers.filesystem_yandex_disk.constants import DISK_ROOT
from music_assistant.providers.filesystem_yandex_disk.provider import YandexDiskFileSystemProvider

provider_module = sys.modules[YandexDiskFileSystemProvider.__module__]


class _FakeApi:
    """Records calls made by the provider hooks."""

    def __init__(self) -> None:
        self.listed: str | None = None

    async def list_children(
        self, folder: str
    ) -> list[tuple[str, str, bool, str, int | None, str | None]]:
        self.listed = folder
        return [("disk:/M/a.flac", "a.flac", False, "h", 1, "2026-10-05T18:00:00Z")]

    async def download_bytes(self, path: str) -> bytes:
        return b"data-" + path.encode()

    async def download_response(
        self, path: str, headers: dict[str, str]
    ) -> tuple[str, str, dict[str, str]]:
        return ("resp", path, headers)


def _provider_with_fake_api() -> tuple[YandexDiskFileSystemProvider, _FakeApi]:
    prov = YandexDiskFileSystemProvider.__new__(YandexDiskFileSystemProvider)
    fake = _FakeApi()
    prov.api = cast("Any", fake)
    return prov, fake


@pytest.mark.asyncio
async def test_api_list_children_delegates() -> None:
    """_api_list_children forwards to the API wrapper."""
    prov, fake = _provider_with_fake_api()
    out = await prov._api_list_children("disk:/M")
    assert fake.listed == "disk:/M"
    assert out == [("disk:/M/a.flac", "a.flac", False, "h", 1, "2026-10-05T18:00:00Z")]


@pytest.mark.asyncio
async def test_api_list_children_empty_maps_to_disk_root() -> None:
    """An empty folder id resolves to the disk root."""
    prov, fake = _provider_with_fake_api()
    await prov._api_list_children("")
    assert fake.listed == "disk:/"


@pytest.mark.asyncio
async def test_api_download_bytes_delegates() -> None:
    """_api_download_bytes forwards to the API wrapper."""
    prov, _ = _provider_with_fake_api()
    assert await prov._api_download_bytes("disk:/x.nfo") == b"data-disk:/x.nfo"


@pytest.mark.asyncio
async def test_api_download_response_forwards_range() -> None:
    """_api_download_response passes the Range header through unchanged."""
    prov, _ = _provider_with_fake_api()
    resp: object = await prov._api_download_response("disk:/M/a.flac", {"Range": "bytes=10-"})
    assert resp == ("resp", "disk:/M/a.flac", {"Range": "bytes=10-"})


def _construct_provider(folder_id: str | None = "root") -> tuple[Any, mock.Mock, mock.Mock]:
    """Construct the provider with setup-data-aware dependencies mocked."""
    mass = mock.Mock()
    config = mock.Mock()
    config.instance_id = "filesystem_yandex_disk--test"
    config.setup_data = {
        "client_id": "client-id",
        "client_secret": "client-secret",
        "refresh_token": "refresh-token",
    }
    if folder_id is not None:
        config.setup_data["folder_id"] = folder_id
    config.values = {}
    config.get_value.side_effect = lambda _key, default=None: default
    mass.config.decrypt_string.side_effect = lambda value: value
    mass.config.get.side_effect = lambda key: (
        config.setup_data if key.endswith("/setup_data") else {}
    )

    def base_init(
        instance: YandexDiskFileSystemProvider,
        base_mass: Any,
        manifest: Any,
        base_config: Any,
        root_folder_id: str,
    ) -> None:
        instance.mass = base_mass
        instance.manifest = manifest
        instance.config = base_config
        instance.root_folder_id = root_folder_id

    auth = mock.Mock()
    api = mock.Mock()
    with (
        mock.patch.object(CloudFileSystemProvider, "__init__", base_init),
        mock.patch.object(provider_module, "MAYandexDiskAuth", return_value=auth) as auth_cls,
        mock.patch.object(provider_module, "YandexDiskApi", return_value=api),
    ):
        provider = YandexDiskFileSystemProvider(mass, mock.Mock(), config)
    return provider, auth_cls, config


def test_init_reads_oauth_values_from_setup_data() -> None:
    """Provider initialization consumes secrets collected by the guided flow."""
    provider, auth_cls, _config = _construct_provider()

    assert provider.root_folder_id == DISK_ROOT
    assert auth_cls.call_args.args[1:4] == ("client-id", "client-secret", "refresh-token")


def test_init_preserves_yandex_folder_path() -> None:
    """A configured Yandex folder path is passed to the cloud base unchanged."""
    provider, _auth_cls, _config = _construct_provider("disk:/Music")

    assert provider.root_folder_id == "disk:/Music"


def test_init_without_folder_scans_whole_disk() -> None:
    """Setup data without a folder falls back to the whole Yandex Disk."""
    provider, _auth_cls, _config = _construct_provider(None)

    assert provider.root_folder_id == DISK_ROOT


def test_rotated_refresh_token_updates_setup_data_immediately() -> None:
    """Refresh-token rotation persists back into encrypted setup data."""
    provider, auth_cls, _config = _construct_provider()
    provider._update_setup_data = mock.Mock()
    persist = auth_cls.call_args.args[4]

    persist("rotated-token")

    provider._update_setup_data.assert_called_once_with(
        "refresh_token", "rotated-token", immediate=True
    )


@pytest.mark.asyncio
async def test_config_entries_only_expose_runtime_sync_options() -> None:
    """The instance options page shows sync options; credentials stay in setup_data."""
    provider, _auth_cls, config = _construct_provider()
    config.setup_data["content_type"] = "audiobooks"

    entries = await provider.get_config_entries()
    keys = {entry.key for entry in entries}

    assert {"client_id", "client_secret", "refresh_token", "folder_id"}.isdisjoint(keys)
    assert {"library_sync_tracks", "library_sync_playlists"} <= keys
    content_type = next(entry for entry in entries if entry.key == "content_type")
    assert content_type.read_only is True
    assert content_type.default_value == "audiobooks"


def test_package_has_no_module_level_config_entries() -> None:
    """Options come from the provider instance; a module-level function is never called."""
    assert not hasattr(provider_package, "get_config_entries")


async def test_options_exclude_sound_effects() -> None:
    """The content-type mirror only lists supported cloud content."""
    provider, _auth_cls, _config = _construct_provider()
    entries = await provider.get_config_entries()
    entry = next(e for e in entries if e.key == "content_type")
    assert entry.options is not None
    assert {option.value for option in entry.options} == {"music", "audiobooks", "podcasts"}


async def test_existing_sound_effects_instance_requires_reconfiguration() -> None:
    """Unsupported saved instances fail clearly before accessing Disk or registering routes."""
    provider, _auth_cls, _config = _construct_provider()
    provider.media_content_type = "sound_effects"
    provider.api.validate = mock.AsyncMock()
    with pytest.raises(SetupFailedError, match="reconfigure"):
        await provider.handle_async_init()
    provider.api.validate.assert_not_awaited()
