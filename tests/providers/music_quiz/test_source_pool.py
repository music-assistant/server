"""Tests for resolving configured source URIs into the round track pool."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, call

import pytest
from music_assistant_models.enums import MediaType
from music_assistant_models.errors import InvalidDataError
from music_assistant_models.media_items import BrowseFolder, Track

from music_assistant.providers.music_quiz.errors import TRANSLATION_OWNER
from music_assistant.providers.music_quiz.models import MusicQuizConfig
from music_assistant.providers.music_quiz.quiz_types.guess_the_song import GuessTheSongQuizType

SUPPORTED_MEDIA_TYPES = (
    MediaType.TRACK,
    MediaType.PLAYLIST,
    MediaType.GENRE,
    MediaType.ALBUM,
    MediaType.ARTIST,
)
NAMED_UNSUPPORTED_MEDIA_TYPES = (
    MediaType.RADIO,
    MediaType.PODCAST,
    MediaType.PODCAST_EPISODE,
    MediaType.AUDIOBOOK,
)
OTHER_UNSUPPORTED_MEDIA_TYPES = tuple(
    media_type
    for media_type in MediaType
    if media_type not in SUPPORTED_MEDIA_TYPES and media_type not in NAMED_UNSUPPORTED_MEDIA_TYPES
)


def _track(item_id: str) -> Track:
    """Return a minimal track."""
    return Track(
        item_id=item_id,
        provider="prov",
        name=f"Track {item_id}",
        provider_mappings=set(),
    )


def _media_item(media_type: MediaType) -> MagicMock:
    """Return a media item mock of the requested type."""
    item = MagicMock()
    item.media_type = media_type
    return item


def _quiz_type(source_uris: list[str]) -> tuple[GuessTheSongQuizType, MagicMock]:
    """Return a quiz type with a mock MusicAssistant for the given sources."""
    mass = MagicMock()
    config = MusicQuizConfig(source_uris=source_uris)
    return GuessTheSongQuizType(mass, config), mass


@pytest.mark.parametrize("media_type", SUPPORTED_MEDIA_TYPES, ids=lambda value: value.value)
async def test_source_pool_accepts_supported_media_type(media_type: MediaType) -> None:
    """Each supported media type is resolved through the playback resolver."""
    quiz_type, mass = _quiz_type([f"prov://{media_type.value}/source"])
    media_item = _media_item(media_type)
    track = _track(media_type.value)
    mass.music.get_item_by_uri = AsyncMock(return_value=media_item)
    mass.player_queues.get_tracks_for_playback = AsyncMock(return_value=[track])

    pool = await quiz_type._get_source_track_pool()

    assert [item.item_id for item in pool.values()] == [media_type.value]
    mass.player_queues.get_tracks_for_playback.assert_awaited_once_with(media_item)


@pytest.mark.parametrize(
    "media_type",
    [*NAMED_UNSUPPORTED_MEDIA_TYPES, *OTHER_UNSUPPORTED_MEDIA_TYPES],
    ids=lambda value: value.value,
)
async def test_source_pool_skips_every_unsupported_media_type(media_type: MediaType) -> None:
    """Unsupported media types are skipped before playback resolution."""
    quiz_type, mass = _quiz_type([f"prov://{media_type.value}/unsupported", "prov://track/usable"])
    unsupported_item = _media_item(media_type)
    usable_item = _media_item(MediaType.TRACK)
    usable_track = _track("usable")
    mass.music.get_item_by_uri = AsyncMock(side_effect=[unsupported_item, usable_item])
    mass.player_queues.get_tracks_for_playback = AsyncMock(return_value=[usable_track])

    pool = await quiz_type._get_source_track_pool()

    assert [item.item_id for item in pool.values()] == ["usable"]
    mass.player_queues.get_tracks_for_playback.assert_awaited_once_with(usable_item)


async def test_source_pool_skips_browse_folder() -> None:
    """Browse folders are skipped before playback resolution."""
    quiz_type, mass = _quiz_type(["prov://folder/root", "prov://track/usable"])
    folder = BrowseFolder(item_id="root", provider="prov", name="Root")
    usable_item = _media_item(MediaType.TRACK)
    usable_track = _track("usable")
    mass.music.get_item_by_uri = AsyncMock(side_effect=[folder, usable_item])
    mass.player_queues.get_tracks_for_playback = AsyncMock(return_value=[usable_track])

    pool = await quiz_type._get_source_track_pool()

    assert [item.item_id for item in pool.values()] == ["usable"]
    mass.player_queues.get_tracks_for_playback.assert_awaited_once_with(usable_item)


async def test_source_pool_deduplicates_overlapping_tracks_by_uri() -> None:
    """Overlapping source results contain each track URI once."""
    quiz_type, mass = _quiz_type(["prov://genre/one", "prov://playlist/two"])
    genre = _media_item(MediaType.GENRE)
    playlist = _media_item(MediaType.PLAYLIST)
    first_shared = _track("shared")
    second_shared = _track("shared")
    first_unique = _track("first")
    second_unique = _track("second")
    mass.music.get_item_by_uri = AsyncMock(side_effect=[genre, playlist])
    mass.player_queues.get_tracks_for_playback = AsyncMock(
        side_effect=[
            [first_shared, first_unique],
            [second_shared, second_unique],
        ]
    )

    pool = await quiz_type._get_source_track_pool()

    assert first_shared.uri is not None
    assert set(pool) == {
        first_shared.uri,
        first_unique.uri,
        second_unique.uri,
    }
    assert pool[first_shared.uri] is second_shared


async def test_source_pool_tolerates_partial_failures_and_unsupported_sources() -> None:
    """Usable sources still populate the pool when other sources cannot."""
    quiz_type, mass = _quiz_type(
        [
            "prov://genre/missing",
            "prov://album/failing",
            "prov://radio/unsupported",
            "prov://track/usable",
        ]
    )
    failing_album = _media_item(MediaType.ALBUM)
    unsupported_radio = _media_item(MediaType.RADIO)
    usable_item = _media_item(MediaType.TRACK)
    usable_track = _track("usable")
    mass.music.get_item_by_uri = AsyncMock(
        side_effect=[
            RuntimeError("missing"),
            failing_album,
            unsupported_radio,
            usable_item,
        ]
    )
    mass.player_queues.get_tracks_for_playback = AsyncMock(
        side_effect=[RuntimeError("failed"), [usable_track]]
    )

    pool = await quiz_type._get_source_track_pool()

    assert [item.item_id for item in pool.values()] == ["usable"]
    assert mass.player_queues.get_tracks_for_playback.await_args_list == [
        call(failing_album),
        call(usable_item),
    ]


async def test_source_pool_raises_localized_error_when_all_sources_are_unusable() -> None:
    """An unusable source set raises the existing localized error."""
    quiz_type, mass = _quiz_type(
        [
            "prov://genre/missing",
            "prov://radio/unsupported",
            "prov://album/empty",
        ]
    )
    unsupported_radio = _media_item(MediaType.RADIO)
    empty_album = _media_item(MediaType.ALBUM)
    mass.music.get_item_by_uri = AsyncMock(
        side_effect=[RuntimeError("missing"), unsupported_radio, empty_album]
    )
    mass.player_queues.get_tracks_for_playback = AsyncMock(return_value=[])

    with pytest.raises(InvalidDataError) as error:
        await quiz_type._get_source_track_pool()

    assert error.value.translation_key == "music_quiz_sources_unavailable"
    assert error.value.translation_owner == TRANSLATION_OWNER
    mass.player_queues.get_tracks_for_playback.assert_awaited_once_with(empty_album)
