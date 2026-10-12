"""Tests for statistics backed by completed play history."""

from unittest.mock import AsyncMock, patch

import pytest
from music_assistant_models.enums import MediaType
from music_assistant_models.errors import InvalidDataError
from music_assistant_models.media_items import Track

from music_assistant.constants import (
    DB_TABLE_GENRE_MEDIA_ITEM_MAPPING,
    DB_TABLE_GENRES,
    DB_TABLE_MEDIA_PROGRESS,
    DB_TABLE_PLAY_HISTORY,
    DB_TABLE_PROVIDER_MAPPINGS,
)
from music_assistant.controllers.statistics.controller import (
    MAX_PLAY_HISTORY_PAGE_SIZE,
    STATISTICS_CACHE_MAX_ENTRIES,
    cache_statistics,
)
from music_assistant.helpers.json import serialize_to_json
from music_assistant.mass import MusicAssistant


async def test_top_items_counts_history_not_media_progress(mass: MusicAssistant) -> None:
    """Statistics count completed history entries, not mutable progress rows."""
    user = await mass.webserver.auth.create_user("statistics-history")
    item = {
        "item_id": "track-1",
        "provider": "test-provider",
        "media_type": MediaType.TRACK.value,
        "userid": user.user_id,
        "name": "Test Track",
        "timestamp": 1_000,
    }
    await mass.music.database.insert(
        DB_TABLE_PLAY_HISTORY,
        {**item, "user_initiated": True},
    )
    for item_index, timestamp in enumerate((1_100, 1_200, 1_300), start=2):
        await mass.music.database.insert(
            DB_TABLE_MEDIA_PROGRESS,
            {
                **item,
                "item_id": f"progress-only-track-{item_index}",
                "timestamp": timestamp,
                "fully_played": False,
                "seconds_played": timestamp,
            },
            allow_replace=True,
        )

    with patch(
        "music_assistant.controllers.statistics.controller.get_current_user",
        return_value=user,
    ):
        results = await mass.statistics.get_top_items(MediaType.TRACK, period="all_time")

    assert len(results) == 1
    assert results[0].item.item_id == "track-1"
    assert results[0].play_count == 1


async def test_listening_time_keeps_same_named_artists_separate(mass: MusicAssistant) -> None:
    """Artists with the same display name retain separate counts by provider identity."""
    user = await mass.webserver.auth.create_user("statistics-artists")
    for artist_id, track_id in (("artist-a", "track-a"), ("artist-b", "track-b")):
        await mass.music.database.insert(
            DB_TABLE_PLAY_HISTORY,
            {
                "item_id": track_id,
                "provider": "test-provider",
                "media_type": MediaType.TRACK.value,
                "userid": user.user_id,
                "name": track_id,
                "timestamp": 1_000,
                "artists": serialize_to_json(
                    [
                        {
                            "item_id": artist_id,
                            "provider": "test-provider",
                            "name": "Same Name",
                        }
                    ]
                ),
            },
        )

    with patch(
        "music_assistant.controllers.statistics.controller.get_current_user",
        return_value=user,
    ):
        results = await mass.statistics.get_listening_time(period="all_time")

    assert len(results) == 2
    assert all(result.name == "Same Name" and result.minutes == 3.0 for result in results)


async def test_genre_distribution_counts_play_history(mass: MusicAssistant) -> None:
    """Genre distribution counts user plays through the track's provider mapping."""
    user = await mass.webserver.auth.create_user("statistics-genres")
    genre_id = await mass.music.database.insert(
        DB_TABLE_GENRES,
        {
            "name": "Rock",
            "sort_name": "rock",
            "metadata": "{}",
            "search_name": "rock",
            "search_sort_name": "rock",
        },
    )
    await mass.music.database.insert(
        DB_TABLE_PROVIDER_MAPPINGS,
        {
            "media_type": MediaType.TRACK.value,
            "item_id": 1,
            "provider_domain": "test",
            "provider_instance": "test-provider",
            "provider_item_id": "track-1",
        },
    )
    await mass.music.database.insert(
        DB_TABLE_GENRE_MEDIA_ITEM_MAPPING,
        {"genre_id": genre_id, "media_id": 1, "media_type": MediaType.TRACK.value},
    )
    for item_id, provider in (("track-1", "test-provider"), ("1", "library")):
        await mass.music.database.insert(
            DB_TABLE_PLAY_HISTORY,
            {
                "item_id": item_id,
                "provider": provider,
                "media_type": MediaType.TRACK.value,
                "userid": user.user_id,
                "name": f"Track {item_id}",
                "timestamp": 1_000,
            },
        )

    with patch(
        "music_assistant.controllers.statistics.controller.get_current_user",
        return_value=user,
    ):
        results = await mass.statistics.get_genre_distribution(period="all_time")

    assert [(result.name, result.value) for result in results] == [("Rock", 2)]


