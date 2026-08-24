"""Tests for the OneDrive provider's API hooks and setup checks."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.errors import (
    LoginFailed,
    ProviderUnavailableError,
    SetupFailedError,
)
from onedrive_personal_sdk.exceptions import AuthenticationError, OneDriveException
from onedrive_personal_sdk.models.items import (
    File,
    Folder,
    Hashes,
    IdentitySet,
    ItemParentReference,
)

from music_assistant.providers.filesystem_onedrive.provider import OneDriveFileSystemProvider

_PARENT = ItemParentReference(drive_id="drive-1")


def _folder(item_id: str, name: str) -> Folder:
    """Build a real SDK Folder item (isinstance checks need the real class)."""
    return Folder(id=item_id, name=name, parent_reference=_PARENT, created_by=IdentitySet())


def _file(
    item_id: str,
    name: str,
    size: int,
    xor_hash: str | None = None,
    sha1_hash: str | None = None,
) -> File:
    """Build a real SDK File item."""
    return File(
        id=item_id,
        name=name,
        parent_reference=_PARENT,
        created_by=IdentitySet(),
        size=size,
        hashes=Hashes(quick_xor_hash=xor_hash, sha1_hash=sha1_hash),
    )


def _make_provider(
    root_folder_id: str = "root",
) -> tuple[OneDriveFileSystemProvider, MagicMock]:
    """Return a bare provider plus the mock bundle behind its client/auth/mass."""
    mocks = MagicMock()
    mocks.config.instance_id = "onedrive--test"
    provider = OneDriveFileSystemProvider.__new__(OneDriveFileSystemProvider)
    provider.root_folder_id = root_folder_id
    provider._root_folder_name = None
    provider._unregister_stream_route = None
    provider._dir_cache = {}
    provider.logger = mocks.logger
    provider.client = mocks.client
    provider.auth = mocks.auth
    provider.mass = mocks.mass
    provider.config = mocks.config
    return provider, mocks


def _response_cm(response: MagicMock) -> MagicMock:
    """Build a fake async context manager mimicking aiohttp's session.get()."""
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=response)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


async def test_list_children_maps_sdk_items() -> None:
    """SDK items map onto RawItem tuples; files without a hash fall back to size."""
    provider, mocks = _make_provider()
    mocks.client.list_drive_items = AsyncMock(
        return_value=[
            _folder("d1", "Albums"),
            _file("f1", "track.mp3", 123, xor_hash="xor-1"),
            _file("f2", "no-hash.mp3", 456),
        ]
    )

    items = await provider._api_list_children("folder-id")

    assert items[0] == ("d1", "Albums", True, "folder", None, None)
    assert items[1] == ("f1", "track.mp3", False, "xor-1", 123, "xor-1")
    assert items[2] == ("f2", "no-hash.mp3", False, "456", 456, None)


async def test_list_children_uses_sha_hash_only_as_sidecar_token() -> None:
    """A SHA hash sharpens the sidecar token but never changes the imported-media checksum."""
    provider, mocks = _make_provider()
    mocks.client.list_drive_items = AsyncMock(
        return_value=[
            _file("f1", "a.mp3", 100, sha1_hash="sha-aaa"),
            _file("f2", "b.mp3", 100, sha1_hash="sha-bbb"),  # same size, different content
        ]
    )

    items = await provider._api_list_children("folder-id")

    # the checksum (imported-media compatibility) still falls back to the size, unchanged...
    assert items[0][3] == "100"
    assert items[1][3] == "100"
    # ...but the SHA hash is carried separately, so same-size files are still distinguishable
    assert items[0][5] == "sha-aaa"
    assert items[1][5] == "sha-bbb"


async def test_list_children_checksum_survives_upgrade_when_only_sha_changes() -> None:
    """
    A SHA-only file's checksum stays stable across an upgrade so it is not fully re-imported.

    Before this change, a file with no quickXorHash (business/SharePoint accounts) used the size
    as its checksum. A content change that only alters the SHA hash (same size) must not disturb
    that checksum, even though it should still be visible to sidecar-signature detection.
    """
    provider, mocks = _make_provider()
    mocks.client.list_drive_items = AsyncMock(
        return_value=[_file("f1", "a.mp3", 100, sha1_hash="sha-before")]
    )
    before = await provider._api_list_children("folder-id")

    mocks.client.list_drive_items = AsyncMock(
        return_value=[_file("f1", "a.mp3", 100, sha1_hash="sha-after")]
    )
    after = await provider._api_list_children("folder-id")

    assert before[0][3] == after[0][3] == "100"  # the imported-media checksum is unaffected
    assert before[0][5] != after[0][5]  # the sidecar token still reflects the content change


