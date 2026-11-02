"""Tests for statistics backed by completed play history."""

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest
from music_assistant_models.auth import Scope, User
from music_assistant_models.enums import ArtistType, MediaType
from music_assistant_models.errors import InvalidDataError
from music_assistant_models.media_items import AudioFormat

from music_assistant.constants import (
    DB_TABLE_ALBUM_TRACKS,
    DB_TABLE_ALBUMS,
    DB_TABLE_ARTISTS,
    DB_TABLE_GENRE_MEDIA_ITEM_MAPPING,
    DB_TABLE_GENRES,
    DB_TABLE_MEDIA_PROGRESS,
    DB_TABLE_PLAY_HISTORY,
    DB_TABLE_PROVIDER_MAPPINGS,
    DB_TABLE_TRACKS,
)
from music_assistant.controllers.statistics.base import MAX_STATISTICS_RESULT_LIMIT
from music_assistant.controllers.webserver.helpers.auth_middleware import set_current_user
from music_assistant.helpers.json import serialize_to_json
from music_assistant.mass import MusicAssistant


@contextmanager
def _as_user(user: User | None) -> Iterator[None]:
    """Set and clear the authenticated-user context around a statistics call."""
    set_current_user(user)
    try:
        yield
    finally:
        set_current_user(None)


@contextmanager
def _frozen_now(timestamp: str) -> Iterator[None]:
    """Pin the current time used for period boundaries and series ends to a UTC instant."""
    with patch(
        "music_assistant.controllers.statistics.base.time.time",
        return_value=datetime.fromisoformat(timestamp).replace(tzinfo=UTC).timestamp(),
    ):
        yield


async def _insert_plays(mass: MusicAssistant, user: User, timestamps: tuple[str, ...]) -> None:
    """Insert completed track plays at the given UTC instants."""
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


async def test_statistics_api_registration_is_unchanged(mass: MusicAssistant) -> None:
    """All statistics mixins retain their registered commands, parameters and scopes."""
    expected_parameters = {
        "statistics/top_items": ("media_type", "period", "limit", "timezone"),
        "statistics/genre_distribution": ("period", "limit", "timezone"),
        "statistics/plays_over_time": ("period", "granularity", "timezone"),
        "statistics/listening_activity": ("period", "timezone"),
        "statistics/listening_time": ("period", "group_by", "limit", "timezone"),
        "statistics/decade_distribution": ("period", "limit", "timezone"),
        "statistics/play_history": ("limit", "media_types", "played_after_timestamp", "offset"),
        "statistics/play_count": ("item_id", "provider", "media_type"),
    }
    commands = {
        command: handler
        for command, handler in mass.command_handlers.items()
        if command.startswith("statistics/")
    }
    assert commands.keys() == expected_parameters.keys()
    for command, handler in commands.items():
        assert tuple(handler.signature.parameters) == expected_parameters[command]
        assert handler.required_scope == Scope.LIBRARY_READ


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

    with _as_user(user):
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
            "duration": 180,
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
                "audio_format": serialize_to_json(AudioFormat().to_dict()),
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

    other_user = await mass.webserver.auth.create_user("statistics-other-user")
    for media_type, user_id in (
        (MediaType.RADIO, user.user_id),
        (MediaType.TRACK, other_user.user_id),
    ):
        await mass.music.database.insert(
            DB_TABLE_PLAY_HISTORY,
            {
                "item_id": str(library_track_id),
                "provider": "library",
                "media_type": media_type.value,
                "userid": user_id,
                "name": "Unrelated play",
                "timestamp": 1_000,
            },
        )

    with _as_user(user):
        results = await mass.statistics.get_top_items(MediaType.TRACK, period="all_time")
        ranked_counts = [
            await mass.statistics.get_play_count(
                result.item.item_id, result.item.provider, MediaType.TRACK
            )
            for result in results
        ]
        mapped_counts = [
            await mass.statistics.get_play_count(item_id, provider, MediaType.TRACK)
            for item_id, provider in (
                ("apple-track", "apple_music"),
                ("apple-track", "apple_music--account"),
                ("apple-track", "apple_music--second-account"),
                ("tidal-track", "tidal--account"),
            )
        ]
        missing_count = await mass.statistics.get_play_count(
            "missing-track", "apple_music--account", MediaType.TRACK
        )

    assert [
        (result.item.item_id, result.item.provider, result.play_count) for result in results
    ] == [
        (str(library_track_id), "library", 3),
        ("unmapped-track", "apple_music--account", 1),
    ]
    assert ranked_counts == [result.play_count for result in results]
    assert mapped_counts == [3, 3, 3, 3]
    assert missing_count == 0


