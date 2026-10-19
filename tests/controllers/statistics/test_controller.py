"""Tests for statistics backed by completed play history."""

import asyncio
from datetime import UTC, datetime
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


@pytest.mark.parametrize("apple_provider", ["apple_music--account", "apple_music"])
async def test_top_items_combines_provider_plays_with_library_identity(
    mass: MusicAssistant,
    apple_provider: str,
) -> None:
    """Mapped provider plays and library plays count as one logical item."""
    user = await mass.webserver.auth.create_user("statistics-top-items-identity")
    library_track_id = await mass.music.database.insert(
        DB_TABLE_TRACKS,
        {
            "name": "Shared Track",
            "sort_name": "shared track",
            "metadata": "{}",
            "search_name": "shared track",
            "search_sort_name": "shared track",
        },
    )
    for provider_domain, provider_instance, provider_item_id in (
        ("apple_music", "apple_music--account", "apple-track"),
        ("apple_music", "apple_music--second-account", "apple-track"),
        ("tidal", "tidal--account", "tidal-track"),
    ):
        await mass.music.database.insert(
            DB_TABLE_PROVIDER_MAPPINGS,
            {
                "media_type": MediaType.TRACK.value,
                "item_id": library_track_id,
                "provider_domain": provider_domain,
                "provider_instance": provider_instance,
                "provider_item_id": provider_item_id,
            },
        )

    plays = (
        (str(library_track_id), "library", "Shared Track"),
        ("apple-track", apple_provider, "Apple Track"),
        ("tidal-track", "tidal--account", "Tidal Track"),
        ("unmapped-track", "apple_music--account", "Unmapped Track"),
    )
    for item_id, provider, name in plays:
        await mass.music.database.insert(
            DB_TABLE_PLAY_HISTORY,
            {
                "item_id": item_id,
                "provider": provider,
                "media_type": MediaType.TRACK.value,
                "userid": user.user_id,
                "name": name,
                "timestamp": 1_000,
            },
        )

    with patch(
        "music_assistant.controllers.statistics.controller.get_current_user",
        return_value=user,
    ):
        results = await mass.statistics.get_top_items(MediaType.TRACK, period="all_time")

    assert [
        (result.item.item_id, result.item.provider, result.play_count) for result in results
    ] == [
        (str(library_track_id), "library", 3),
        ("unmapped-track", "apple_music--account", 1),
    ]


async def test_top_items_prefers_exact_instance_over_domain_mapping(mass: MusicAssistant) -> None:
    """An exact instance match wins over another account's domain match."""
    user = await mass.webserver.auth.create_user("statistics-exact-instance")
    for library_id, provider_instance in ((42, "test"), (1, "test--other-account")):
        await mass.music.database.insert(
            DB_TABLE_PROVIDER_MAPPINGS,
            {
                "media_type": MediaType.TRACK.value,
                "item_id": library_id,
                "provider_domain": "test",
                "provider_instance": provider_instance,
                "provider_item_id": "track-1",
            },
        )
    await mass.music.database.insert(
        DB_TABLE_PLAY_HISTORY,
        {
            "item_id": "track-1",
            "provider": "test",
            "media_type": MediaType.TRACK.value,
            "userid": user.user_id,
            "name": "Track",
            "timestamp": 1_000,
        },
    )

    with patch(
        "music_assistant.controllers.statistics.controller.get_current_user",
        return_value=user,
    ):
        results = await mass.statistics.get_top_items(MediaType.TRACK, period="all_time")

    assert [
        (result.item.item_id, result.item.provider, result.play_count) for result in results
    ] == [("42", "library", 1)]


async def test_top_items_artist_tab_counts_track_history(mass: MusicAssistant) -> None:
    """The artist tab groups track history by mapped artist identity."""
    user = await mass.webserver.auth.create_user("statistics-top-artists")
    library_artist_id = 42
    await mass.music.database.insert(
        DB_TABLE_PROVIDER_MAPPINGS,
        {
            "media_type": MediaType.ARTIST.value,
            "item_id": library_artist_id,
            "provider_domain": "test",
            "provider_instance": "test-provider",
            "provider_item_id": "provider-artist",
        },
    )
    plays = (
        (
            "track-one",
            [{"item_id": "provider-artist", "provider": "test-provider", "name": "Mapped"}],
        ),
        (
            "track-two",
            [{"item_id": str(library_artist_id), "provider": "library", "name": "Mapped"}],
        ),
        (
            "track-three",
            [{"item_id": "unmapped-artist", "provider": "test-provider", "name": "Unmapped"}],
        ),
    )
    for track_id, artists in plays:
        await mass.music.database.insert(
            DB_TABLE_PLAY_HISTORY,
            {
                "item_id": track_id,
                "provider": "test-provider",
                "media_type": MediaType.TRACK.value,
                "userid": user.user_id,
                "name": track_id,
                "timestamp": 1_000,
                "artists": serialize_to_json(artists),
            },
        )

    with patch(
        "music_assistant.controllers.statistics.controller.get_current_user",
        return_value=user,
    ):
        results = await mass.statistics.get_top_items(MediaType.ARTIST, period="all_time")

    assert [
        (result.item.item_id, result.item.provider, result.play_count) for result in results
    ] == [(str(library_artist_id), "library", 2), ("unmapped-artist", "test-provider", 1)]


