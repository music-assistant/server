"""Tests for the beets provider library sync."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import aclosing
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from music_assistant_models.enums import MediaType
from music_assistant_models.media_items import Album

from music_assistant.controllers.tasks.context import calculate_progress
from music_assistant.providers.beets import BeetsProvider
from music_assistant.providers.beets.library import BeetsLibraryError, BeetsRow
from music_assistant.providers.beets.parsers import item_checksum
from tests.providers.beets.beets_db import BeetsDb, album_fields, item_fields
from tests.providers.beets.conftest import INSTANCE_ID, album_prov_id, track_prov_id

MakeProvider = Callable[..., Awaitable[BeetsProvider]]
REPORT_FAILURE = "music_assistant.providers.beets.report_current_task_failure"


async def _stored_checksums(provider: BeetsProvider) -> list[dict[str, str]]:
    """Return provider_mappings rows as a previous sync of the current database would store them."""
    albums = await provider.library.get_albums()
    rows = []
    async for batch in provider.library.iter_items():
        for item in batch:
            album = albums.get(item.album_id) if item.album_id else None
            rows.append(
                {
                    "provider_item_id": track_prov_id(item.id),
                    "details": item_checksum(item, album),
                }
            )
    return rows


def _stub_cleanup(provider: BeetsProvider) -> None:
    """Replace the deletion steps with mocks so a test can assert whether they ran."""
    provider._process_deletions = AsyncMock()  # type: ignore[method-assign]
    provider._process_orphaned_albums_and_artists = AsyncMock()  # type: ignore[method-assign]


async def test_first_sync_imports_every_item(
    make_provider: MakeProvider, beets_db: BeetsDb
) -> None:
    """Without previous checksums every beets item is added as new."""
    album_id = beets_db.add_album(**album_fields())
    beets_db.add_item(**item_fields(album_id=album_id))
    beets_db.add_item(**item_fields(album_id=album_id, track=2, title="Two"))
    beets_db.add_item(**item_fields(title="Loose"))
    provider = await make_provider()

    await provider.sync_library(MediaType.TRACK)

    calls = provider.mass.music.tracks.add_item_to_library.await_args_list  # type: ignore[attr-defined]
    assert sorted(call.args[0].name for call in calls) == ["Loose", "Song", "Two"]
    assert all(call.kwargs["overwrite_existing"] is False for call in calls)


async def test_sync_with_zero_initial_count_does_not_crash_on_progress(
    make_provider: MakeProvider, beets_db: BeetsDb
) -> None:
    """Beets reporting 0 items at count time, with an item arriving before the first batch, syncs."""
    beets_db.add_item(**item_fields())
    provider = await make_provider()
    provider.library.count_items = AsyncMock(return_value=0)  # type: ignore[method-assign]

    def _update_progress(current: int, total: int, _text: str | None = None) -> int | None:
        # a real task context raises when total <= 0, exactly like production
        return calculate_progress(current, total)

    with patch(
        "music_assistant.providers.beets.update_current_task_progress_from_index",
        side_effect=_update_progress,
    ):
        await provider.sync_library(MediaType.TRACK)

    calls = provider.mass.music.tracks.add_item_to_library.await_args_list  # type: ignore[attr-defined]
    assert [call.args[0].name for call in calls] == ["Song"]
    assert provider.sync_running is False


async def test_album_for_falls_back_to_get_album_when_missing_from_pre_read_map(
    make_provider: MakeProvider, beets_db: BeetsDb
) -> None:
    """An album missing from the pre-read albums map is still looked up and set on the track."""
    album_id = beets_db.add_album(**album_fields())
    beets_db.add_item(**item_fields(album_id=album_id))
    provider = await make_provider()
    provider.library.get_albums = AsyncMock(return_value={})  # type: ignore[method-assign]

    await provider.sync_library(MediaType.TRACK)

    calls = provider.mass.music.tracks.add_item_to_library.await_args_list  # type: ignore[attr-defined]
    assert len(calls) == 1
    track = calls[0].args[0]
    assert isinstance(track.album, Album)
    assert track.album.item_id == album_prov_id(album_id)


async def test_second_sync_while_running_is_ignored(
    make_provider: MakeProvider, beets_db: BeetsDb
) -> None:
    """A sync request while one is running does nothing."""
    beets_db.add_item(**item_fields())
    provider = await make_provider()
    provider.sync_running = True
    await provider.sync_library(MediaType.TRACK)
    provider.mass.music.tracks.add_item_to_library.assert_not_awaited()  # type: ignore[attr-defined]


async def test_only_changed_items_are_updated(
    make_provider: MakeProvider, beets_db: BeetsDb
) -> None:
    """Unchanged items are skipped and an edited item is overwritten."""
    album_id = beets_db.add_album(**album_fields())
    beets_db.add_item(**item_fields(album_id=album_id))
    edited = beets_db.add_item(**item_fields(album_id=album_id, track=2, title="Two"))
    provider = await make_provider()
    provider.mass.music.database.get_rows_from_query = AsyncMock(  # type: ignore[method-assign]
        return_value=await _stored_checksums(provider)
    )
    _stub_cleanup(provider)
    beets_db.update_item(edited, title="Two (edited)")

    await provider.sync_library(MediaType.TRACK)

    calls = provider.mass.music.tracks.add_item_to_library.await_args_list  # type: ignore[attr-defined]
    assert [(call.args[0].item_id, call.kwargs["overwrite_existing"]) for call in calls] == [
        (track_prov_id(edited), True)
    ]


async def test_album_and_flex_edits_resync_items(
    make_provider: MakeProvider, beets_db: BeetsDb
) -> None:
    """An album edit re-syncs all its tracks and a flexible attribute re-syncs its item."""
    album_id = beets_db.add_album(**album_fields())
    first = beets_db.add_item(**item_fields(album_id=album_id))
    second = beets_db.add_item(**item_fields(album_id=album_id, track=2))
    loose = beets_db.add_item(**item_fields(title="Loose"))
    provider = await make_provider()
    provider.mass.music.database.get_rows_from_query = AsyncMock(  # type: ignore[method-assign]
        return_value=await _stored_checksums(provider)
    )
    _stub_cleanup(provider)
    beets_db.update_album(album_id, label="New Label")
    beets_db.set_item_flex(loose, "mood", "calm")

    await provider.sync_library(MediaType.TRACK)

    calls = provider.mass.music.tracks.add_item_to_library.await_args_list  # type: ignore[attr-defined]
    assert sorted(call.args[0].item_id for call in calls) == sorted(
        [track_prov_id(first), track_prov_id(second), track_prov_id(loose)]
    )


async def test_items_removed_from_beets_are_deleted(
    make_provider: MakeProvider, beets_db: BeetsDb
) -> None:
    """Items beets no longer has are passed to the deletion step."""
    beets_db.add_item(**item_fields())
    gone = beets_db.add_item(**item_fields(title="Gone"))
    provider = await make_provider()
    provider.mass.music.database.get_rows_from_query = AsyncMock(  # type: ignore[method-assign]
        return_value=await _stored_checksums(provider)
    )
    _stub_cleanup(provider)
    beets_db.delete_item(gone)

    await provider.sync_library(MediaType.TRACK)

    provider._process_deletions.assert_awaited_once_with(  # type: ignore[attr-defined]
        {track_prov_id(gone)}
    )
    provider._process_orphaned_albums_and_artists.assert_awaited_once()  # type: ignore[attr-defined]


async def test_empty_library_does_not_delete_previous_items(make_provider: MakeProvider) -> None:
    """A beets database that suddenly has no items aborts before deleting anything."""
    provider = await make_provider()
    provider.mass.music.database.get_rows_from_query = AsyncMock(  # type: ignore[method-assign]
        return_value=[{"provider_item_id": track_prov_id(1), "details": "x"}]
    )
    _stub_cleanup(provider)

    with patch(REPORT_FAILURE) as report:
        await provider.sync_library(MediaType.TRACK)

    provider._process_deletions.assert_not_awaited()  # type: ignore[attr-defined]
    provider._process_orphaned_albums_and_artists.assert_not_awaited()  # type: ignore[attr-defined]
    report.assert_called_once()


async def test_unreadable_library_does_not_delete(
    make_provider: MakeProvider, beets_db: BeetsDb
) -> None:
    """A locked or unreadable database aborts the sync before deleting anything."""
    beets_db.add_item(**item_fields())
    provider = await make_provider()
    provider.mass.music.database.get_rows_from_query = AsyncMock(  # type: ignore[method-assign]
        return_value=[
            {"provider_item_id": track_prov_id(1), "details": "x"},
            {"provider_item_id": track_prov_id(2), "details": "y"},
        ]
    )
    _stub_cleanup(provider)
    provider.library.iter_items = MagicMock(  # type: ignore[method-assign]
        side_effect=BeetsLibraryError("database is locked")
    )

    with patch(REPORT_FAILURE) as report:
        await provider.sync_library(MediaType.TRACK)

    provider._process_deletions.assert_not_awaited()  # type: ignore[attr-defined]
    provider._process_orphaned_albums_and_artists.assert_not_awaited()  # type: ignore[attr-defined]
    report.assert_called_once()
    assert provider.sync_running is False


async def test_read_error_after_imports_started_does_not_delete(
    make_provider: MakeProvider, beets_db: BeetsDb
) -> None:
    """A read that fails while imports are still running waits for them and deletes nothing."""
    first = beets_db.add_item(**item_fields(title="First"))
    beets_db.add_item(**item_fields(title="Second"))
    provider = await make_provider()
    provider.mass.music.database.get_rows_from_query = AsyncMock(  # type: ignore[method-assign]
        return_value=[{"provider_item_id": track_prov_id(99), "details": "x"}]
    )
    _stub_cleanup(provider)
    async with aclosing(provider.library.iter_items(1)) as batches:
        first_batch = await anext(batches)
    imports = provider.mass.music.tracks.add_item_to_library
    awaited_when_read_failed: list[int] = []

    async def _fail_after_first_batch() -> AsyncGenerator[list[BeetsRow]]:
        yield first_batch
        awaited_when_read_failed.append(imports.await_count)  # type: ignore[attr-defined]
        msg = "database is locked"
        raise BeetsLibraryError(msg)

    provider.library.iter_items = MagicMock(  # type: ignore[method-assign]
        return_value=_fail_after_first_batch()
    )

    with patch(REPORT_FAILURE) as report:
        await provider.sync_library(MediaType.TRACK)

    assert awaited_when_read_failed == [0]
    calls = imports.await_args_list  # type: ignore[attr-defined]
    assert [call.args[0].item_id for call in calls] == [track_prov_id(first)]
    provider._process_deletions.assert_not_awaited()  # type: ignore[attr-defined]
    provider._process_orphaned_albums_and_artists.assert_not_awaited()  # type: ignore[attr-defined]
    report.assert_called_once()
    assert provider.sync_running is False


async def test_failing_item_is_reported_and_not_deleted(
    make_provider: MakeProvider, beets_db: BeetsDb
) -> None:
    """An item that cannot be parsed is reported, not imported and not deleted."""
    ok = beets_db.add_item(**item_fields())
    bad = beets_db.add_item(**item_fields(title="Bad", artists="", artist=""))
    provider = await make_provider()
    provider.mass.music.database.get_rows_from_query = AsyncMock(  # type: ignore[method-assign]
        return_value=[{"provider_item_id": track_prov_id(bad), "details": "old"}]
    )
    _stub_cleanup(provider)

    with patch(REPORT_FAILURE) as report:
        await provider.sync_library(MediaType.TRACK)

    calls = provider.mass.music.tracks.add_item_to_library.await_args_list  # type: ignore[attr-defined]
    assert [call.args[0].item_id for call in calls] == [track_prov_id(ok)]
    assert report.call_count == 1
    provider._process_deletions.assert_not_awaited()  # type: ignore[attr-defined]


async def test_sync_sets_loudness(make_provider: MakeProvider, beets_db: BeetsDb) -> None:
    """ReplayGain feeds the loudness store, and a beets rating does not touch the favorite."""
    item_id = beets_db.add_item(**item_fields(rg_track_gain=-5.0, rg_album_gain=-4.0))
    beets_db.set_item_flex(item_id, "rating", "0.9")
    provider = await make_provider()
    _stub_cleanup(provider)

    await provider.sync_library(MediaType.TRACK)

    provider.mass.music.tracks.set_favorite.assert_not_awaited()  # type: ignore[attr-defined]
    provider.mass.streams.audio_analysis.set_track_loudness.assert_awaited_once_with(  # type: ignore[attr-defined]
        track_prov_id(item_id), INSTANCE_ID, -13.0, -14.0
    )


async def test_process_deletions_only_unmaps_tracks(make_provider: MakeProvider) -> None:
    """Deleted tracks lose their beets mapping; albums and artists are left to the orphan pass."""
    provider = await make_provider()
    music = provider.mass.music
    library_tracks = {track_prov_id(5): MagicMock(item_id=10), track_prov_id(6): None}
    music.tracks.get_library_item_by_prov_id = AsyncMock(  # type: ignore[method-assign]
        side_effect=lambda item_id, _instance: library_tracks[item_id]
    )
    music.albums.remove_item_from_library = AsyncMock()  # type: ignore[method-assign]
    music.artists.remove_item_from_library = AsyncMock()  # type: ignore[method-assign]

    await provider._process_deletions({track_prov_id(5), track_prov_id(6)})

    music.tracks.remove_provider_mapping.assert_awaited_once_with(  # type: ignore[attr-defined]
        10, INSTANCE_ID, track_prov_id(5)
    )
    music.tracks.remove_item_from_library.assert_not_awaited()  # type: ignore[attr-defined]
    music.albums.remove_item_from_library.assert_not_awaited()
    music.artists.remove_item_from_library.assert_not_awaited()


async def test_orphaned_albums_and_artists_lose_this_instances_mappings(
    make_provider: MakeProvider,
) -> None:
    """Albums and artists of this instance without tracks lose only this instance's mappings."""
    provider = await make_provider()
    music = provider.mass.music
    music.database.get_rows_from_query = AsyncMock(  # type: ignore[method-assign]
        side_effect=[[{"item_id": 3}], [{"item_id": 4}]]
    )
    music.albums.remove_provider_mappings = AsyncMock()  # type: ignore[method-assign,misc]
    music.artists.remove_provider_mappings = AsyncMock()  # type: ignore[method-assign,misc]
    music.albums.remove_item_from_library = AsyncMock()  # type: ignore[method-assign]
    music.artists.remove_item_from_library = AsyncMock()  # type: ignore[method-assign]

    await provider._process_orphaned_albums_and_artists()

    music.albums.remove_provider_mappings.assert_awaited_once_with(3, INSTANCE_ID)
    music.artists.remove_provider_mappings.assert_awaited_once_with(4, INSTANCE_ID)
    music.albums.remove_item_from_library.assert_not_awaited()
    music.artists.remove_item_from_library.assert_not_awaited()
    assert all(
        call.args[1] == {"instance_id": INSTANCE_ID}
        for call in music.database.get_rows_from_query.await_args_list
    )


