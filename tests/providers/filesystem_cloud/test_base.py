"""Tests for the CloudFileSystemProvider base class."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock
from urllib.parse import quote

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request
from music_assistant_models.errors import MediaNotFoundError, ProviderUnavailableError

from music_assistant.providers.filesystem_cloud.base import CloudFileSystemProvider, RawItem
from music_assistant.providers.filesystem_local.helpers import FileSystemItem

if TYPE_CHECKING:
    from aiohttp import ClientResponse

BASE_URL = "http://ma.local:8097"
INSTANCE_ID = "cloud--test"
ROOT_ID = "root-id"

# a small two-level tree: /Artist/track.mp3 + /cover.jpg
TREE: dict[str, list[RawItem]] = {
    ROOT_ID: [
        ("artist-id", "Artist", True, "2026-01-01", None),
        ("cover-id", "cover.jpg", False, "2026-01-01", 4),
        ("notes-id", "notes.txt", False, "2026-01-01", 2),
    ],
    "artist-id": [
        ("track-id", "track.mp3", False, "2026-01-02", 1000),
    ],
}
FILE_DATA = {"cover-id": b"image-bytes", "track-id": b"audio-bytes", "notes-id": b"n"}


class _StubCloudProvider(CloudFileSystemProvider):
    """CloudFileSystemProvider wired to an in-memory folder tree."""

    tree: dict[str, list[RawItem]]
    file_data: dict[str, bytes]
    fail_list: set[str]
    fail_download: set[str]
    list_calls: list[str]
    download_headers: dict[str, str] | None

    async def _api_list_children(self, folder_id: str) -> list[RawItem]:
        self.list_calls.append(folder_id)
        if folder_id in self.fail_list:
            raise ProviderUnavailableError(f"list failed: {folder_id}")
        return self.tree.get(folder_id, [])

    async def _api_download_bytes(self, file_id: str) -> bytes:
        if file_id in self.fail_download:
            raise ProviderUnavailableError(f"download failed: {file_id}")
        return self.file_data[file_id]

    async def _api_download_response(self, file_id: str, headers: dict[str, str]) -> ClientResponse:
        self.download_headers = headers
        raise ProviderUnavailableError(f"download failed: {file_id}")


def _make_provider(tree: dict[str, list[RawItem]] | None = None) -> _StubCloudProvider:
    provider = _StubCloudProvider.__new__(_StubCloudProvider)
    provider.root_folder_id = ROOT_ID
    provider._dir_cache = {}
    provider._dir_cache_expiry = {}
    provider._unregister_stream_route = None
    provider.logger = MagicMock()
    provider.media_content_type = "music"
    provider.config = MagicMock()
    provider.config.instance_id = INSTANCE_ID
    provider.config.get_value = MagicMock(return_value=False)
    provider.mass = MagicMock()
    provider.mass.streams.base_url = BASE_URL
    provider.tree = TREE if tree is None else tree
    provider.file_data = FILE_DATA
    provider.fail_list = set()
    provider.fail_download = set()
    provider.list_calls = []
    provider.download_headers = None
    return provider


async def _run_enumerate(
    provider: _StubCloudProvider,
    *,
    root_scan_errors: list[OSError] | None = None,
) -> None:
    """Drive _enumerate_files_for_sync with empty sync buckets."""
    await provider._enumerate_files_for_sync(
        file_checksums={},
        cue_file_checksums={},
        cur_filenames=set(),
        items_to_process=[],
        unchanged_cue_items=[],
        cue_stems=set(),
        root_scan_errors=root_scan_errors if root_scan_errors is not None else [],
    )


async def test_scandir_converts_raw_items() -> None:
    """Raw API entries become FileSystemItems; files stream via the dynamic URL."""
    provider = _make_provider()

    items = {item.filename: item for item in await provider._scandir("")}

    folder = items["Artist"]
    assert folder.is_dir
    assert folder.relative_path == "Artist"
    assert folder.absolute_path == ""
    image = items["cover.jpg"]
    assert not image.is_dir
    assert image.checksum == "2026-01-01"
    assert image.file_size == 4
    assert image.absolute_path == f"{BASE_URL}/{INSTANCE_ID}_stream?path={quote('cover.jpg')}"
    # the listing is cached for path lookups
    assert "cover.jpg" in provider._dir_cache[""]


async def test_scandir_skips_duplicate_names() -> None:
    """Clouds may allow duplicate names in a folder; only the first wins."""
    provider = _make_provider(
        tree={ROOT_ID: [("id1", "a.mp3", False, "1", 1), ("id2", "a.mp3", False, "2", 2)]}
    )
    logger = MagicMock()
    provider.logger = logger

    items = await provider._scandir("")

    assert len(items) == 1
    assert provider._dir_cache[""]["a.mp3"][0] == "id1"
    logger.warning.assert_called_once()


async def test_scandir_sanitizes_slashes_in_names() -> None:
    """Slashes in cloud file names would corrupt the path scheme."""
    provider = _make_provider(tree={ROOT_ID: [("id1", "AC/DC", True, "1", None)]})

    items = await provider._scandir("")

    assert items[0].relative_path == "AC_DC"


async def test_scandir_serves_recent_listing_from_cache() -> None:
    """Re-listing a folder within the TTL must not hit the API again."""
    provider = _make_provider()

    first = await provider._scandir("")
    second = await provider._scandir("")

    assert provider.list_calls == [ROOT_ID]
    assert [item.relative_path for item in second] == [item.relative_path for item in first]


async def test_scandir_refetches_after_ttl_expiry() -> None:
    """An expired listing is fetched fresh from the API."""
    provider = _make_provider()

    await provider._scandir("")
    provider._dir_cache_expiry[""] = 0.0
    await provider._scandir("")

    assert provider.list_calls == [ROOT_ID, ROOT_ID]


async def test_sync_walk_bypasses_listing_cache() -> None:
    """A sync must always fetch fresh listings, even right after browsing."""
    provider = _make_provider()
    provider._classify_scan_item = MagicMock()  # type: ignore[method-assign]

    await provider._scandir("")
    await _run_enumerate(provider)

    # root listed once by browse and again (fresh) by the sync walk
    assert provider.list_calls.count(ROOT_ID) == 2


async def test_lookup_answers_from_cache() -> None:
    """Resolving the same path twice must not re-list the folder."""
    provider = _make_provider()

    first = await provider.resolve("Artist/track.mp3")
    calls_after_first = len(provider.list_calls)
    second = await provider.resolve("Artist/track.mp3")

    assert first.relative_path == second.relative_path == "Artist/track.mp3"
    # resolving walked root + Artist once; the second resolve was free
    assert provider.list_calls == [ROOT_ID, "artist-id"]
    assert len(provider.list_calls) == calls_after_first


async def test_parent_folder_helpers_usable_on_cloud_items() -> None:
    """
    Parent-folder helpers must work although absolute_path is a stream URL.

    The audiobook/podcast logic in LocalFileSystemProvider scans an item's
    parent folder and uses its name; both must be based on the relative path.
    """
    provider = _make_provider()

    track = await provider.resolve("Artist/track.mp3")

    assert track.relative_parent_path == "Artist"
    assert track.parent_name == "Artist"
    items = await provider._scandir(track.relative_parent_path)
    assert [item.filename for item in items] == ["track.mp3"]


async def test_resolve_id_of_root_and_unknown_path() -> None:
    """The empty path is the root folder; unknown paths raise MediaNotFoundError."""
    provider = _make_provider()

    assert await provider._resolve_id("") == ROOT_ID
    with pytest.raises(MediaNotFoundError):
        await provider._resolve_id("Artist/nope.mp3")


async def test_resolve_missing_raises() -> None:
    """Resolving a missing path raises MediaNotFoundError."""
    provider = _make_provider()

    with pytest.raises(MediaNotFoundError):
        await provider.resolve("missing.mp3")


async def test_exists() -> None:
    """exists() is True for present paths, False for absent/empty ones."""
    provider = _make_provider()

    assert await provider.exists("Artist/track.mp3")
    assert not await provider.exists("Artist/nope.mp3")
    assert not await provider.exists("")


async def test_exists_returns_false_on_api_failure() -> None:
    """An API failure while probing must read as 'does not exist', not crash."""
    provider = _make_provider()
    provider.fail_list = {ROOT_ID}

    assert not await provider.exists("Artist/track.mp3")


def test_normalize_path() -> None:
    """Paths are stripped and ./.. segments collapsed."""
    provider = _make_provider()

    assert provider._normalize_path("/Artist/track.mp3/") == "Artist/track.mp3"
    assert provider._normalize_path("Artist/./track.mp3") == "Artist/track.mp3"
    assert provider._normalize_path("Artist/../cover.jpg") == "cover.jpg"
    assert provider._normalize_path(".") == ""


async def test_read_file() -> None:
    """_read_file returns the file bytes for a relative path."""
    provider = _make_provider()

    assert await provider._read_file("cover.jpg") == b"image-bytes"


async def test_read_file_failure_maps_to_media_not_found() -> None:
    """A failed download surfaces as MediaNotFoundError to the inherited callers."""
    provider = _make_provider()
    provider.fail_download = {"cover-id"}

    with pytest.raises(MediaNotFoundError):
        await provider._read_file("cover.jpg")


async def test_resolve_image_strips_cache_suffix() -> None:
    """The ?cs= suffix added for embedded images must be dropped before lookup."""
    provider = _make_provider()

    assert await provider.resolve_image("cover.jpg?cs=12345") == b"image-bytes"


async def test_resolve_image_extracts_embedded_art(monkeypatch: pytest.MonkeyPatch) -> None:
    """An audio file path means embedded art, extracted over the stream URL."""
    provider = _make_provider()
    extract = AsyncMock(return_value=b"art-bytes")
    monkeypatch.setattr(
        "music_assistant.providers.filesystem_cloud.base.get_embedded_image", extract
    )

    result = await provider.resolve_image("Artist/track.mp3?cs=999")

    assert result == b"art-bytes"
    extract.assert_awaited_once_with(provider._stream_url("Artist/track.mp3"))


async def test_resolve_image_embedded_art_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """No embedded art in the audio file raises MediaNotFoundError."""
    provider = _make_provider()
    monkeypatch.setattr(
        "music_assistant.providers.filesystem_cloud.base.get_embedded_image",
        AsyncMock(return_value=None),
    )

    with pytest.raises(MediaNotFoundError):
        await provider.resolve_image("Artist/track.mp3")


async def test_enumerate_classifies_supported_files_only() -> None:
    """The sync walk visits all folders and classifies only supported extensions."""
    provider = _make_provider()
    provider._classify_scan_item = MagicMock()  # type: ignore[method-assign]

    await _run_enumerate(provider)

    # cover.jpg and notes.txt are not sync candidates; folder art is handled separately
    classified = [
        call.args[0].relative_path for call in provider._classify_scan_item.call_args_list
    ]
    assert classified == ["Artist/track.mp3"]


async def test_enumerate_subfolder_error_is_skipped() -> None:
    """A failing subfolder is logged and skipped; the rest of the sync continues."""
    provider = _make_provider(
        tree={
            ROOT_ID: [
                ("bad-id", "Bad", True, "1", None),
                ("artist-id", "Artist", True, "1", None),
            ],
            "artist-id": [("track-id", "track.mp3", False, "1", 1)],
        }
    )
    provider.fail_list = {"bad-id"}
    provider._classify_scan_item = MagicMock()  # type: ignore[method-assign]
    logger = MagicMock()
    provider.logger = logger
    root_scan_errors: list[OSError] = []

    await _run_enumerate(provider, root_scan_errors=root_scan_errors)

    assert not root_scan_errors
    logger.warning.assert_called_once()
    assert provider._classify_scan_item.call_count == 1


async def test_enumerate_root_error_aborts() -> None:
    """A root-level listing failure is reported so the sync aborts."""
    provider = _make_provider()
    provider.fail_list = {ROOT_ID}
    root_scan_errors: list[OSError] = []

    await _run_enumerate(provider, root_scan_errors=root_scan_errors)

    assert len(root_scan_errors) == 1


async def test_enumerate_stops_on_directory_cycle() -> None:
    """A path re-appearing during the walk must be visited only once."""
    provider = _make_provider()
    listing = {
        "": [FileSystemItem("A", "A", "", is_dir=True)],
        "A": [FileSystemItem("B", "A/B", "", is_dir=True)],
        "A/B": [FileSystemItem("A", "A", "", is_dir=True)],
    }
    scanned: list[str] = []

    async def fake_scandir(path: str, use_cache: bool = True) -> list[FileSystemItem]:
        assert use_cache is False
        scanned.append(path)
        return listing[path]

    provider._scandir = AsyncMock(side_effect=fake_scandir)  # type: ignore[method-assign]

    await _run_enumerate(provider)

    assert sorted(scanned) == ["", "A", "A/B"]


async def test_stream_request_without_path_is_bad_request() -> None:
    """The stream route rejects requests without a path parameter."""
    provider = _make_provider()
    request = make_mocked_request("GET", f"/{INSTANCE_ID}_stream")

    with pytest.raises(web.HTTPBadRequest):
        await provider._handle_stream_request(request)


async def test_stream_request_unknown_path_is_not_found() -> None:
    """The stream route returns 404 for paths that don't resolve."""
    provider = _make_provider()
    request = make_mocked_request("GET", f"/{INSTANCE_ID}_stream?path=nope.mp3")

    with pytest.raises(web.HTTPNotFound):
        await provider._handle_stream_request(request)