@pytest.mark.parametrize("apple_provider", ["apple_music--account", "apple_music"])
async def test_artist_aggregations_merge_provider_artist_mappings(
    mass: MusicAssistant,
    apple_provider: str,
) -> None:
    """Artist distribution and listening time group all mapped artist identities together."""
    user = await mass.webserver.auth.create_user("statistics-artist-mappings")
    library_artist_id = 100
    artist_mappings = (
        ("apple_music--account", "apple-artist"),
        ("apple_music--second-account", "apple-artist"),
        ("tidal--account", "tidal-artist"),
    )
    for provider_instance, provider_artist_id in artist_mappings:
        await mass.music.database.insert(
            DB_TABLE_PROVIDER_MAPPINGS,
            {
                "media_type": MediaType.ARTIST.value,
                "item_id": library_artist_id,
                "provider_domain": provider_instance.split("--", maxsplit=1)[0],
                "provider_instance": provider_instance,
                "provider_item_id": provider_artist_id,
            },
        )

    plays = (
        (apple_provider, "apple-track", "apple-artist", 120),
        ("tidal--account", "tidal-track", "tidal-artist", 240),
    )
    for provider_instance, provider_track_id, provider_artist_id, duration in plays:
        library_track_id = await mass.music.database.insert(
            DB_TABLE_TRACKS,
            {
                "name": provider_track_id,
                "sort_name": provider_track_id,
                "duration": duration,
                "metadata": "{}",
                "search_name": provider_track_id,
                "search_sort_name": provider_track_id,
            },
        )
        mapping_instances = (
            ("apple_music--account", "apple_music--second-account")
            if provider_track_id == "apple-track"
            else (provider_instance,)
        )
        for mapping_instance in mapping_instances:
            await mass.music.database.insert(
                DB_TABLE_PROVIDER_MAPPINGS,
                {
                    "media_type": MediaType.TRACK.value,
                    "item_id": library_track_id,
                    "provider_domain": mapping_instance.split("--", maxsplit=1)[0],
                    "provider_instance": mapping_instance,
                    "provider_item_id": provider_track_id,
                },
            )
        await mass.music.database.insert(
            DB_TABLE_PLAY_HISTORY,
            {
                "item_id": provider_track_id,
                "provider": provider_instance,
                "media_type": MediaType.TRACK.value,
                "userid": user.user_id,
                "name": provider_track_id,
                "timestamp": 1_000,
                "artists": serialize_to_json(
                    [
                        {
                            "item_id": provider_artist_id,
                            "provider": provider_instance,
                            "name": "Shared Artist",
                        }
                    ]
                ),
            },
        )

    with patch(
        "music_assistant.controllers.statistics.controller.get_current_user",
        return_value=user,
    ):
        artist_distribution = await mass.statistics.get_artist_distribution(period="all_time")
        listening_time = await mass.statistics.get_listening_time(
            period="all_time", group_by="artist"
        )

    assert [
        (result.item.item_id, result.item.provider, result.play_count)
        for result in artist_distribution
    ] == [(str(library_artist_id), "library", 2)]
    assert [(result.name, result.minutes) for result in listening_time] == [("Shared Artist", 6.0)]