async def test_album_top_items_count_listened_tracks_not_album_enqueues(
    mass: MusicAssistant,
) -> None:
    """Album rankings and counts derive from completed track plays across sources."""
    user = await mass.webserver.auth.create_user("statistics-album-track-plays")
    album_id = await mass.music.database.insert(
        DB_TABLE_ALBUMS,
        {
            "name": "Played Album",
            "sort_name": "played album",
            "album_type": "album",
            "metadata": serialize_to_json({"images": []}),
            "search_name": "played album",
            "search_sort_name": "played album",
        },
    )
    await mass.music.database.insert(
        DB_TABLE_PROVIDER_MAPPINGS,
        {
            "media_type": MediaType.ALBUM.value,
            "item_id": album_id,
            "provider_domain": "playlist_source",
            "provider_instance": "playlist_source--account",
            "provider_item_id": "provider-album",
            "audio_format": serialize_to_json(AudioFormat().to_dict()),
        },
    )
    for track_name, provider_item_id in (("Track One", "track-one"), ("Track Two", "track-two")):
        track_id = await mass.music.database.insert(
            DB_TABLE_TRACKS,
            {
                "name": track_name,
                "sort_name": track_name.casefold(),
                "duration": 180,
                "metadata": "{}",
                "search_name": track_name.casefold(),
                "search_sort_name": track_name.casefold(),
            },
        )
        await mass.music.database.insert(
            DB_TABLE_ALBUM_TRACKS,
            {"track_id": track_id, "album_id": album_id, "disc_number": 1, "track_number": 1},
        )
        await mass.music.database.insert(
            DB_TABLE_PROVIDER_MAPPINGS,
            {
                "media_type": MediaType.TRACK.value,
                "item_id": track_id,
                "provider_domain": "playlist_source",
                "provider_instance": "playlist_source--account",
                "provider_item_id": provider_item_id,
                "audio_format": serialize_to_json(AudioFormat().to_dict()),
            },
        )
        await mass.music.database.insert(
            DB_TABLE_PLAY_HISTORY,
            {
                "item_id": provider_item_id,
                "provider": "playlist_source--account",
                "media_type": MediaType.TRACK.value,
                "userid": user.user_id,
                "name": track_name,
                "timestamp": 1_000,
            },
        )
    await mass.music.database.insert(
        DB_TABLE_PLAY_HISTORY,
        {
            "item_id": "track-one",
            "provider": "playlist_source--account",
            "media_type": MediaType.TRACK.value,
            "userid": user.user_id,
            "name": "Track One replay",
            "timestamp": 2_000,
        },
    )
    await mass.music.database.insert(
        DB_TABLE_PLAY_HISTORY,
        {
            "item_id": str(album_id),
            "provider": "library",
            "media_type": MediaType.ALBUM.value,
            "userid": user.user_id,
            "name": "Album enqueue, not a completed track play",
            "timestamp": 3_000,
        },
    )

    with _as_user(user):
        rankings = await mass.statistics.get_top_items(MediaType.ALBUM, period="all_time")
        counts = [
            await mass.statistics.get_play_count(str(album_id), "library", MediaType.ALBUM),
            await mass.statistics.get_play_count(
                "provider-album", "playlist_source--account", MediaType.ALBUM
            ),
        ]

    assert [(result.item.item_id, result.play_count) for result in rankings] == [(str(album_id), 3)]
    assert counts == [3, 3]


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

    with _as_user(user):
        results = await mass.statistics.get_top_items(MediaType.TRACK, period="all_time")
        count = await mass.statistics.get_play_count("track-1", "test", MediaType.TRACK)
        other_account_count = await mass.statistics.get_play_count(
            "track-1", "test--other-account", MediaType.TRACK
        )

    assert [
        (result.item.item_id, result.item.provider, result.play_count) for result in results
    ] == [("42", "library", 1)]
    assert count == 1
    assert other_account_count == 0


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

    with _as_user(user):
        results = await mass.statistics.get_top_items(MediaType.ARTIST, period="all_time")
        counts = [
            await mass.statistics.get_play_count(
                result.item.item_id, result.item.provider, MediaType.ARTIST
            )
            for result in results
        ]
        provider_count = await mass.statistics.get_play_count(
            "provider-artist", "test-provider", MediaType.ARTIST
        )

    assert [
        (result.item.item_id, result.item.provider, result.play_count) for result in results
    ] == [(str(library_artist_id), "library", 2), ("unmapped-artist", "test-provider", 1)]
    assert counts == [result.play_count for result in results]
    assert provider_count == 2