@pytest.mark.parametrize("path", ["cover.jpg", "notes.txt", "playlist.m3u"])
async def test_stream_request_non_audio_extension_is_not_found(path: str) -> None:
    """The stream route refuses non-audio files so it can't leak arbitrary cloud files."""
    provider = _make_provider()
    request = make_mocked_request("GET", f"/{INSTANCE_ID}_stream?path={quote(path)}")

    with pytest.raises(web.HTTPNotFound):
        await provider._handle_stream_request(request)

    # rejected before any cloud API lookup
    assert provider.list_calls == []


async def test_stream_request_forwards_range_and_maps_api_failure() -> None:
    """The Range header is forwarded to the download; API failure becomes a 502."""
    provider = _make_provider()
    request = make_mocked_request(
        "GET",
        f"/{INSTANCE_ID}_stream?path={quote('Artist/track.mp3')}",
        headers={"Range": "bytes=100-"},
    )

    with pytest.raises(web.HTTPBadGateway):
        await provider._handle_stream_request(request)

    assert provider.download_headers == {"Range": "bytes=100-"}


async def test_post_init_registers_stream_route_and_unload_removes_it() -> None:
    """_post_init registers the dynamic stream route; unload unregisters it."""
    provider = _make_provider()
    unregister = MagicMock()
    provider.mass.streams.register_dynamic_route = MagicMock(return_value=unregister)

    await provider._post_init()

    provider.mass.streams.register_dynamic_route.assert_called_once()
    assert provider.mass.streams.register_dynamic_route.call_args.args[0] == (
        f"/{INSTANCE_ID}_stream"
    )

    await provider.unload()
    unregister.assert_called_once()


