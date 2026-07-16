"""Tests for the Yandex Disk provider's cloud hooks."""

from __future__ import annotations

from typing import Any, cast

import pytest

from music_assistant.providers.filesystem_yandex_disk.provider import YandexDiskFileSystemProvider


class _FakeApi:
    """Records calls made by the provider hooks."""

    def __init__(self) -> None:
        self.listed: str | None = None

    async def list_children(self, folder: str) -> list[tuple[str, str, bool, str, int | None]]:
        self.listed = folder
        return [("disk:/M/a.flac", "a.flac", False, "h", 1)]

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
    assert out == [("disk:/M/a.flac", "a.flac", False, "h", 1)]


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
