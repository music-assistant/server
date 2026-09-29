"""Tests for the yadisk-backed API client mapping and token handling."""

from __future__ import annotations

from typing import Any, cast

import pytest
from music_assistant_models.errors import LoginFailed

from music_assistant.providers.filesystem_yandex_disk.api_client import YandexDiskApi, _to_raw_item


class _Resource:
    """Minimal stand-in for a yadisk resource object."""

    def __init__(self, **kwargs: object) -> None:
        self.__dict__.update(kwargs)


def test_to_raw_item_file() -> None:
    """A file resource maps to a RawItem with checksum, size, and metadata token."""
    res = _Resource(
        path="disk:/Music/a.flac",
        name="a.flac",
        type="file",
        md5="abc",
        size=123,
        modified="2026-08-28T07:00:00Z",
    )
    assert _to_raw_item(res) == (
        "disk:/Music/a.flac",
        "a.flac",
        False,
        "abc",
        123,
        "2026-08-28T07:00:00Z",
    )


def test_to_raw_item_dir_has_empty_checksum_and_no_size() -> None:
    """A directory has no size and an empty checksum."""
    res = _Resource(path="disk:/Music", name="Music", type="dir", md5=None, size=None)
    assert _to_raw_item(res) == ("disk:/Music", "Music", True, "", None, None)


def test_to_raw_item_file_without_md5_falls_back() -> None:
    """A file lacking md5 gets the 'unknown' checksum sentinel."""
    res = _Resource(path="disk:/x.mp3", name="x.mp3", type="file", md5=None, size=1)
    _id, _name, is_dir, checksum, size, metadata_token = _to_raw_item(res)
    assert is_dir is False
    assert checksum == "unknown"
    assert size == 1
    assert metadata_token is None


class _AuthStub:
    async def async_get_access_token(self) -> str:
        return "at"


@pytest.mark.asyncio
async def test_validate_refreshes_and_accepts_token() -> None:
    """validate() pulls a fresh access token and accepts a valid one."""
    api = YandexDiskApi.__new__(YandexDiskApi)
    api._auth = cast("Any", _AuthStub())

    class _Client:
        token = ""

        async def check_token(self) -> bool:
            return True

    api._client = cast("Any", _Client())
    await api.validate()
    assert api._client.token == "at"  # refreshed onto the yadisk client


@pytest.mark.asyncio
async def test_validate_rejected_token_raises() -> None:
    """A token the API rejects surfaces as LoginFailed."""
    api = YandexDiskApi.__new__(YandexDiskApi)
    api._auth = cast("Any", _AuthStub())

    class _Client:
        token = ""

        async def check_token(self) -> bool:
            return False

    api._client = cast("Any", _Client())
    with pytest.raises(LoginFailed):
        await api.validate()