async def test_unload_without_registered_route() -> None:
    """Unload before _post_init (e.g. failed setup) must not raise."""
    provider = _make_provider()

    await provider.unload()


# tree with a playlist folder next to the music: /Playlists/list.m3u + /Artist/track.mp3
PLAYLIST_TREE: dict[str, list[RawItem]] = {
    ROOT_ID: [
        ("playlists-id", "Playlists", True, "2026-01-01", None),
        ("artist-id", "Artist", True, "2026-01-01", None),
    ],
    "playlists-id": [
        ("list-id", "list.m3u", False, "2026-01-01", 10),
    ],
    "artist-id": [
        ("track-id", "track.mp3", False, "2026-01-02", 1000),
    ],
}


def _make_playlist_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[_StubCloudProvider, MagicMock]:
    """Return a provider on PLAYLIST_TREE with tag parsing and track building stubbed."""
    provider = _make_provider(tree=PLAYLIST_TREE)
    # the inherited implementation lives in (and imports from) filesystem_local
    monkeypatch.setattr(
        "music_assistant.providers.filesystem_local.async_parse_tags",
        AsyncMock(return_value=MagicMock()),
    )
    track = MagicMock(name="track")
    provider._parse_track = AsyncMock(return_value=track)  # type: ignore[method-assign]
    return provider, track


