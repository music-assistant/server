"""Tests for filesystem provider sync behavior when the storage misbehaves."""

import errno
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

from music_assistant_models.enums import MediaType
from music_assistant_models.errors import ProviderUnavailableError

from music_assistant.providers.filesystem_local import LocalFileSystemProvider
from music_assistant.providers.filesystem_local.constants import (
    CONF_ENTRY_CONTENT_TYPE,
    CONF_ENTRY_LIBRARY_SYNC_PLAYLISTS,
    CONF_ENTRY_LIBRARY_SYNC_TRACKS,
)
from music_assistant.providers.filesystem_local.cue import make_cue_track_id
from music_assistant.providers.filesystem_local.helpers import ScanErrors

# two previously indexed tracks; the scans below only find the first one, so the
# second is what a deletion pass would remove from the library
FOUND_FILE = "Artist/Album/track1.mp3"
MISSING_FILE = "Artist/Album/track2.mp3"


def _create_provider() -> LocalFileSystemProvider:
    """Create a music LocalFileSystemProvider with mocked dependencies."""
    config_values = {
        CONF_ENTRY_CONTENT_TYPE.key: "music",
        CONF_ENTRY_LIBRARY_SYNC_TRACKS.key: True,
        CONF_ENTRY_LIBRARY_SYNC_PLAYLISTS.key: True,
    }
    mock_config = MagicMock()
    mock_config.get_value = MagicMock(side_effect=lambda key: config_values.get(key))

    with patch.object(LocalFileSystemProvider, "__init__", lambda *_a, **_kw: None):
        provider = LocalFileSystemProvider.__new__(LocalFileSystemProvider)

    provider.config = mock_config
    provider.media_content_type = "music"
    provider.sync_running = False
    provider.logger = MagicMock()
    provider.available = True
    provider.mass = MagicMock()
    provider.mass.music.database.get_rows_from_query = AsyncMock(
        return_value=[
            {"provider_item_id": FOUND_FILE, "details": "1"},
            {"provider_item_id": MISSING_FILE, "details": "1"},
        ]
    )
    provider._process_deletions = AsyncMock()  # type: ignore[method-assign]
    provider._process_orphaned_albums_and_artists = AsyncMock()  # type: ignore[method-assign]
    provider._set_available = MagicMock()  # type: ignore[method-assign]
    return provider


def _create_unavailable_provider() -> LocalFileSystemProvider:
    """Create a provider already flagged down, with real availability handling."""
    with patch.object(LocalFileSystemProvider, "__init__", lambda *_a, **_kw: None):
        provider = LocalFileSystemProvider.__new__(LocalFileSystemProvider)
    provider.config = MagicMock()
    provider.logger = MagicMock()
    provider.mass = MagicMock()
    provider.base_path = "/media"
    provider.available = False
    return provider


def _enumerate_result(
    *,
    failed_dirs: int = 0,
    failed_entries: int = 0,
    fatal: bool = False,
    found_files: set[str] | None = None,
) -> Any:
    """Build an _enumerate_files_for_sync stub with the given scan outcome."""

    async def _enumerate(**kwargs: Any) -> None:
        scan_errors: ScanErrors = kwargs["scan_errors"]
        scan_errors.failed_dirs = failed_dirs
        scan_errors.failed_entries = failed_entries
        if fatal:
            scan_errors.fatal = OSError("storage gone")
        kwargs["cur_filenames"].update(found_files or set())

    return AsyncMock(side_effect=_enumerate)


async def test_deletions_run_on_clean_scan() -> None:
    """A scan without errors processes deletions as usual."""
    provider = _create_provider()
    provider._enumerate_files_for_sync = _enumerate_result(  # type: ignore[method-assign]
        found_files={FOUND_FILE}
    )

    await provider.sync_library(MediaType.TRACK)

    provider._process_deletions.assert_awaited_once_with({MISSING_FILE})  # type: ignore[attr-defined]
    provider._process_orphaned_albums_and_artists.assert_awaited_once()  # type: ignore[attr-defined]


async def test_deletions_skipped_when_directories_failed() -> None:
    """A scan that could not read some directories must not delete their content."""
    provider = _create_provider()
    provider._enumerate_files_for_sync = _enumerate_result(  # type: ignore[method-assign]
        failed_dirs=3, found_files={FOUND_FILE}
    )

    await provider.sync_library(MediaType.TRACK)

    provider._process_deletions.assert_not_called()  # type: ignore[attr-defined]
    provider._process_orphaned_albums_and_artists.assert_not_called()  # type: ignore[attr-defined]
    # the storage itself is reachable, so the provider stays available
    provider._set_available.assert_called_once_with(True)  # type: ignore[attr-defined]


async def test_deletions_skipped_when_files_failed() -> None:
    """A scan that could not read some files must not delete them either."""
    provider = _create_provider()
    provider._enumerate_files_for_sync = _enumerate_result(  # type: ignore[method-assign]
        failed_entries=2, found_files={FOUND_FILE}
    )

    await provider.sync_library(MediaType.TRACK)

    provider._process_deletions.assert_not_called()  # type: ignore[attr-defined]
    provider._process_orphaned_albums_and_artists.assert_not_called()  # type: ignore[attr-defined]
    provider._set_available.assert_called_once_with(True)  # type: ignore[attr-defined]