async def test_listening_time_keeps_same_named_artists_separate(mass: MusicAssistant) -> None:
    """Artist duration sums use mapped track lengths and keep provider identities separate."""
    user = await mass.webserver.auth.create_user("statistics-artists")
    track_durations = {"track-a": 120, "track-b": 240}
    for artist_id, track_id in (("artist-a", "track-a"), ("artist-b", "track-b")):
        library_track_id = await mass.music.database.insert(
            DB_TABLE_TRACKS,
            {
                "name": track_id,
                "sort_name": track_id,
                "duration": track_durations[track_id],
                "metadata": "{}",
                "search_name": track_id,
                "search_sort_name": track_id,
            },
        )
        await mass.music.database.insert(
            DB_TABLE_PROVIDER_MAPPINGS,
            {
                "media_type": MediaType.TRACK.value,
                "item_id": library_track_id,
                "provider_domain": "test",
                "provider_instance": "test-provider",
                "provider_item_id": track_id,
            },
        )
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
    await mass.music.database.insert(
        DB_TABLE_PLAY_HISTORY,
        {
            "item_id": "unmapped-track",
            "provider": "test-provider",
            "media_type": MediaType.TRACK.value,
            "userid": user.user_id,
            "name": "Unmapped Track",
            "timestamp": 1_000,
            "artists": serialize_to_json(
                [{"item_id": "artist-unmapped", "provider": "test-provider", "name": "Other"}]
            ),
        },
    )

    with patch(
        "music_assistant.controllers.statistics.controller.get_current_user",
        return_value=user,
    ):
        results = await mass.statistics.get_listening_time(period="all_time")

    assert [(result.name, result.minutes) for result in results] == [
        ("Same Name", 4.0),
        ("Same Name", 2.0),
    ]


@pytest.mark.parametrize("history_provider", ["test-provider", "test"])
async def test_genre_distribution_counts_play_history(
    mass: MusicAssistant, history_provider: str
) -> None:
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
    track_id = await mass.music.database.insert(
        DB_TABLE_TRACKS,
        {
            "name": "Rock Track",
            "sort_name": "rock track",
            "duration": 240,
            "metadata": "{}",
            "search_name": "rock track",
            "search_sort_name": "rock track",
        },
    )
    for provider_instance in ("test-provider", "test-second-account"):
        await mass.music.database.insert(
            DB_TABLE_PROVIDER_MAPPINGS,
            {
                "media_type": MediaType.TRACK.value,
                "item_id": track_id,
                "provider_domain": "test",
                "provider_instance": provider_instance,
                "provider_item_id": "track-1",
            },
        )
    await mass.music.database.insert(
        DB_TABLE_GENRE_MEDIA_ITEM_MAPPING,
        {"genre_id": genre_id, "media_id": track_id, "media_type": MediaType.TRACK.value},
    )
    for item_id, provider in (("track-1", history_provider), (str(track_id), "library")):
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
    assert [(result.name, result.minutes) for result in listening_time] == [("Rock", 8.0)]


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


async def test_weekly_plays_use_monday_dates_across_year_boundaries(
    mass: MusicAssistant,
) -> None:
    """Weekly events share their Monday bucket even across a calendar year boundary."""
    user = await mass.webserver.auth.create_user("statistics-week-boundaries")
    for index, date in enumerate(("2026-12-28", "2027-01-01", "2027-01-03", "2027-01-04")):
        await mass.music.database.insert(
            DB_TABLE_PLAY_HISTORY,
            {
                "item_id": f"track-{index}",
                "provider": "test-provider",
                "media_type": MediaType.TRACK.value,
                "userid": user.user_id,
                "name": f"Track {index}",
                "timestamp": datetime.fromisoformat(date).replace(tzinfo=UTC).timestamp(),
            },
        )

    with patch(
        "music_assistant.controllers.statistics.controller.get_current_user",
        return_value=user,
    ):
        results = await mass.statistics.get_plays_over_time(period="all_time", granularity="week")

    assert [(point.timestamp, point.value) for point in results] == [
        ("2026-12-28", 3),
        ("2027-01-04", 1),
    ]