async def test_parse_playlist_line_relative_to_playlist_folder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A line relative to the playlist's own folder resolves through the cloud lookup."""
    provider, track = _make_playlist_provider(monkeypatch)

    result = await provider._parse_playlist_line("../Artist/track.mp3", "Playlists")

    assert result is track
    file_item = provider._parse_track.await_args.args[0]  # type: ignore[attr-defined]
    assert file_item.relative_path == "Artist/track.mp3"


async def test_parse_playlist_line_sibling_of_playlist(monkeypatch: pytest.MonkeyPatch) -> None:
    """A bare filename next to the playlist file resolves relative to its folder."""
    provider, track = _make_playlist_provider(monkeypatch)

    assert await provider._parse_playlist_line("track.mp3", "Artist") is track


async def test_parse_playlist_line_relative_to_root(monkeypatch: pytest.MonkeyPatch) -> None:
    """A root-relative line still resolves after the playlist-folder candidate misses."""
    provider, track = _make_playlist_provider(monkeypatch)

    assert await provider._parse_playlist_line("Artist/track.mp3", "Playlists") is track


async def test_parse_playlist_line_missing_file_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A line pointing at a nonexistent file logs a warning instead of raising."""
    provider, _ = _make_playlist_provider(monkeypatch)

    assert await provider._parse_playlist_line("Missing/nope.mp3", "Playlists") is None
    provider.logger.warning.assert_called_once()  # type: ignore[attr-defined]