@pytest.mark.parametrize("apple_provider", ["apple_music--account", "apple_music"])
@pytest.mark.parametrize("duplicate_credit", [False, True])
async def test_artist_aggregations_merge_provider_artist_mappings(
    mass: MusicAssistant,
    apple_provider: str,
    duplicate_credit: bool,
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
        artist_credits = [
            {
                "item_id": provider_artist_id,
                "provider": provider_instance,
                "name": "Shared Artist",
                "image": {
                    "type": "thumb",
                    "path": "https://example.com/artist.jpg",
                    "provider": "url",
                    "remotely_accessible": True,
                },
            }
        ]
        if duplicate_credit:
            artist_credits.append(
                {
                    "item_id": str(library_artist_id),
                    "provider": "library",
                    "name": "Alias Artist",
                    "image": {
                        "type": "thumb",
                        "path": "https://example.com/alias.jpg",
                        "provider": "url",
                        "remotely_accessible": True,
                    },
                }
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
                "artists": serialize_to_json(artist_credits),
            },
        )

    with _as_user(user):
        ranking = await mass.statistics.get_top_items(MediaType.ARTIST, period="all_time")
        counts = [
            await mass.statistics.get_play_count(item_id, provider, MediaType.ARTIST)
            for item_id, provider in (
                (str(library_artist_id), "library"),
                ("apple-artist", "apple_music"),
                ("apple-artist", "apple_music--account"),
                ("apple-artist", "apple_music--second-account"),
                ("tidal-artist", "tidal--account"),
            )
        ]
        listening_time = await mass.statistics.get_listening_time(
            period="all_time", group_by="artist"
        )

    assert [
        (result.item.item_id, result.item.provider, result.play_count) for result in ranking
    ] == [(str(library_artist_id), "library", 2)]
    assert counts == [2, 2, 2, 2, 2]
    assert [(result.name, result.minutes) for result in listening_time] == [("Shared Artist", 6.0)]
    assert [
        (result.item.item_id, result.item.provider, result.item.media_type)
        for result in listening_time
        if result.item
    ] == [(str(library_artist_id), "library", MediaType.ARTIST)]