@pytest.mark.parametrize(
    ("timezone", "granularity", "timestamps", "expected"),
    [
        ("Europe/Berlin", "day", ("2026-12-31T23:30:00",), [("2027-01-01", 1)]),
        ("Europe/Berlin", "month", ("2026-12-31T23:30:00",), [("2027-01", 1)]),
        (
            "Europe/Berlin",
            "week",
            ("2026-12-27T23:30:00", "2027-01-03T23:30:00"),
            [("2026-12-28", 1), ("2027-01-04", 1)],
        ),
        (
            "Europe/Berlin",
            "hour",
            ("2027-03-28T00:30:00", "2027-03-28T01:30:00"),
            [("2027-03-28 01:00:00", 1), ("2027-03-28 03:00:00", 1)],
        ),
        (
            "Europe/Berlin",
            "hour",
            ("2027-10-31T00:30:00", "2027-10-31T01:30:00"),
            [("2027-10-31 02:00:00", 2)],
        ),
        ("Asia/Kolkata", "hour", ("2027-01-01T18:45:00",), [("2027-01-02 00:00:00", 1)]),
    ],
)
async def test_play_buckets_follow_iana_timezone(
    mass: MusicAssistant,
    timezone: str,
    granularity: str,
    timestamps: tuple[str, ...],
    expected: list[tuple[str, int]],
) -> None:
    """Local buckets respect date boundaries and historical daylight-saving offsets."""
    user = await mass.webserver.auth.create_user("statistics-local-buckets")
    for index, timestamp in enumerate(timestamps):
        await mass.music.database.insert(
            DB_TABLE_PLAY_HISTORY,
            {
                "item_id": f"track-{index}",
                "provider": "test-provider",
                "media_type": MediaType.TRACK.value,
                "userid": user.user_id,
                "name": f"Track {index}",
                "timestamp": datetime.fromisoformat(timestamp).replace(tzinfo=UTC).timestamp(),
            },
        )

    with patch(
        "music_assistant.controllers.statistics.controller.get_current_user",
        return_value=user,
    ):
        results = await mass.statistics.get_plays_over_time(
            period="all_time", granularity=granularity, timezone=timezone
        )
        activity = await mass.statistics.get_listening_activity(
            period="all_time", timezone=timezone
        )

    assert [(point.timestamp, point.value) for point in results] == expected
    assert sum(point.value for point in activity) == len(timestamps)


@pytest.mark.parametrize(
    ("now", "expected"),
    [
        ("2027-01-01T00:30:00", "2026-12-31T23:00:00"),
        ("2027-03-28T12:00:00", "2027-03-27T23:00:00"),
        ("2027-10-31T12:00:00", "2027-10-30T22:00:00"),
    ],
)
async def test_today_cutoff_uses_local_midnight(
    mass: MusicAssistant, now: str, expected: str
) -> None:
    """Local midnight uses its own UTC offset, even on a daylight-saving transition day."""
    with patch(
        "music_assistant.controllers.statistics.controller.time.time",
        return_value=datetime.fromisoformat(now).replace(tzinfo=UTC).timestamp(),
    ):
        cutoff = mass.statistics._get_period_cutoff("today", "Europe/Berlin")

    assert cutoff == datetime.fromisoformat(expected).replace(tzinfo=UTC).timestamp()


async def test_statistics_cache_and_heatmap_are_timezone_scoped(mass: MusicAssistant) -> None:
    """UTC and local requests cannot share cached calendar buckets or heatmap coordinates."""
    user = await mass.webserver.auth.create_user("statistics-zone-cache")
    await mass.music.database.insert(
        DB_TABLE_PLAY_HISTORY,
        {
            "item_id": "track-1",
            "provider": "test-provider",
            "media_type": MediaType.TRACK.value,
            "userid": user.user_id,
            "name": "Track",
            "timestamp": datetime(2027, 1, 3, 23, 30, tzinfo=UTC).timestamp(),
        },
    )

    with patch(
        "music_assistant.controllers.statistics.controller.get_current_user",
        return_value=user,
    ):
        utc_series = await mass.statistics.get_plays_over_time(period="all_time")
        local_series = await mass.statistics.get_plays_over_time(
            period="all_time", timezone="Europe/Berlin"
        )
        utc_activity = await mass.statistics.get_listening_activity(period="all_time")
        local_activity = await mass.statistics.get_listening_activity(
            period="all_time", timezone="Europe/Berlin"
        )
        cached_series = await mass.statistics.get_plays_over_time(
            period="all_time", timezone="Europe/Berlin"
        )

    assert [(point.timestamp, point.value) for point in utc_series] == [("2027-01-03", 1)]
    assert [(point.timestamp, point.value) for point in local_series] == [("2027-01-04", 1)]
    assert [(point.weekday, point.hour, point.value) for point in utc_activity] == [(6, 23, 1)]
    assert [(point.weekday, point.hour, point.value) for point in local_activity] == [(0, 0, 1)]
    assert cached_series == local_series


@pytest.mark.parametrize("timezone", ["Not/AZone", "", "/etc/localtime"])
async def test_statistics_reject_invalid_timezones(mass: MusicAssistant, timezone: str) -> None:
    """Every period-based endpoint rejects invalid IANA zones rather than falling back."""
    user = await mass.webserver.auth.create_user("statistics-invalid-zone")
    with patch(
        "music_assistant.controllers.statistics.controller.get_current_user",
        return_value=user,
    ):
        calls = (
            lambda: mass.statistics.get_top_items(MediaType.TRACK, timezone=timezone),
            lambda: mass.statistics.get_genre_distribution(timezone=timezone),
            lambda: mass.statistics.get_artist_distribution(timezone=timezone),
            lambda: mass.statistics.get_plays_over_time(timezone=timezone),
            lambda: mass.statistics.get_listening_activity(timezone=timezone),
            lambda: mass.statistics.get_listening_time(timezone=timezone),
            lambda: mass.statistics.get_decade_distribution(timezone=timezone),
        )
        for call in calls:
            with pytest.raises(InvalidDataError, match="Invalid timezone"):
                await call()