async def test_new_import_waits_for_an_overwrite_reading_the_library_track(
    make_provider: MakeProvider, beets_db: BeetsDb
) -> None:
    """A new item cannot merge into a library track while an overwrite of it is in progress."""
    changed_id = beets_db.add_item(**item_fields(title="Changed"))
    new_id = beets_db.add_item(**item_fields(title="New"))
    provider = await make_provider()
    tracks = provider.mass.music.tracks
    lookup_started = asyncio.Event()
    release_lookup = asyncio.Event()

    async def _slow_lookup(*_args: Any) -> None:
        lookup_started.set()
        await release_lookup.wait()

    tracks.get_library_item_by_prov_id = AsyncMock(side_effect=_slow_lookup)  # type: ignore[method-assign]
    changed = await provider.library.get_item(changed_id)
    new = await provider.library.get_item(new_id)
    assert changed is not None
    assert new is not None

    overwrite = asyncio.create_task(provider._import_item(changed, None, "a", overwrite=True))
    await lookup_started.wait()
    add = asyncio.create_task(provider._import_item(new, None, "b", overwrite=False))
    await asyncio.sleep(0)
    tracks.add_item_to_library.assert_not_awaited()  # type: ignore[attr-defined]

    release_lookup.set()
    await asyncio.gather(overwrite, add)
    assert [
        call.args[0].item_id
        for call in tracks.add_item_to_library.await_args_list  # type: ignore[attr-defined]
    ] == [track_prov_id(changed_id), track_prov_id(new_id)]
