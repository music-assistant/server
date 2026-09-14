"""Tests for the beets provider library sync."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from unittest.mock import AsyncMock, MagicMock, patch

from music_assistant_models.enums import MediaType

from music_assistant.providers.beets import BeetsProvider
from music_assistant.providers.beets.library import BeetsLibraryError
from music_assistant.providers.beets.parsers import item_checksum
from tests.providers.beets.beets_db import BeetsDb, album_fields, item_fields
from tests.providers.beets.conftest import INSTANCE_ID

MakeProvider = Callable[..., Awaitable[BeetsProvider]]
REPORT_FAILURE = "music_assistant.providers.beets.report_current_task_failure"


async def _stored_checksums(provider: BeetsProvider) -> list[dict[str, str]]:
    """Return provider_mappings rows as a previous sync of the current database would store them."""
    albums = await provider.library.get_albums()
    rows = []
    async for batch in provider.library.iter_items():
        for item in batch:
            album = albums.get(item.album_id) if item.album_id else None
            rows.append({"provider_item_id": str(item.id), "details": item_checksum(item, album)})
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


async def test_artist_and_album_passes_do_nothing(
    make_provider: MakeProvider, beets_db: BeetsDb
) -> None:
    """Artists and albums are imported with their tracks, so their passes are no-ops."""
    beets_db.add_item(**item_fields())
    provider = await make_provider()
    await provider.sync_library(MediaType.ARTIST)
    await provider.sync_library(MediaType.ALBUM)
    provider.mass.music.tracks.add_item_to_library.assert_not_awaited()  # type: ignore[attr-defined]


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
        (str(edited), True)
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
        [str(first), str(second), str(loose)]
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

    provider._process_deletions.assert_awaited_once_with({str(gone)})  # type: ignore[attr-defined]
    provider._process_orphaned_albums_and_artists.assert_awaited_once()  # type: ignore[attr-defined]


async def test_empty_library_does_not_delete_previous_items(make_provider: MakeProvider) -> None:
    """A beets database that suddenly has no items aborts before deleting anything."""
    provider = await make_provider()
    provider.mass.music.database.get_rows_from_query = AsyncMock(  # type: ignore[method-assign]
        return_value=[{"provider_item_id": "1", "details": "x"}]
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
            {"provider_item_id": "1", "details": "x"},
            {"provider_item_id": "2", "details": "y"},
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


async def test_failing_item_is_reported_and_not_deleted(
    make_provider: MakeProvider, beets_db: BeetsDb
) -> None:
    """An item that cannot be parsed is reported, not imported and not deleted."""
    ok = beets_db.add_item(**item_fields())
    bad = beets_db.add_item(**item_fields(title="Bad", artists="", artist=""))
    provider = await make_provider()
    provider.mass.music.database.get_rows_from_query = AsyncMock(  # type: ignore[method-assign]
        return_value=[{"provider_item_id": str(bad), "details": "old"}]
    )
    _stub_cleanup(provider)

    with patch(REPORT_FAILURE) as report:
        await provider.sync_library(MediaType.TRACK)

    calls = provider.mass.music.tracks.add_item_to_library.await_args_list  # type: ignore[attr-defined]
    assert [call.args[0].item_id for call in calls] == [str(ok)]
    assert report.call_count == 1
    provider._process_deletions.assert_not_awaited()  # type: ignore[attr-defined]


async def test_sync_sets_favorite_and_loudness(
    make_provider: MakeProvider, beets_db: BeetsDb
) -> None:
    """A rating above the threshold marks a favorite and ReplayGain feeds the loudness store."""
    item_id = beets_db.add_item(**item_fields(rg_track_gain=-5.0, rg_album_gain=-4.0))
    beets_db.set_item_flex(item_id, "rating", "0.9")
    provider = await make_provider(favorite_rating_threshold=0.8)
    _stub_cleanup(provider)

    await provider.sync_library(MediaType.TRACK)

    provider.mass.music.tracks.set_favorite.assert_awaited_once_with(  # type: ignore[attr-defined]
        1, True
    )
    provider.mass.streams.audio_analysis.set_track_loudness.assert_awaited_once_with(  # type: ignore[attr-defined]
        str(item_id), INSTANCE_ID, -13.0, -14.0
    )


async def test_process_deletions_removes_tracks_and_emptied_parents(
    make_provider: MakeProvider,
) -> None:
    """Deleted tracks are removed, plus albums and artists left with nothing in the library."""
    provider = await make_provider()
    music = provider.mass.music
    library_track = MagicMock(
        item_id=10, album=MagicMock(item_id=20), artists=[MagicMock(item_id=30)]
    )
    music.tracks.get_library_item_by_prov_id = AsyncMock(  # type: ignore[method-assign]
        return_value=library_track
    )
    music.albums.get_library_item = AsyncMock(  # type: ignore[method-assign]
        return_value=MagicMock(artists=[MagicMock(item_id=31)])
    )
    music.albums.tracks = AsyncMock(return_value=[])  # type: ignore[method-assign]
    music.albums.remove_item_from_library = AsyncMock()  # type: ignore[method-assign]
    music.artists.albums = AsyncMock(return_value=[])  # type: ignore[method-assign]
    music.artists.tracks = AsyncMock(  # type: ignore[method-assign]
        side_effect=lambda artist_id, _provider: [] if artist_id == 30 else [MagicMock()]
    )
    music.artists.remove_item_from_library = AsyncMock()  # type: ignore[method-assign]

    await provider._process_deletions({"5"})

    music.tracks.get_library_item_by_prov_id.assert_awaited_once_with("5", INSTANCE_ID)
    music.tracks.remove_item_from_library.assert_awaited_once_with(  # type: ignore[attr-defined]
        10
    )
    music.albums.remove_item_from_library.assert_awaited_once_with(20)
    music.artists.remove_item_from_library.assert_awaited_once_with(30)


async def test_orphaned_albums_and_artists_are_removed(make_provider: MakeProvider) -> None:
    """Albums and artists of this instance without tracks are removed."""
    provider = await make_provider()
    music = provider.mass.music
    music.database.get_rows_from_query = AsyncMock(  # type: ignore[method-assign]
        side_effect=[[{"item_id": 3}], [{"item_id": 4}]]
    )
    music.albums.remove_item_from_library = AsyncMock()  # type: ignore[method-assign]
    music.artists.remove_item_from_library = AsyncMock()  # type: ignore[method-assign]

    await provider._process_orphaned_albums_and_artists()

    music.albums.remove_item_from_library.assert_awaited_once_with(3)
    music.artists.remove_item_from_library.assert_awaited_once_with(4)
    assert all(
        call.args[1] == {"instance_id": INSTANCE_ID}
        for call in music.database.get_rows_from_query.await_args_list
    )