async def test_listening_time_keeps_same_named_artists_separate(mass: MusicAssistant) -> None:
    """Artist duration sums use mapped track lengths and keep each artist as its own item."""
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

    with _as_user(user):
        results = await mass.statistics.get_listening_time(period="all_time")

    assert [(result.name, result.minutes) for result in results] == [
        ("Same Name", 4.0),
        ("Same Name", 2.0),
    ]
    assert [
        (result.item.item_id, result.item.provider, result.item.media_type, result.item.name)
        for result in results
        if result.item
    ] == [
        ("artist-b", "test-provider", MediaType.ARTIST, "Same Name"),
        ("artist-a", "test-provider", MediaType.ARTIST, "Same Name"),
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

    with _as_user(user):
        results = await mass.statistics.get_genre_distribution(period="all_time")
        listening_time = await mass.statistics.get_listening_time(
            period="all_time", group_by="genre"
        )
        with pytest.raises(InvalidDataError):
            await mass.statistics.get_listening_time(group_by="mood")

    assert [(result.name, result.value) for result in results] == [("Rock", 2)]
    assert [(result.name, result.minutes) for result in listening_time] == [("Rock", 8.0)]
    assert [result.item for result in listening_time] == [None]


@pytest.mark.parametrize("media_type", [MediaType.TRACK, MediaType.ARTIST])
async def test_statistics_preserve_unavailable_library_history(
    mass: MusicAssistant, media_type: MediaType
) -> None:
    """History keeps missing and unavailable library items without offering them as playable."""
    user = await mass.webserver.auth.create_user("statistics-library-availability")
    table = DB_TABLE_ARTISTS if media_type == MediaType.ARTIST else DB_TABLE_TRACKS
    library_ids: dict[str, int] = {}
    for name in ("Available", "Unavailable", "Deleted"):
        library_id = await mass.music.database.insert(
            table,
            {
                "name": name,
                "sort_name": name.casefold(),
                "metadata": "{}",
                "search_name": name.casefold(),
                "search_sort_name": name.casefold(),
                **(
                    {"artist_type": ArtistType.SINGER.value}
                    if media_type == MediaType.ARTIST
                    else {"duration": 0}
                ),
            },
        )
        library_ids[name] = library_id
        await mass.music.database.insert(
            DB_TABLE_PROVIDER_MAPPINGS,
            {
                "media_type": media_type.value,
                "item_id": library_id,
                "provider_domain": "test",
                "provider_instance": "test-provider",
                "provider_item_id": name,
                "available": name != "Unavailable",
                "audio_format": serialize_to_json(AudioFormat().to_dict()),
            },
        )
        await mass.music.database.insert(
            DB_TABLE_PLAY_HISTORY,
            {
                "item_id": str(library_id),
                "provider": "library",
                "media_type": media_type.value,
                "userid": user.user_id,
                "name": name,
                "timestamp": 1_000,
            },
        )
        if media_type == MediaType.ARTIST:
            await mass.music.database.insert(
                DB_TABLE_PLAY_HISTORY,
                {
                    "item_id": f"track-{library_id}",
                    "provider": "test-provider",
                    "media_type": MediaType.TRACK.value,
                    "userid": user.user_id,
                    "name": "Credited track",
                    "timestamp": 1_000,
                    "artists": serialize_to_json(
                        [{"item_id": str(library_id), "provider": "library", "name": name}]
                    ),
                },
            )

    await mass.music.database.delete(table, {"item_id": library_ids["Deleted"]})
    await mass.music.database.delete(
        DB_TABLE_PROVIDER_MAPPINGS,
        {"media_type": media_type.value, "item_id": library_ids["Deleted"]},
    )
    controller = mass.music.get_controller(media_type)
    with (
        _as_user(user),
        patch(
            "music_assistant_models.media_items.media_item.get_global_cache_value",
            return_value={"test-provider"},
        ),
        patch.object(
            controller, "get_library_items_by_query", wraps=controller.get_library_items_by_query
        ) as batch_lookup,
    ):
        ranking = await mass.statistics.get_top_items(media_type, period="all_time")
        history = await mass.statistics.get_play_history(media_types=[media_type])
        responses = [[result.item for result in ranking], history]

    for items in responses:
        assert {item.name: item.available for item in items} == {
            "Available": True,
            "Unavailable": False,
            "Deleted": False,
        }
    assert [result.play_count for result in ranking] == [1, 1, 1]
    assert batch_lookup.await_count == len(responses)


@pytest.mark.parametrize(
    ("history_provider", "expected_available"),
    [("test-provider", False), ("test", True)],
)
async def test_provider_history_respects_mapping_availability(
    mass: MusicAssistant, history_provider: str, expected_available: bool
) -> None:
    """History uses exact-instance and available domain mappings without changing its key."""
    user = await mass.webserver.auth.create_user("statistics-provider-history-availability")
    track_id = await mass.music.database.insert(
        DB_TABLE_TRACKS,
        {
            "name": "Unavailable provider track",
            "sort_name": "unavailable provider track",
            "duration": 180,
            "metadata": "{}",
            "search_name": "unavailable provider track",
            "search_sort_name": "unavailable provider track",
        },
    )
    await mass.music.database.insert(
        DB_TABLE_PROVIDER_MAPPINGS,
        {
            "media_type": MediaType.TRACK.value,
            "item_id": track_id,
            "provider_domain": "test",
            "provider_instance": "test-provider",
            "provider_item_id": "provider-track",
            "available": False,
            "audio_format": serialize_to_json(AudioFormat().to_dict()),
        },
    )
    await mass.music.database.insert(
        DB_TABLE_PROVIDER_MAPPINGS,
        {
            "media_type": MediaType.TRACK.value,
            "item_id": track_id,
            "provider_domain": "test",
            "provider_instance": "test-second-provider",
            "provider_item_id": "provider-track",
            "available": True,
            "audio_format": serialize_to_json(AudioFormat().to_dict()),
        },
    )
    await mass.music.database.insert(
        DB_TABLE_PLAY_HISTORY,
        {
            "item_id": "provider-track",
            "provider": history_provider,
            "media_type": MediaType.TRACK.value,
            "userid": user.user_id,
            "name": "Unavailable provider track",
            "timestamp": 1_000,
        },
    )

    with (
        _as_user(user),
        patch(
            "music_assistant.controllers.statistics.history.get_global_cache_value",
            return_value={"test", "test-provider", "test-second-provider"},
        ),
        patch(
            "music_assistant.controllers.statistics.base.get_global_cache_value",
            return_value={"test", "test-provider", "test-second-provider"},
        ),
    ):
        results = await mass.statistics.get_play_history(media_types=[MediaType.TRACK])

    assert len(results) == 1
    assert results[0].provider == history_provider
    assert results[0].item_id == "provider-track"
    assert results[0].available is expected_available


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

    with _as_user(user):
        results = await mass.statistics.get_play_history(limit=2, offset=1)

    assert [result.item_id for result in results] == ["track-2", "track-1"]


async def test_play_history_default_includes_podcast_episodes(mass: MusicAssistant) -> None:
    """The default history filter includes playable podcast episodes."""
    user = await mass.webserver.auth.create_user("statistics-podcast-history")
    await mass.music.database.insert(
        DB_TABLE_PLAY_HISTORY,
        {
            "item_id": "episode-1",
            "provider": "podcast-provider",
            "media_type": MediaType.PODCAST_EPISODE.value,
            "userid": user.user_id,
            "name": "Podcast episode",
            "timestamp": 1_000,
        },
    )

    with _as_user(user):
        results = await mass.statistics.get_play_history()

    assert [(item.item_id, item.media_type) for item in results] == [
        ("episode-1", MediaType.PODCAST_EPISODE)
    ]


async def test_play_history_validates_and_caps_pagination(mass: MusicAssistant) -> None:
    """Reject invalid pagination and cap the requested page size."""
    user = await mass.webserver.auth.create_user("statistics-page-limits")

    with (
        _as_user(user),
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
        lambda limit: mass.statistics.get_top_items(MediaType.ARTIST, limit=limit),
        lambda limit: mass.statistics.get_listening_time(limit=limit),
        lambda limit: mass.statistics.get_decade_distribution(limit=limit),
    )

    with (
        _as_user(user),
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

    with _as_user(user):
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

    with _as_user(user):
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

    with _as_user(user), _frozen_now("2027-01-04T00:00:00"):
        results = await mass.statistics.get_plays_over_time(period="all_time", granularity="week")

    assert [(point.timestamp, point.value) for point in results] == [
        ("2026-12-28", 3),
        ("2027-01-04", 1),
    ]


async def test_monthly_plays_sum_daily_buckets(mass: MusicAssistant) -> None:
    """Monthly SQL buckets sum all daily play groups within each month."""
    user = await mass.webserver.auth.create_user("statistics-month-buckets")
    for index, date in enumerate(("2026-08-01", "2026-08-20", "2026-09-01")):
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

    with _as_user(user), _frozen_now("2026-09-01T00:00:00"):
        results = await mass.statistics.get_plays_over_time(period="all_time", granularity="month")

    assert [(point.timestamp, point.value) for point in results] == [
        ("2026-08", 2),
        ("2026-09", 1),
    ]


async def test_utc_activity_sums_hourly_buckets(mass: MusicAssistant) -> None:
    """UTC activity aggregates daily play groups into weekday/hour totals."""
    user = await mass.webserver.auth.create_user("statistics-activity-buckets")
    for index, timestamp in enumerate(
        ("2026-08-03T01:05:00", "2026-08-03T01:55:00", "2026-08-03T02:05:00")
    ):
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

    with _as_user(user):
        results = await mass.statistics.get_listening_activity(period="all_time")

    assert [(point.weekday, point.hour, point.value) for point in results] == [
        (0, 1, 2),
        (0, 2, 1),
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

    with _as_user(user), _frozen_now(timestamps[-1]):
        results = await mass.statistics.get_plays_over_time(
            period="all_time", granularity=granularity, timezone=timezone
        )
        activity = await mass.statistics.get_listening_activity(
            period="all_time", timezone=timezone
        )

    assert [(point.timestamp, point.value) for point in results] == expected
    assert sum(point.value for point in activity) == len(timestamps)


@pytest.mark.parametrize(
    ("timezone", "now", "expected"),
    [
        ("Europe/Berlin", "2027-01-01T00:30:00", "2026-12-31T23:00:00"),
        ("Europe/Berlin", "2027-03-28T12:00:00", "2027-03-27T23:00:00"),
        ("Europe/Berlin", "2027-10-31T12:00:00", "2027-10-30T22:00:00"),
        ("America/Havana", "2026-11-01T04:30:00", "2026-11-01T04:00:00"),
        ("America/Havana", "2026-11-01T05:30:00", "2026-11-01T04:00:00"),
        ("America/Havana", "2026-11-01T06:30:00", "2026-11-01T04:00:00"),
    ],
)
async def test_today_cutoff_uses_local_midnight(
    mass: MusicAssistant, timezone: str, now: str, expected: str
) -> None:
    """Local midnight uses its own UTC offset, even on a daylight-saving transition day."""
    with patch(
        "music_assistant.controllers.statistics.base.time.time",
        return_value=datetime.fromisoformat(now).replace(tzinfo=UTC).timestamp(),
    ):
        cutoff = mass.statistics._get_period_cutoff("today", timezone)

    assert cutoff == datetime.fromisoformat(expected).replace(tzinfo=UTC).timestamp()


async def test_statistics_and_heatmap_are_timezone_scoped(mass: MusicAssistant) -> None:
    """UTC and local requests use their own calendar buckets and heatmap coordinates."""
    user = await mass.webserver.auth.create_user("statistics-zones")
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

    with _as_user(user), _frozen_now("2027-01-03T23:30:00"):
        utc_series = await mass.statistics.get_plays_over_time(period="all_time")
        local_series = await mass.statistics.get_plays_over_time(
            period="all_time", timezone="Europe/Berlin"
        )
        utc_activity = await mass.statistics.get_listening_activity(period="all_time")
        local_activity = await mass.statistics.get_listening_activity(
            period="all_time", timezone="Europe/Berlin"
        )

    assert [(point.timestamp, point.value) for point in utc_series] == [("2027-01-03", 1)]
    assert [(point.timestamp, point.value) for point in local_series] == [("2027-01-04", 1)]
    assert [(point.weekday, point.hour, point.value) for point in utc_activity] == [(6, 23, 1)]
    assert [(point.weekday, point.hour, point.value) for point in local_activity] == [(0, 0, 1)]


@pytest.mark.parametrize("timezone", ["Not/AZone", "", "/etc/localtime"])
async def test_statistics_reject_invalid_timezones(mass: MusicAssistant, timezone: str) -> None:
    """Every period-based endpoint rejects invalid IANA zones rather than falling back."""
    user = await mass.webserver.auth.create_user("statistics-invalid-zone")
    with _as_user(user):
        calls = (
            lambda: mass.statistics.get_top_items(MediaType.TRACK, timezone=timezone),
            lambda: mass.statistics.get_genre_distribution(timezone=timezone),
            lambda: mass.statistics.get_top_items(MediaType.ARTIST, timezone=timezone),
            lambda: mass.statistics.get_plays_over_time(timezone=timezone),
            lambda: mass.statistics.get_listening_activity(timezone=timezone),
            lambda: mass.statistics.get_listening_time(timezone=timezone),
            lambda: mass.statistics.get_decade_distribution(timezone=timezone),
        )
        for call in calls:
            with pytest.raises(InvalidDataError, match="Invalid timezone"):
                await call()


@pytest.mark.parametrize("history_provider", ["test-provider", "test"])
@pytest.mark.parametrize(
    ("album_years", "expected"),
    [
        ((1982, 1998, 1985), [("1980s", 2)]),
        ((0, 1998, 1985), [("1990s", 1), ("1980s", 1)]),
        ((0, 0, 0), []),
        ((None, 1998, None), [("1990s", 1)]),
    ],
)
async def test_decade_distribution_resolves_provider_ids_and_deduplicates_albums(
    mass: MusicAssistant,
    history_provider: str,
    album_years: tuple[int | None, ...],
    expected: list[tuple[str, int]],
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
    for year in album_years:
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

    with _as_user(user):
        results = await mass.statistics.get_decade_distribution(period="all_time")

    assert [(result.name, result.value) for result in results] == expected


async def test_plays_over_time_does_not_inherit_default_query_limit(
    mass: MusicAssistant,
) -> None:
    """A valid hourly series requests all buckets instead of the DB helper's 500-row default."""
    user = await mass.webserver.auth.create_user("statistics-series-limit")

    with (
        _as_user(user),
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


@pytest.mark.parametrize(
    ("timezone", "now", "plays", "expected"),
    [
        (
            "Europe/Berlin",
            "2027-01-15T10:20:00",
            ("2027-01-14T22:59:00", "2027-01-15T08:10:00", "2027-01-15T08:50:00"),
            [(f"2027-01-15 {hour:02d}:00:00", 2 if hour == 9 else 0) for hour in range(12)],
        ),
        (
            "Europe/Berlin",
            "2027-03-28T05:30:00",
            ("2027-03-28T00:30:00",),
            [
                (f"2027-03-28 {hour:02d}:00:00", 1 if hour == 1 else 0)
                for hour in (0, 1, 3, 4, 5, 6, 7)
            ],
        ),
        (
            "Europe/Berlin",
            "2027-10-31T05:30:00",
            ("2027-10-31T00:30:00", "2027-10-31T01:30:00"),
            [(f"2027-10-31 {hour:02d}:00:00", 2 if hour == 2 else 0) for hour in range(7)],
        ),
        (
            "UTC",
            "2027-01-15T03:20:00",
            ("2027-01-15T02:00:00",),
            [(f"2027-01-15 {hour:02d}:00:00", 1 if hour == 2 else 0) for hour in range(4)],
        ),
        # 02:45 jumps to 03:45, so only the end of the 03:00 hour exists; a play at 03:50
        # keeps its bucket
        (
            "Pacific/Chatham",
            "2026-09-26T15:45:00",
            ("2026-09-26T14:05:00",),
            [(f"2026-09-27 {hour:02d}:00:00", 1 if hour == 3 else 0) for hour in range(6)],
        ),
        # 02:00 jumps half an hour to 02:30, so the 02:00 hour exists only in part
        (
            "Australia/Lord_Howe",
            "2026-10-03T17:30:00",
            ("2026-10-03T15:40:00",),
            [(f"2026-10-04 {hour:02d}:00:00", 1 if hour == 2 else 0) for hour in range(5)],
        ),
        # 03:00 rolls back two hours to 01:00, so 01:00 and 02:00 happen twice
        (
            "Antarctica/Troll",
            "2026-10-25T06:30:00",
            ("2026-10-24T23:30:00", "2026-10-25T01:30:00"),
            [(f"2026-10-25 {hour:02d}:00:00", 2 if hour == 1 else 0) for hour in range(7)],
        ),
    ],
)
async def test_today_series_fills_every_local_hour(
    mass: MusicAssistant,
    timezone: str,
    now: str,
    plays: tuple[str, ...],
    expected: list[tuple[str, int]],
) -> None:
    """Today has a bucket for each local hour since midnight, across daylight-saving changes."""
    user = await mass.webserver.auth.create_user("statistics-today-series")
    await _insert_plays(mass, user, plays)

    with _as_user(user), _frozen_now(now):
        results = await mass.statistics.get_plays_over_time(
            period="today", granularity="hour", timezone=timezone
        )

    assert [(point.timestamp, point.value) for point in results] == expected


@pytest.mark.parametrize(
    ("now", "expected"),
    [
        ("2027-03-28T01:30:00", [("2027-03-28 03:00:00", 1)]),
        (
            "2027-03-28T04:30:00",
            [(f"2027-03-28 {hour:02d}:00:00", 1 if hour == 3 else 0) for hour in range(3, 7)],
        ),
    ],
)
async def test_all_time_hours_start_right_after_a_two_hour_gap(
    mass: MusicAssistant, now: str, expected: list[tuple[str, int]]
) -> None:
    """A first play in the hour after Antarctica/Troll's 01:00 to 03:00 jump keeps its bucket."""
    user = await mass.webserver.auth.create_user("statistics-troll-spring")
    # 03:20 local, twenty minutes after the clocks jumped from 01:00 to 03:00
    await _insert_plays(mass, user, ("2027-03-28T01:20:00",))

    with _as_user(user), _frozen_now(now):
        results = await mass.statistics.get_plays_over_time(
            period="all_time", granularity="hour", timezone="Antarctica/Troll"
        )

    assert [(point.timestamp, point.value) for point in results] == expected


@pytest.mark.parametrize("timezone", ["UTC", "Europe/Berlin"])
async def test_rolling_week_series_spans_eight_calendar_days(
    mass: MusicAssistant, timezone: str
) -> None:
    """A 7-day window starting mid-day fills every calendar day it touches through today."""
    user = await mass.webserver.auth.create_user("statistics-week-series")
    await _insert_plays(
        mass, user, ("2027-01-08T09:00:00", "2027-01-08T12:00:00", "2027-01-12T12:00:00")
    )

    with _as_user(user), _frozen_now("2027-01-15T10:00:00"):
        results = await mass.statistics.get_plays_over_time(
            period="week", granularity="day", timezone=timezone
        )

    assert [(point.timestamp, point.value) for point in results] == [
        ("2027-01-08", 1),
        ("2027-01-09", 0),
        ("2027-01-10", 0),
        ("2027-01-11", 0),
        ("2027-01-12", 1),
        ("2027-01-13", 0),
        ("2027-01-14", 0),
        ("2027-01-15", 0),
    ]


async def test_rolling_series_steps_days_and_weeks_across_daylight_saving(
    mass: MusicAssistant,
) -> None:
    """Day and week buckets step by calendar date, so a clock change neither skips nor repeats."""
    user = await mass.webserver.auth.create_user("statistics-dst-days")
    await _insert_plays(mass, user, ("2027-03-27T12:00:00",))

    with _as_user(user), _frozen_now("2027-03-30T10:00:00"):
        days = await mass.statistics.get_plays_over_time(
            period="week", granularity="day", timezone="Europe/Berlin"
        )
        weeks = await mass.statistics.get_plays_over_time(
            period="month", granularity="week", timezone="Europe/Berlin"
        )

    assert [point.timestamp for point in days] == [f"2027-03-{day:02d}" for day in range(23, 31)]
    assert [(point.timestamp, point.value) for point in weeks] == [
        ("2027-02-22", 0),
        ("2027-03-01", 0),
        ("2027-03-08", 0),
        ("2027-03-15", 0),
        ("2027-03-22", 1),
        ("2027-03-29", 0),
    ]


@pytest.mark.parametrize("timezone", ["UTC", "Europe/Berlin"])
async def test_all_time_series_fills_months_from_first_play(
    mass: MusicAssistant, timezone: str
) -> None:
    """All-time months run from the first month with plays through the current month."""
    user = await mass.webserver.auth.create_user("statistics-all-time-series")
    await _insert_plays(mass, user, ("2026-11-10T12:00:00", "2027-02-03T12:00:00"))

    with _as_user(user), _frozen_now("2027-04-15T12:00:00"):
        results = await mass.statistics.get_plays_over_time(
            period="all_time", granularity="month", timezone=timezone
        )

    assert [(point.timestamp, point.value) for point in results] == [
        ("2026-11", 1),
        ("2026-12", 0),
        ("2027-01", 0),
        ("2027-02", 1),
        ("2027-03", 0),
        ("2027-04", 0),
    ]


@pytest.mark.parametrize("timezone", ["UTC", "Europe/Berlin"])
@pytest.mark.parametrize(
    ("period", "granularity"),
    [("today", "hour"), ("week", "day"), ("year", "week"), ("all_time", "month")],
)
async def test_series_is_empty_without_plays_in_period(
    mass: MusicAssistant, timezone: str, period: str, granularity: str
) -> None:
    """No plays in the period returns no buckets, so clients can show their empty state."""
    user = await mass.webserver.auth.create_user("statistics-empty-series")
    with _as_user(user), _frozen_now("2027-01-15T10:00:00"):
        empty_history = await mass.statistics.get_plays_over_time(
            period=period, granularity=granularity, timezone=timezone
        )
    await _insert_plays(mass, user, ("2020-01-01T12:00:00",))
    with _as_user(user), _frozen_now("2027-01-15T10:00:00"):
        older_history = await mass.statistics.get_plays_over_time(
            period=period, granularity=granularity, timezone=timezone
        )

    assert empty_history == []
    if period != "all_time":
        assert older_history == []


async def test_plays_over_time_rejects_unsupported_parameters(mass: MusicAssistant) -> None:
    """Unsupported chart parameters do not silently return default data."""
    user = await mass.webserver.auth.create_user("statistics-invalid-granularity")

    with _as_user(user):
        with pytest.raises(InvalidDataError):
            await mass.statistics.get_plays_over_time(granularity="fortnight")
        with pytest.raises(InvalidDataError):
            await mass.statistics.get_plays_over_time(period="fortnight")
