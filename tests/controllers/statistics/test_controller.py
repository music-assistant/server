"""Tests for statistics backed by completed play history."""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from music_assistant_models.enums import MediaType
from music_assistant_models.errors import InvalidDataError
from music_assistant_models.media_items import Track

from music_assistant.constants import (
    DB_TABLE_ALBUM_TRACKS,
    DB_TABLE_ALBUMS,
    DB_TABLE_CACHE,
    DB_TABLE_GENRE_MEDIA_ITEM_MAPPING,
    DB_TABLE_GENRES,
    DB_TABLE_MEDIA_PROGRESS,
    DB_TABLE_PLAY_HISTORY,
    DB_TABLE_PROVIDER_MAPPINGS,
    DB_TABLE_TRACKS,
)
from music_assistant.controllers.statistics.controller import (
    MAX_STATISTICS_RESULT_LIMIT,
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
        listening_time = await mass.statistics.get_listening_time(
            period="all_time", group_by="genre"
        )
        with pytest.raises(InvalidDataError):
            await mass.statistics.get_listening_time(group_by="mood")

    assert [(result.name, result.value) for result in results] == [("Rock", 2)]
    assert [(result.name, result.minutes) for result in listening_time] == [("Rock", 6.0)]


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

        await mass.statistics.get_play_history(limit=MAX_STATISTICS_RESULT_LIMIT + 1)

    assert get_rows.await_args is not None
    assert get_rows.await_args.kwargs["limit"] == MAX_STATISTICS_RESULT_LIMIT


async def test_statistics_aggregation_limits_are_validated_and_capped(
    mass: MusicAssistant,
) -> None:
    """Every public aggregation rejects unbounded limits and applies the same cap."""
    user = await mass.webserver.auth.create_user("statistics-aggregation-limits")
    aggregation_calls = (
        lambda limit: mass.statistics.get_top_items(MediaType.TRACK, limit=limit),
        lambda limit: mass.statistics.get_genre_distribution(limit=limit),
        lambda limit: mass.statistics.get_artist_distribution(limit=limit),
        lambda limit: mass.statistics.get_listening_time(limit=limit),
        lambda limit: mass.statistics.get_decade_distribution(limit=limit),
    )

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
        for call in aggregation_calls:
            with pytest.raises(InvalidDataError):
                await call(0)
        for call in aggregation_calls:
            await call(MAX_STATISTICS_RESULT_LIMIT + 1)

    assert [call.kwargs["limit"] for call in get_rows.await_args_list] == [
        MAX_STATISTICS_RESULT_LIMIT
    ] * len(aggregation_calls)


async def test_play_history_artwork_preserves_provider_instance(mass: MusicAssistant) -> None:
    """Artwork paths keep their exact provider lookup key when returned to clients."""
    user = await mass.webserver.auth.create_user("statistics-artwork-provider")
    await mass.music.database.insert(
        DB_TABLE_PLAY_HISTORY,
        {
            "item_id": "track-1",
            "provider": "test-provider",
            "media_type": MediaType.TRACK.value,
            "userid": user.user_id,
            "name": "Track with provider artwork",
            "timestamp": 1_000,
            "image": serialize_to_json(
                {
                    "type": "thumb",
                    "path": "covers/track-1.jpg",
                    "provider": "test-provider--account",
                    "remotely_accessible": False,
                }
            ),
        },
    )

    with patch(
        "music_assistant.controllers.statistics.controller.get_current_user",
        return_value=user,
    ):
        results = await mass.statistics.get_top_items(MediaType.TRACK, period="all_time")

    assert results[0].item.image is not None
    assert results[0].item.image.provider == "test-provider--account"


async def test_play_trends_exclude_container_history_rows(mass: MusicAssistant) -> None:
    """Trends count playable history events, not artist/album/playlist selections."""
    user = await mass.webserver.auth.create_user("statistics-playable-events")
    media_types = (
        MediaType.TRACK,
        MediaType.RADIO,
        MediaType.AUDIOBOOK,
        MediaType.PODCAST_EPISODE,
        MediaType.ARTIST,
        MediaType.ALBUM,
        MediaType.PLAYLIST,
        MediaType.PODCAST,
    )
    for index, media_type in enumerate(media_types):
        await mass.music.database.insert(
            DB_TABLE_PLAY_HISTORY,
            {
                "item_id": f"item-{index}",
                "provider": "test-provider",
                "media_type": media_type.value,
                "userid": user.user_id,
                "name": f"Item {index}",
                "timestamp": 1_000,
            },
        )

    with patch(
        "music_assistant.controllers.statistics.controller.get_current_user",
        return_value=user,
    ):
        trend = await mass.statistics.get_plays_over_time(period="all_time")
        activity = await mass.statistics.get_listening_activity(period="all_time")

    assert sum(point.value for point in trend) == 4
    assert sum(point.value for point in activity) == 4


async def test_decade_distribution_resolves_provider_ids_and_deduplicates_albums(
    mass: MusicAssistant,
) -> None:
    """Provider plays map to library tracks and count once despite duplicate album links."""
    user = await mass.webserver.auth.create_user("statistics-decades")
    track_ids = []
    for name in ("Mapped track", "Library track"):
        track_ids.append(
            await mass.music.database.insert(
                DB_TABLE_TRACKS,
                {
                    "name": name,
                    "sort_name": name.casefold(),
                    "metadata": "{}",
                    "search_name": name.casefold(),
                    "search_sort_name": name.casefold(),
                },
            )
        )

    album_ids = []
    for year in (1982, 1998, 1985):
        album_ids.append(
            await mass.music.database.insert(
                DB_TABLE_ALBUMS,
                {
                    "name": f"Album {year}",
                    "sort_name": f"album {year}",
                    "album_type": "album",
                    "year": year,
                    "metadata": "{}",
                    "search_name": f"album {year}",
                    "search_sort_name": f"album {year}",
                },
            )
        )
    for track_id, album_id in (
        (track_ids[0], album_ids[0]),
        (track_ids[0], album_ids[1]),
        (track_ids[1], album_ids[2]),
    ):
        await mass.music.database.insert(
            DB_TABLE_ALBUM_TRACKS,
            {"track_id": track_id, "album_id": album_id, "disc_number": 1, "track_number": 1},
        )

    await mass.music.database.insert(
        DB_TABLE_PROVIDER_MAPPINGS,
        {
            "media_type": MediaType.TRACK.value,
            "item_id": track_ids[0],
            "provider_domain": "test",
            "provider_instance": "test-provider",
            "provider_item_id": "provider-track-1",
        },
    )
    for item_id, provider in (
        ("provider-track-1", "test-provider"),
        (str(track_ids[1]), "library"),
    ):
        await mass.music.database.insert(
            DB_TABLE_PLAY_HISTORY,
            {
                "item_id": item_id,
                "provider": provider,
                "media_type": MediaType.TRACK.value,
                "userid": user.user_id,
                "name": item_id,
                "timestamp": 1_000,
            },
        )

    with patch(
        "music_assistant.controllers.statistics.controller.get_current_user",
        return_value=user,
    ):
        results = await mass.statistics.get_decade_distribution(period="all_time")

    assert [(result.name, result.value) for result in results] == [("1980s", 2)]


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


async def _wait_for_statistics_cache_entry(
    mass: MusicAssistant,
    user_id: str,
    method_name: str = "cached_value",
) -> list[dict[str, object]]:
    assert mass.cache.database is not None
    key_prefix = f"{method_name}.{user_id}."
    for _ in range(100):
        rows = await mass.cache.database.get_rows_from_query(
            f"SELECT key FROM {DB_TABLE_CACHE} WHERE provider = :provider AND key LIKE :key_prefix",
            {"provider": "statistics", "key_prefix": f"{key_prefix}%"},
            limit=0,
        )
        if rows:
            return [dict(row) for row in rows]
        await asyncio.sleep(0.01)
    pytest.fail(f"statistics cache entry for {user_id} was not stored")


async def test_statistics_cache_uses_core_cache_and_scopes_by_user(
    mass: MusicAssistant,
) -> None:
    """Statistics results use the shared cache with separate keys per current user."""
    first_user = await mass.webserver.auth.create_user("statistics-cache-first")
    second_user = await mass.webserver.auth.create_user("statistics-cache-second")
    calls = 0

    @cache_statistics()
    async def cached_value(_self: object, _value: str) -> int:
        nonlocal calls
        calls += 1
        return calls

    with patch(
        "music_assistant.controllers.statistics.controller.get_current_user",
        return_value=first_user,
    ):
        assert await cached_value(mass.statistics, "shared-args") == 1
    await _wait_for_statistics_cache_entry(mass, first_user.user_id)

    with patch(
        "music_assistant.controllers.statistics.controller.get_current_user",
        return_value=first_user,
    ):
        assert await cached_value(mass.statistics, "shared-args") == 1

    with patch(
        "music_assistant.controllers.statistics.controller.get_current_user",
        return_value=second_user,
    ):
        assert await cached_value(mass.statistics, "shared-args") == 2

    assert calls == 2


async def test_statistics_cache_does_not_store_inflight_result_after_invalidation(
    mass: MusicAssistant,
) -> None:
    """A cache miss that overlaps a history write cannot restore stale data."""
    user = await mass.webserver.auth.create_user("statistics-cache-race")
    query_started = asyncio.Event()
    finish_query = asyncio.Event()
    calls = 0

    @cache_statistics()
    async def query(_self: object) -> int:
        nonlocal calls
        calls += 1
        if calls == 1:
            query_started.set()
            await finish_query.wait()
        return calls

    with patch(
        "music_assistant.controllers.statistics.controller.get_current_user",
        return_value=user,
    ):
        in_flight_query: asyncio.Future[int] = asyncio.ensure_future(query(mass.statistics))
        await query_started.wait()
        mass.statistics.invalidate_statistics_cache(play_history_written=True)
        finish_query.set()

        assert await in_flight_query == 1
        assert await query(mass.statistics) == 2


async def test_plays_over_time_does_not_inherit_default_query_limit(
    mass: MusicAssistant,
) -> None:
    """A valid hourly series requests all buckets instead of the DB helper's 500-row default."""
    user = await mass.webserver.auth.create_user("statistics-series-limit")

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
        await mass.statistics.get_plays_over_time(period="month", granularity="hour")

    assert get_rows.await_args is not None
    assert get_rows.await_args.kwargs["limit"] == 0


async def test_plays_over_time_rejects_unsupported_granularity(mass: MusicAssistant) -> None:
    """Unsupported chart granularities do not silently return daily data."""
    user = await mass.webserver.auth.create_user("statistics-invalid-granularity")

    with (
        patch(
            "music_assistant.controllers.statistics.controller.get_current_user",
            return_value=user,
        ),
        pytest.raises(InvalidDataError),
    ):
        await mass.statistics.get_plays_over_time(granularity="fortnight")


async def test_played_item_ids_returns_more_than_database_default_limit(
    mass: MusicAssistant,
) -> None:
    """The recency membership result is not silently truncated to 500 IDs."""
    user = await mass.webserver.auth.create_user("statistics-item-id-limit")
    expected_ids = {f"track-{index}" for index in range(MAX_STATISTICS_RESULT_LIMIT + 1)}
    for item_id in expected_ids:
        await mass.music.database.insert(
            DB_TABLE_PLAY_HISTORY,
            {
                "item_id": item_id,
                "provider": "test-provider",
                "media_type": MediaType.TRACK.value,
                "userid": user.user_id,
                "name": item_id,
                "timestamp": 1_000,
            },
        )

    with patch(
        "music_assistant.controllers.statistics.controller.get_current_user",
        return_value=user,
    ):
        result = await mass.statistics.get_played_item_ids(MediaType.TRACK, 0)

    assert {item_id for _, item_id in result} == expected_ids
    await _wait_for_statistics_cache_entry(mass, user.user_id, "get_played_item_ids")
    with patch(
        "music_assistant.controllers.statistics.controller.get_current_user",
        return_value=user,
    ):
        cached_result = await mass.statistics.get_played_item_ids(MediaType.TRACK, 0)

    assert cached_result == result