async def test_play_history_supports_stable_offset_pagination(mass: MusicAssistant) -> None:
    """Equal timestamps use the history row ID as a deterministic pagination tie-breaker."""
    user = await mass.webserver.auth.create_user("statistics-pagination")
    for track_id in ("track-1", "track-2", "track-3"):
        await mass.music.database.insert(
            DB_TABLE_PLAY_HISTORY,
            {
                "item_id": track_id,
                "provider": "test-provider",
                "media_type": MediaType.TRACK.value,
                "userid": user.user_id,
                "name": track_id,
                "timestamp": 1_000,
            },
        )

    with patch(
        "music_assistant.controllers.statistics.controller.get_current_user",
        return_value=user,
    ):
        results = await mass.statistics.get_play_history(limit=2, offset=1)

    assert [result.item_id for result in results] == ["track-2", "track-1"]


async def test_play_history_validates_and_caps_pagination(mass: MusicAssistant) -> None:
    """Reject invalid pagination and cap the requested page size."""
    user = await mass.webserver.auth.create_user("statistics-page-limits")

    with (
        patch(
            "music_assistant.controllers.statistics.controller.get_current_user",
            return_value=user,
        ),
        patch.object(
            mass.music.database,
            "get_rows_from_query",
            new_callable=AsyncMock,
            return_value=[],
        ) as get_rows,
    ):
        with pytest.raises(InvalidDataError):
            await mass.statistics.get_play_history(limit=0)
        with pytest.raises(InvalidDataError):
            await mass.statistics.get_play_history(offset=-1)

        await mass.statistics.get_play_history(limit=MAX_PLAY_HISTORY_PAGE_SIZE + 1)

    assert get_rows.await_args is not None
    assert get_rows.await_args.kwargs["limit"] == MAX_PLAY_HISTORY_PAGE_SIZE


async def test_played_item_ids_reflects_new_history_immediately(mass: MusicAssistant) -> None:
    """A completed play is visible immediately to the recency membership query."""
    user = await mass.webserver.auth.create_user("statistics-live-recency")
    track = Track(
        item_id="newly-played-track",
        provider="test-provider",
        name="Newly Played Track",
        uri="test-provider://track/newly-played-track",
        provider_mappings=set(),
    )

    with patch(
        "music_assistant.controllers.statistics.controller.get_current_user",
        return_value=user,
    ):
        assert await mass.statistics.get_played_item_ids(MediaType.TRACK, 0) == set()
        await mass.music.mark_item_played(track, queue_id="test-queue", userid=user.user_id)
        result = await mass.statistics.get_played_item_ids(MediaType.TRACK, 0)

    assert result == {("test-provider", "newly-played-track")}


async def test_statistics_cache_evicts_old_entries() -> None:
    """The statistics cache remains bounded when query parameters keep changing."""
    calls: list[int] = []

    @cache_statistics()
    async def get_value(_self: object, value: int) -> int:
        calls.append(value)
        return value

    with patch(
        "music_assistant.controllers.statistics.controller.get_current_user",
        return_value=None,
    ):
        for value in range(STATISTICS_CACHE_MAX_ENTRIES + 1):
            await get_value(None, value)
        await get_value(None, 0)

    assert len(calls) == STATISTICS_CACHE_MAX_ENTRIES + 2
