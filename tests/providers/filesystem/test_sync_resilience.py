"""Tests for filesystem provider sync behavior when the storage misbehaves."""

import asyncio
import errno
from typing import Any, Self, cast
from unittest.mock import AsyncMock, MagicMock, patch

from music_assistant_models.enums import MediaType
from music_assistant_models.errors import ProviderUnavailableError

from music_assistant.providers.filesystem_local import _ONDEMAND_NFO_ITEMS, LocalFileSystemProvider
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
    provider.unloading = False
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


async def test_sync_shares_one_ondemand_listing_scope_across_the_whole_batch() -> None:
    """
    Every track processed in a sync shares one on-demand listing/NFO-root memo.

    This is what lets a folder shared by several tracks be listed - and its NFO parsed -
    only once for the whole sync, instead of once per track, whenever the up-front index
    isn't trusted (before it is built, or after an incomplete scan leaves it unready).
    """
    provider = _create_provider()
    item_a = MagicMock(relative_path="Artist/Album/a.flac")
    item_b = MagicMock(relative_path="Artist/Album/b.flac")

    async def _enumerate(**kwargs: Any) -> None:
        kwargs["items_to_process"].extend([(item_a, None), (item_b, None)])
        kwargs["cur_filenames"].update({FOUND_FILE})
        # an incomplete scan leaves the up-front index unready for the whole sync -
        # exactly the scenario this shared batch scope exists for
        kwargs["scan_errors"].failed_dirs = 1

    provider._enumerate_files_for_sync = _enumerate  # type: ignore[method-assign]
    provider.mass.create_task = lambda coro, **_kwargs: asyncio.ensure_future(coro)  # type: ignore[method-assign]
    captured_memos: list[Any] = []

    async def _process_item_async(*_args: Any, **_kwargs: Any) -> bool:
        captured_memos.append(_ONDEMAND_NFO_ITEMS.get())
        return False

    provider._process_item_async = _process_item_async  # type: ignore[method-assign]

    await provider.sync_library(MediaType.TRACK)

    assert len(captured_memos) == 2
    assert captured_memos[0] is not None
    assert captured_memos[0] is captured_memos[1]


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


async def test_nfo_index_not_marked_ready_after_an_incomplete_scan() -> None:
    """
    An incomplete scan must not make the sync-wide NFO index authoritative.

    Some folders/files failing to read means the walk may have missed an NFO that does
    exist on disk; trusting the resulting (partial) index anyway could make a changed
    track wrongly resolve to a synthetic identity instead of retrying a direct lookup.
    A cleanup at the very end of the sync always resets the flag, so its in-sync value
    is captured via a `TaskManager` stand-in, the last thing entered before that cleanup.
    """
    provider = _create_provider()
    provider._enumerate_files_for_sync = _enumerate_result(  # type: ignore[method-assign]
        failed_dirs=1, found_files={FOUND_FILE}
    )
    captured_ready_states: list[bool] = []

    class _CapturingTaskManager:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            captured_ready_states.append(provider._sync_nfo_index_ready)

        async def __aenter__(self) -> Self:
            return self

        async def __aexit__(self, *_exc: object) -> None:
            return None

        async def create_task_with_limit(self, _coro: Any) -> None:
            """Discard the task; nothing to process in this scenario."""

    with patch("music_assistant.providers.filesystem_local.TaskManager", _CapturingTaskManager):
        await provider.sync_library(MediaType.TRACK)

    assert captured_ready_states == [False]


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
    cast("MagicMock", provider.mass.call_later).assert_called_once()


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

    await provider.unload()

    cast("MagicMock", provider.mass.cancel_timer).assert_called_once_with(
        provider._availability_probe_id
    )
    # a check that already started is a task under the same id, and must stop too
    cast("MagicMock", provider.mass.cancel_task).assert_called_once_with(
        provider._availability_probe_id
    )


async def test_probe_does_not_rearm_after_the_provider_is_unloaded() -> None:
    """A check still running when the provider is torn down must not schedule another."""
    provider = _create_unavailable_provider()
    call_later = cast("MagicMock", provider.mass.call_later)

    async def _unload_midway() -> bool:
        # the provider is torn down while this check is in flight
        provider.unloading = True
        return False

    provider._is_reachable = _unload_midway  # type: ignore[method-assign]

    await provider._probe_availability()

    assert call_later.call_count == 0