async def test_failed_item_is_kept_in_the_scan_result() -> None:
    """A file that cannot be processed stays in the scan result so it is not deleted."""
    provider = _create_provider()
    provider._sync_tracks = True
    cur_filenames: set[str] = set()
    item = MagicMock()
    item.ext = "mp3"
    item.relative_path = MISSING_FILE
    item.absolute_path = f"/media/{MISSING_FILE}"

    with patch(
        "music_assistant.providers.filesystem_local.async_parse_tags",
        AsyncMock(side_effect=OSError(errno.EIO, "i/o error")),
    ):
        result = await provider._process_item_async(item, None, cur_filenames)

    assert result is False
    # the file is still on disk, so the deletion step must not treat it as removed
    assert cur_filenames == {MISSING_FILE}


async def test_failed_cue_keeps_its_previous_tracks() -> None:
    """A CUE sheet that cannot be parsed keeps the track ids of the previous scan."""
    provider = _create_provider()
    cue_path = "Artist/Album/album.cue"
    cue_tracks = {make_cue_track_id(cue_path, 1), make_cue_track_id(cue_path, 2)}
    cur_filenames: set[str] = set()
    item = MagicMock()
    item.ext = "cue"
    item.relative_path = cue_path
    item.absolute_path = f"/media/{cue_path}"
    provider._cue = MagicMock()
    provider._cue.parse_tracks = AsyncMock(side_effect=OSError(errno.EIO, "i/o error"))

    result = await provider._process_item_async(
        item, None, cur_filenames, prev_filenames={*cue_tracks, MISSING_FILE}
    )

    assert result is False
    # without the track ids the deletion step would drop every track of the album
    assert cur_filenames == {cue_path, *cue_tracks}


async def test_sync_aborts_on_fatal_scan_error() -> None:
    """A scan aborted by the circuit breaker skips deletions and flags the provider down."""
    provider = _create_provider()
    provider._enumerate_files_for_sync = _enumerate_result(  # type: ignore[method-assign]
        failed_dirs=20, fatal=True
    )

    await provider.sync_library(MediaType.TRACK)

    provider._process_deletions.assert_not_called()  # type: ignore[attr-defined]
    provider._process_orphaned_albums_and_artists.assert_not_called()  # type: ignore[attr-defined]
    provider._set_available.assert_called_once_with(False)  # type: ignore[attr-defined]


async def test_aborted_sync_starts_checking_for_the_storage() -> None:
    """A scan aborted by the circuit breaker leaves a reachability check running."""
    provider = _create_provider()
    provider.base_path = "/media"
    # exercise the real availability handling rather than the mock _create_provider installs
    provider._set_available = LocalFileSystemProvider._set_available.__get__(  # type: ignore[method-assign]
        provider, LocalFileSystemProvider
    )
    provider._enumerate_files_for_sync = _enumerate_result(  # type: ignore[method-assign]
        failed_dirs=20, fatal=True
    )

    await provider.sync_library(MediaType.TRACK)

    assert provider.available is False
    assert provider._availability_probe is not None


async def test_probe_keeps_waiting_while_the_storage_is_gone() -> None:
    """A provider whose storage is still missing stays down and checks again later."""
    provider = _create_unavailable_provider()
    call_later = cast("MagicMock", provider.mass.call_later)
    provider._is_reachable = AsyncMock(return_value=False)  # type: ignore[method-assign]

    await provider._probe_availability()

    assert provider.available is False
    assert call_later.call_count == 1


async def test_probe_brings_the_provider_back() -> None:
    """The provider becomes available again as soon as its storage can be read."""
    provider = _create_unavailable_provider()
    call_later = cast("MagicMock", provider.mass.call_later)
    provider._is_reachable = AsyncMock(return_value=True)  # type: ignore[method-assign]

    await provider._probe_availability()

    assert provider.available is True
    # recovered, so no further check is scheduled
    assert call_later.call_count == 0


async def test_probe_treats_an_error_as_still_unreachable() -> None:
    """A provider whose reachability check raises keeps waiting instead of coming back."""
    provider = _create_unavailable_provider()
    call_later = cast("MagicMock", provider.mass.call_later)
    provider._is_reachable = AsyncMock(  # type: ignore[method-assign]
        side_effect=ProviderUnavailableError("cloud api down")
    )

    await provider._probe_availability()

    assert provider.available is False
    assert call_later.call_count == 1


async def test_unload_stops_checking() -> None:
    """Unloading a provider that went down leaves no timer behind."""
    provider = _create_unavailable_provider()
    provider._schedule_availability_probe()
    timer = provider._availability_probe

    await provider.unload()

    assert timer is not None
    timer.cancel.assert_called_once()  # type: ignore[attr-defined]
    assert provider._availability_probe is None
