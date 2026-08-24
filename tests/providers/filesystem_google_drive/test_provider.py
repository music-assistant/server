"""Tests for the Google Drive provider's API hooks and setup checks."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import ClientError
from google_drive_api.exceptions import ApiException, AuthException
from music_assistant_models.errors import (
    LoginFailed,
    MediaNotFoundError,
    ProviderUnavailableError,
    SetupFailedError,
)

from music_assistant.providers.filesystem_google_drive.constants import FOLDER_MIME_TYPE
from music_assistant.providers.filesystem_google_drive.provider import (
    GoogleDriveFileSystemProvider,
    _translate_errors,
)


def _make_provider(
    root_folder_id: str = "root",
) -> tuple[GoogleDriveFileSystemProvider, MagicMock]:
    """Return a bare provider plus the mock bundle behind its api/auth/mass."""
    mocks = MagicMock()
    mocks.config.instance_id = "gdrive--test"
    provider = GoogleDriveFileSystemProvider.__new__(GoogleDriveFileSystemProvider)
    provider.root_folder_id = root_folder_id
    provider._root_folder_name = None
    provider._unregister_stream_route = None
    provider._dir_cache = {}
    provider.logger = mocks.logger
    provider.api = mocks.api
    provider.auth = mocks.auth
    provider.mass = mocks.mass
    provider.config = mocks.config
    return provider, mocks


async def test_list_children_follows_pagination() -> None:
    """All pages are fetched and the page token is passed along."""
    provider, mocks = _make_provider()
    mocks.api.list_files = AsyncMock(
        side_effect=[
            {
                "files": [{"id": "f1", "name": "one.mp3", "modifiedTime": "t1", "size": "10"}],
                "nextPageToken": "page2",
            },
            {"files": [{"id": "f2", "name": "two.mp3", "modifiedTime": "t2", "size": "20"}]},
        ]
    )

    items = await provider._api_list_children("folder-id")

    assert [item[0] for item in items] == ["f1", "f2"]
    second_params = mocks.api.list_files.call_args_list[1].kwargs["params"]
    assert second_params["pageToken"] == "page2"


async def test_list_children_maps_drive_fields() -> None:
    """Drive file dicts map onto RawItem tuples, with sane fallbacks."""
    provider, mocks = _make_provider()
    mocks.api.list_files = AsyncMock(
        return_value={
            "files": [
                {"id": "d1", "name": "Albums", "mimeType": FOLDER_MIME_TYPE},
                {"id": "f1", "name": "track.mp3", "modifiedTime": "2026-01-01", "size": "123"},
                {"id": "f2", "name": "no-meta.mp3"},
            ]
        }
    )

    items = await provider._api_list_children("folder-id")

    assert items[0] == ("d1", "Albums", True, "unknown", None)
    assert items[1] == ("f1", "track.mp3", False, "2026-01-01", 123)
    assert items[2] == ("f2", "no-meta.mp3", False, "unknown", None)


async def test_list_children_translates_api_errors() -> None:
    """Drive client errors surface as MA errors per the base's hook contract."""
    provider, mocks = _make_provider()
    mocks.api.list_files = AsyncMock(side_effect=ApiException("quota"))

    with pytest.raises(ProviderUnavailableError):
        await provider._api_list_children("folder-id")


def test_translate_errors_mapping() -> None:
    """Auth errors become LoginFailed; API/transport errors ProviderUnavailableError."""
    with pytest.raises(LoginFailed), _translate_errors():
        raise AuthException("expired")
    with pytest.raises(ProviderUnavailableError), _translate_errors():
        raise ApiException("boom")
    with pytest.raises(ProviderUnavailableError), _translate_errors():
        raise ClientError("connection reset")
    # MA errors pass through untouched
    with pytest.raises(MediaNotFoundError), _translate_errors():
        raise MediaNotFoundError("missing")


async def test_async_init_verifies_auth() -> None:
    """Bad credentials fail setup with LoginFailed."""
    provider, mocks = _make_provider()
    mocks.api.get_user = AsyncMock(side_effect=AuthException("invalid_grant"))

    with pytest.raises(LoginFailed):
        await provider.handle_async_init()


async def test_async_init_rejects_unknown_folder_id() -> None:
    """A folder ID Drive doesn't know fails setup with a helpful message."""
    provider, mocks = _make_provider(root_folder_id="not-a-real-id")
    mocks.api.get_user = AsyncMock(return_value={})
    mocks.auth.get_json = AsyncMock(side_effect=ApiException("404"))

    with pytest.raises(SetupFailedError, match="not the folder name"):
        await provider.handle_async_init()


async def test_async_init_rejects_non_folder_id() -> None:
    """A file ID configured as the root folder fails setup."""
    provider, mocks = _make_provider(root_folder_id="file-id")
    mocks.api.get_user = AsyncMock(return_value={})
    mocks.auth.get_json = AsyncMock(return_value={"id": "file-id", "mimeType": "audio/mpeg"})

    with pytest.raises(SetupFailedError, match="not a folder"):
        await provider.handle_async_init()


async def test_async_init_stores_folder_name_and_registers_route() -> None:
    """A valid folder ID stores the display name and registers the stream route."""
    provider, mocks = _make_provider(root_folder_id="folder-id")
    mocks.api.get_user = AsyncMock(return_value={})
    mocks.auth.get_json = AsyncMock(
        return_value={"id": "folder-id", "name": "My Music", "mimeType": FOLDER_MIME_TYPE}
    )

    await provider.handle_async_init()

    assert provider.instance_name_postfix == "My Music"
    mocks.mass.streams.register_dynamic_route.assert_called_once()


async def test_async_init_with_whole_drive_root() -> None:
    """The 'root' alias skips the folder check and has no name postfix."""
    provider, mocks = _make_provider(root_folder_id="root")
    mocks.api.get_user = AsyncMock(return_value={})

    await provider.handle_async_init()

    mocks.auth.get_json.assert_not_called()
    assert provider.instance_name_postfix is None
