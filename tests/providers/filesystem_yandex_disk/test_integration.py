"""
Integration test: raw Yandex Disk listings map through the cloud base.

Exercises the real ``CloudFileSystemProvider._scandir`` path (normalize ->
resolve id -> _api_list_children -> _to_item) against the provider's hook, so a
fixture listing turns into ``FileSystemItem``s with MA proxy stream URLs.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, cast

import pytest

from music_assistant.providers.filesystem_yandex_disk.provider import YandexDiskFileSystemProvider

_FIXTURE = Path(__file__).parent / "fixtures" / "listdir_music.json"


class _StreamsStub:
    base_url = "http://ma.local:8095"


class _MassStub:
    streams = _StreamsStub()


class _ConfigStub:
    instance_id = "yd_inst"


class _FakeApi:
    def __init__(self, rows: list[list[object]]) -> None:
        self._rows = [tuple(r) for r in rows]
        self.requested: str | None = None

    async def list_children(self, folder: str) -> list[tuple[object, ...]]:
        self.requested = folder
        return list(self._rows)


def _make_provider() -> tuple[YandexDiskFileSystemProvider, _FakeApi]:
    prov = YandexDiskFileSystemProvider.__new__(YandexDiskFileSystemProvider)
    fake = _FakeApi(json.loads(_FIXTURE.read_text()))
    prov.mass = cast("Any", _MassStub())
    prov.config = cast("Any", _ConfigStub())
    prov.logger = logging.getLogger("yandex_disk_test")
    prov.root_folder_id = "disk:/Music"
    prov._dir_cache = {}
    prov._dir_cache_expiry = {}
    prov.api = cast("Any", fake)
    return prov, fake


@pytest.mark.asyncio
async def test_scandir_maps_listing_to_filesystem_items() -> None:
    """A root scan yields directories and files with proxy stream URLs."""
    prov, fake = _make_provider()
    items = await prov._scandir("")

    # the root scan resolves "" to the configured root folder id
    assert fake.requested == "disk:/Music"

    by_name = {item.filename: item for item in items}
    assert set(by_name) == {"Album", "track1.flac", "track2.mp3"}

    album = by_name["Album"]
    assert album.is_dir is True
    assert album.absolute_path == ""  # folders don't stream

    track = by_name["track1.flac"]
    assert track.is_dir is False
    assert track.checksum == "md5aaa"
    assert track.file_size == 10485760
    # files point at MA's own proxy stream route
    assert track.absolute_path == "http://ma.local:8095/yd_inst_stream?path=track1.flac"