@pytest.mark.parametrize("history_provider", ["test-provider", "test"])
async def test_decade_distribution_resolves_provider_ids_and_deduplicates_albums(
    mass: MusicAssistant,
    history_provider: str,
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

    for provider_instance in ("test-provider", "test-second-account"):
        await mass.music.database.insert(
            DB_TABLE_PROVIDER_MAPPINGS,
            {
                "media_type": MediaType.TRACK.value,
                "item_id": track_ids[0],
                "provider_domain": "test",
                "provider_instance": provider_instance,
                "provider_item_id": "provider-track-1",
            },
        )
    for item_id, provider in (
        ("provider-track-1", history_provider),
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


async def test_statistics_cache_invalidates_only_affected_users(mass: MusicAssistant) -> None:
    """A user's completed play leaves another user's cached statistics intact."""
    first_user = await mass.webserver.auth.create_user("statistics-cache-write-first")
    second_user = await mass.webserver.auth.create_user("statistics-cache-write-second")
    calls = 0

    @cache_statistics()
    async def cached_value(_self: object) -> int:
        nonlocal calls
        calls += 1
        return calls

    with patch(
        "music_assistant.controllers.statistics.controller.get_current_user",
        return_value=first_user,
    ):
        assert await cached_value(mass.statistics) == 1
    await _wait_for_statistics_cache_entry(mass, first_user.user_id)
    with patch(
        "music_assistant.controllers.statistics.controller.get_current_user",
        return_value=second_user,
    ):
        assert await cached_value(mass.statistics) == 2
    await _wait_for_statistics_cache_entry(mass, second_user.user_id)

    mass.statistics.invalidate_statistics_cache(
        play_history_written=True, user_ids=[first_user.user_id]
    )

    with patch(
        "music_assistant.controllers.statistics.controller.get_current_user",
        return_value=second_user,
    ):
        assert await cached_value(mass.statistics) == 2
    with patch(
        "music_assistant.controllers.statistics.controller.get_current_user",
        return_value=first_user,
    ):
        assert await cached_value(mass.statistics) == 3


async def test_statistics_cache_global_invalidation_rotates_all_user_keys(
    mass: MusicAssistant,
) -> None:
    """Clearing the user map never reuses cache keys from an earlier generation."""
    users = [
        await mass.webserver.auth.create_user("statistics-global-first"),
        await mass.webserver.auth.create_user("statistics-global-second"),
    ]
    calls = 0

    @cache_statistics()
    async def cached_value(_self: object) -> int:
        nonlocal calls
        calls += 1
        return calls

    for index, user in enumerate(users, start=1):
        with patch(
            "music_assistant.controllers.statistics.controller.get_current_user",
            return_value=user,
        ):
            assert await cached_value(mass.statistics) == index
        await _wait_for_statistics_cache_entry(mass, user.user_id)

    generations = dict(mass.statistics._statistics_user_cache_generations)
    mass.statistics.invalidate_statistics_cache(play_history_written=False)
    assert mass.statistics._statistics_user_cache_generations == generations
    mass.statistics.invalidate_statistics_cache(play_history_written=True)
    assert mass.statistics._statistics_user_cache_generations == {}

    for index, user in enumerate(users, start=3):
        with patch(
            "music_assistant.controllers.statistics.controller.get_current_user",
            return_value=user,
        ):
            assert await cached_value(mass.statistics) == index
        assert (
            mass.statistics._statistics_user_cache_generations[user.user_id]
            != generations[user.user_id]
        )


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


async def test_plays_over_time_rejects_unsupported_parameters(mass: MusicAssistant) -> None:
    """Unsupported chart parameters do not silently return default data."""
    user = await mass.webserver.auth.create_user("statistics-invalid-granularity")

    with patch(
        "music_assistant.controllers.statistics.controller.get_current_user",
        return_value=user,
    ):
        with pytest.raises(InvalidDataError):
            await mass.statistics.get_plays_over_time(granularity="fortnight")
        with pytest.raises(InvalidDataError):
            await mass.statistics.get_plays_over_time(period="fortnight")


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