async def test_list_children_translates_errors() -> None:
    """SDK errors surface as MA errors per the base's hook contract."""
    provider, mocks = _make_provider()
    mocks.client.list_drive_items = AsyncMock(side_effect=AuthenticationError(401, "expired"))
    with pytest.raises(LoginFailed):
        await provider._api_list_children("folder-id")

    mocks.client.list_drive_items = AsyncMock(side_effect=OneDriveException("throttled"))
    with pytest.raises(ProviderUnavailableError):
        await provider._api_list_children("folder-id")


async def test_download_response_forwards_range_header() -> None:
    """The direct Graph download carries both the bearer token and the Range header."""
    provider, mocks = _make_provider()
    mocks.auth.async_get_access_token = AsyncMock(return_value="token-1")
    mocks.mass.http_session.get = AsyncMock(return_value=MagicMock())

    await provider._api_download_response("file-1", {"Range": "bytes=100-"})

    call = mocks.mass.http_session.get.call_args
    assert call.args[0].endswith("/me/drive/items/file-1/content")
    assert call.kwargs["headers"]["Authorization"] == "Bearer token-1"
    assert call.kwargs["headers"]["Range"] == "bytes=100-"


async def test_resolve_root_folder_stores_id_and_name() -> None:
    """A configured folder path resolves to its Graph item ID and display name."""
    provider, mocks = _make_provider(root_folder_id="/My Music/")
    mocks.auth.async_get_access_token = AsyncMock(return_value="token-1")
    response = MagicMock()
    response.status = 200
    response.json = AsyncMock(return_value={"id": "id-1", "name": "My Music", "folder": {}})
    mocks.mass.http_session.get = MagicMock(return_value=_response_cm(response))

    await provider._resolve_root_folder()

    assert provider.root_folder_id == "id-1"
    assert provider.instance_name_postfix == "My Music"
    # the path is addressed root-relative, stripped and url-quoted
    assert mocks.mass.http_session.get.call_args.args[0].endswith("/me/drive/root:/My%20Music")


async def test_resolve_root_folder_not_found() -> None:
    """A path OneDrive doesn't know fails setup with a helpful message."""
    provider, mocks = _make_provider(root_folder_id="Nope")
    mocks.auth.async_get_access_token = AsyncMock(return_value="token-1")
    response = MagicMock()
    response.status = 404
    mocks.mass.http_session.get = MagicMock(return_value=_response_cm(response))

    with pytest.raises(SetupFailedError, match="not found"):
        await provider._resolve_root_folder()


async def test_resolve_root_folder_rejects_non_folder() -> None:
    """A file path configured as the root folder fails setup."""
    provider, mocks = _make_provider(root_folder_id="track.mp3")
    mocks.auth.async_get_access_token = AsyncMock(return_value="token-1")
    response = MagicMock()
    response.status = 200
    response.json = AsyncMock(return_value={"id": "id-1", "name": "track.mp3", "file": {}})
    mocks.mass.http_session.get = MagicMock(return_value=_response_cm(response))

    with pytest.raises(SetupFailedError, match="not a folder"):
        await provider._resolve_root_folder()


async def test_resolve_root_folder_auth_error() -> None:
    """A 401 during folder resolution surfaces as LoginFailed."""
    provider, mocks = _make_provider(root_folder_id="My Music")
    mocks.auth.async_get_access_token = AsyncMock(return_value="token-1")
    response = MagicMock()
    response.status = 401
    response.text = AsyncMock(return_value="token expired")
    mocks.mass.http_session.get = MagicMock(return_value=_response_cm(response))

    with pytest.raises(LoginFailed):
        await provider._resolve_root_folder()


async def test_async_init_with_whole_drive_root() -> None:
    """The 'root' default validates auth with a single call and has no name postfix."""
    provider, mocks = _make_provider(root_folder_id="root")
    mocks.client.get_drive_item = AsyncMock(return_value=MagicMock())

    await provider.handle_async_init()

    mocks.client.get_drive_item.assert_called_once_with("root")
    assert provider.instance_name_postfix is None
    mocks.mass.streams.register_dynamic_route.assert_called_once()


async def test_async_init_verifies_auth() -> None:
    """Bad credentials fail setup with LoginFailed."""
    provider, mocks = _make_provider(root_folder_id="root")
    mocks.client.get_drive_item = AsyncMock(side_effect=AuthenticationError(401, "invalid_grant"))

    with pytest.raises(LoginFailed):
        await provider.handle_async_init()
