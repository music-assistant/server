"""Tests for the Yandex Disk provider's cloud hooks."""

from __future__ import annotations

import sys
from typing import Any, cast
from unittest import mock

import pytest

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
        return [("disk:/M/a.flac", "a.flac", False, "h", 1, "modified")]

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
    assert out == [("disk:/M/a.flac", "a.flac", False, "h", 1, "modified")]


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


def _construct_provider(
    folder_id: str | None = "root", *, legacy_root: str | None = None
) -> tuple[Any, mock.Mock, mock.Mock]:
    """Construct the provider with setup-data-aware dependencies mocked."""
    mass = mock.Mock()
    config = mock.Mock()
    config.instance_id = "filesystem_yandex_disk--test"
    config.setup_data = {
        "client_id": "client-id",
        "client_secret": "client-secret",
        "refresh_token": "refresh-token",
        "content_type": "podcasts",
    }
    if folder_id is not None:
        config.setup_data["folder_id"] = folder_id
    config.values = {}
    config.get_value.side_effect = lambda key, default=None: (
        legacy_root if key == "root_path" else default
    )
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


def test_init_reads_legacy_root_path() -> None:
    """Existing pre-SetupSession instances retain their configured scan root."""
    provider, _auth_cls, _config = _construct_provider(None, legacy_root="disk:/Legacy")

    assert provider.root_folder_id == "disk:/Legacy"


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
async def test_inherited_config_entries_only_expose_runtime_sync_options() -> None:
    """The provider instance inherits runtime entries and uses its setup content type."""
    provider, _auth_cls, _config = _construct_provider()

    entries = await provider.get_config_entries()
    keys = {entry.key for entry in entries}

    assert {"client_id", "client_secret", "refresh_token", "folder_id"}.isdisjoint(keys)
    assert {"library_sync_tracks", "library_sync_playlists"} <= keys
    content_type = next(entry for entry in entries if entry.key == "content_type")
    assert content_type.read_only is True
    assert content_type.default_value == "podcasts"
