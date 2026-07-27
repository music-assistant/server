"""Tests for filesystem provider sync behavior when the storage misbehaves."""

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from music_assistant_models.enums import MediaType

from music_assistant.providers.filesystem_local import LocalFileSystemProvider
from music_assistant.providers.filesystem_local.constants import (
    CONF_ENTRY_CONTENT_TYPE,
    CONF_ENTRY_LIBRARY_SYNC_PLAYLISTS,
    CONF_ENTRY_LIBRARY_SYNC_TRACKS,
)
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


def _enumerate_result(
    *, failed_dirs: int = 0, fatal: bool = False, found_files: set[str] | None = None
) -> Any:
    """Build an _enumerate_files_for_sync stub with the given scan outcome."""

    async def _enumerate(**kwargs: Any) -> None:
        scan_errors: ScanErrors = kwargs["scan_errors"]
        scan_errors.failed_dirs = failed_dirs
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
