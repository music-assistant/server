"""Test Yandex Music Recommendations."""

from __future__ import annotations

import json
import pathlib
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, Mock, patch

import pytest
from music_assistant_models.enums import MediaType
from music_assistant_models.errors import InvalidDataError
from music_assistant_models.media_items import (
    Album,
    Artist,
    ItemMapping,
    Playlist,
    RecommendationFolder,
    Track,
    UniqueList,
)
from yandex_music import Track as YandexTrack

from music_assistant.providers.yandex_music.constants import (
    MY_WAVE_PLAYLIST_ID,
    RADIO_TRACK_ID_SEP,
    ROTOR_STATION_MY_WAVE,
)
from music_assistant.providers.yandex_music.provider import YandexMusicProvider, _WaveState
from tests.common import use_real_create_task

from .conftest import DE_JSON_CLIENT, provider_dir

_RECOMMENDATION_STRINGS = json.loads((provider_dir() / "strings.json").read_text(encoding="utf-8"))[
    "media"
]["recommendations"]


def _media_item_mock(spec: type) -> Mock:
    """
    Return a media item stand-in that a copy hands back unchanged.

    A cached result is copied per caller, which real media items survive because they
    compare by value. A mock compares by identity, so keep it out of the copy.

    :param spec: The media item class the mock stands in for.
    """
    item = Mock(spec=spec)
    item.__deepcopy__ = lambda _memo: item
    return item


@pytest.fixture
def provider_mock() -> Mock:
    """Return a mock Yandex Music provider."""
    provider = Mock(spec=YandexMusicProvider)
    provider.domain = "yandex_music"
    provider.instance_id = "yandex_music_instance"
    provider.logger = Mock()

    # Mock client
    provider.client = AsyncMock()
    provider.client.user_id = 12345

    # Mock config
    provider.config = Mock()
    provider.config.get_value = Mock(side_effect=lambda key: 150 if "max_tracks" in key else None)

    # Mock mass with cache
    provider.mass = Mock()
    provider.mass.metadata = Mock()
    provider.mass.metadata.locale = "en_US"
    provider.mass.cache = AsyncMock()
    provider.mass.cache.get = AsyncMock(return_value=None)  # Cache always misses
    provider.mass.cache.get_with_freshness = AsyncMock(return_value=(None, False, False))
    provider.mass.cache.set = AsyncMock()
    use_real_create_task(provider.mass)

    # Resolve media labels through the real helper; unauthored keys fall back.
    provider.mass.translations.get_translation = Mock(return_value=None)
    provider._media_label = YandexMusicProvider._media_label.__get__(provider, YandexMusicProvider)

    return provider


def _install_wave_state(provider_mock: Mock) -> _WaveState:
    """Stub _get_wave_state to return a fresh in-memory _WaveState per provider_mock."""
    wave = _WaveState()
    provider_mock._get_wave_state = Mock(return_value=wave)
    return wave


@pytest.mark.asyncio
async def test_get_my_wave_recommendations_success(provider_mock: Mock) -> None:
    """Test _get_my_wave_recommendations returns data when session API provides tracks."""
    _install_wave_state(provider_mock)
    mock_track = Mock()
    mock_track.id = "12345"
    mock_track.track_id = "12345"

    # Mock the session-API helper; return the same track every time — matches
    # the old single-track-per-batch test intent where the fake rotor returns
    # the same shape across repeated batch calls.
    provider_mock._fetch_rotor_session_batch = AsyncMock(return_value=([mock_track], "batch_a"))

    mock_parsed_track = _media_item_mock(Track)
    mock_parsed_track.item_id = f"12345{RADIO_TRACK_ID_SEP}{ROTOR_STATION_MY_WAVE}"
    mock_parsed_track.name = "Test Track"
    mock_parsed_track.provider_mappings = []
    provider_mock._parse_my_wave_track = Mock(return_value=mock_parsed_track)

    result = await YandexMusicProvider._get_my_wave_recommendations(provider_mock)

    assert result is not None
    assert isinstance(result, RecommendationFolder)
    assert result.item_id == MY_WAVE_PLAYLIST_ID
    assert result.provider == provider_mock.instance_id
    assert result.name == _RECOMMENDATION_STRINGS[MY_WAVE_PLAYLIST_ID]["name"]
    assert result.translation_key == MY_WAVE_PLAYLIST_ID
    assert result.icon == "mdi-waveform"
    assert len(result.items) > 0


@pytest.mark.asyncio
async def test_get_my_wave_recommendations_empty(provider_mock: Mock) -> None:
    """Test _get_my_wave_recommendations returns None when session API yields no tracks."""
    _install_wave_state(provider_mock)
    provider_mock._fetch_rotor_session_batch = AsyncMock(return_value=([], None))

    result = await YandexMusicProvider._get_my_wave_recommendations(provider_mock)

    assert result is None


@pytest.mark.asyncio
async def test_get_my_wave_recommendations_duplicate_filtering(provider_mock: Mock) -> None:
    """Test _get_my_wave_recommendations filters duplicate tracks across batches."""
    _install_wave_state(provider_mock)
    mock_track1 = Mock()
    mock_track1.id = "12345"
    mock_track1.track_id = "12345"

    mock_track2 = Mock()
    mock_track2.id = "12345"  # Same ID
    mock_track2.track_id = "12345"

    # First batch returns track1, second batch returns track2 (duplicate)
    provider_mock._fetch_rotor_session_batch = AsyncMock(
        side_effect=[
            ([mock_track1], "batch_a"),
            ([mock_track2], "batch_b"),
        ]
    )

    mock_parsed_track = _media_item_mock(Track)
    mock_parsed_track.item_id = f"12345{RADIO_TRACK_ID_SEP}{ROTOR_STATION_MY_WAVE}"
    mock_parsed_track.name = "Test Track"
    mock_parsed_track.provider_mappings = []

    # _parse_my_wave_track returns track on first call, None on duplicate
    provider_mock._parse_my_wave_track = Mock(side_effect=[mock_parsed_track, None])

    result = await YandexMusicProvider._get_my_wave_recommendations(provider_mock)

    assert result is not None
    # Should only have 1 track despite 2 API calls (duplicate filtered)
    assert len(result.items) == 1


@pytest.mark.asyncio
async def test_get_my_wave_recommendations_invalid_data_error(provider_mock: Mock) -> None:
    """Test _get_my_wave_recommendations handles parse failures gracefully."""
    _install_wave_state(provider_mock)
    mock_track = Mock()
    mock_track.id = "12345"
    mock_track.track_id = "12345"

    provider_mock._fetch_rotor_session_batch = AsyncMock(return_value=([mock_track], "batch_a"))

    # _parse_my_wave_track returns None (simulates parse error handled internally)
    provider_mock._parse_my_wave_track = Mock(return_value=None)

    result = await YandexMusicProvider._get_my_wave_recommendations(provider_mock)

    # Should return None as no valid tracks were parsed
    assert result is None


@pytest.mark.asyncio
async def test_get_feed_recommendations_success(provider_mock: Mock) -> None:
    """Test _get_feed_recommendations returns data when API provides feed."""
    # Mock feed with generated playlists
    mock_gen_playlist = Mock()
    mock_gen_playlist.ready = True
    mock_gen_playlist.data = Mock()  # Playlist data

    mock_feed = Mock()
    mock_feed.generated_playlists = [mock_gen_playlist]

    provider_mock.client.get_feed = AsyncMock(return_value=mock_feed)

    # Mock parse_playlist
    mock_parsed_playlist = Mock(spec=Playlist)
    mock_parsed_playlist.item_id = "playlist_1"
    mock_parsed_playlist.name = "Playlist of the Day"

    with patch(
        "music_assistant.providers.yandex_music.provider.parse_playlist",
        return_value=mock_parsed_playlist,
    ):
        result = await YandexMusicProvider._get_feed_recommendations(provider_mock)

    assert result is not None
    assert isinstance(result, RecommendationFolder)
    assert result.item_id == "feed"
    assert result.provider == provider_mock.instance_id
    assert result.name == _RECOMMENDATION_STRINGS["feed"]["name"]
    assert result.translation_key == "feed"
    assert result.icon == "mdi-account-music"
    assert len(result.items) > 0


@pytest.mark.asyncio
async def test_get_feed_recommendations_empty(provider_mock: Mock) -> None:
    """Test _get_feed_recommendations returns None when feed is empty."""
    provider_mock.client.get_feed = AsyncMock(return_value=None)

    result = await YandexMusicProvider._get_feed_recommendations(provider_mock)

    assert result is None


@pytest.mark.asyncio
async def test_get_feed_recommendations_no_generated_playlists(provider_mock: Mock) -> None:
    """Test _get_feed_recommendations returns None when no generated playlists."""
    mock_feed = Mock()
    mock_feed.generated_playlists = []

    provider_mock.client.get_feed = AsyncMock(return_value=mock_feed)

    result = await YandexMusicProvider._get_feed_recommendations(provider_mock)

    assert result is None


@pytest.mark.asyncio
async def test_get_feed_recommendations_invalid_data_error(provider_mock: Mock) -> None:
    """Test _get_feed_recommendations handles InvalidDataError gracefully."""
    mock_gen_playlist = Mock()
    mock_gen_playlist.ready = True
    mock_gen_playlist.data = Mock()

    mock_feed = Mock()
    mock_feed.generated_playlists = [mock_gen_playlist]

    provider_mock.client.get_feed = AsyncMock(return_value=mock_feed)

    with patch(
        "music_assistant.providers.yandex_music.provider.parse_playlist",
        side_effect=InvalidDataError("Parse error"),
    ):
        result = await YandexMusicProvider._get_feed_recommendations(provider_mock)

    assert result is None
    provider_mock.logger.debug.assert_called()


@pytest.mark.asyncio
async def test_get_chart_recommendations_success(provider_mock: Mock) -> None:
    """Test _get_chart_recommendations returns data when API provides chart."""
    # Mock TrackShort with .track attribute
    mock_track_short = Mock()
    mock_track_obj = Mock()  # The actual Track object
    mock_track_short.track = mock_track_obj

    mock_chart = Mock()
    mock_chart.tracks = [mock_track_short]

    mock_chart_info = Mock()
    mock_chart_info.chart = mock_chart

    provider_mock.client.get_chart = AsyncMock(return_value=mock_chart_info)

    # Mock parse_track
    mock_parsed_track = _media_item_mock(Track)
    mock_parsed_track.item_id = "track_1"
    mock_parsed_track.name = "Chart Track 1"

    with patch(
        "music_assistant.providers.yandex_music.provider.parse_track",
        return_value=mock_parsed_track,
    ):
        result = await YandexMusicProvider._get_chart_recommendations(provider_mock)

    assert result is not None
    assert isinstance(result, RecommendationFolder)
    assert result.item_id == "chart"
    assert result.provider == provider_mock.instance_id
    assert result.name == _RECOMMENDATION_STRINGS["chart"]["name"]
    assert result.translation_key == "chart"
    assert result.icon == "mdi-chart-line"
    assert len(result.items) > 0


@pytest.mark.asyncio
async def test_get_chart_recommendations_empty(provider_mock: Mock) -> None:
    """Test _get_chart_recommendations returns None when chart is empty."""
    provider_mock.client.get_chart = AsyncMock(return_value=None)

    result = await YandexMusicProvider._get_chart_recommendations(provider_mock)

    assert result is None


@pytest.mark.asyncio
async def test_get_chart_recommendations_no_tracks(provider_mock: Mock) -> None:
    """Test _get_chart_recommendations returns None when chart has no tracks."""
    mock_chart = Mock()
    mock_chart.tracks = []

    mock_chart_info = Mock()
    mock_chart_info.chart = mock_chart

    provider_mock.client.get_chart = AsyncMock(return_value=mock_chart_info)

    result = await YandexMusicProvider._get_chart_recommendations(provider_mock)

    assert result is None


@pytest.mark.asyncio
async def test_get_chart_recommendations_invalid_data_error(provider_mock: Mock) -> None:
    """Test _get_chart_recommendations handles InvalidDataError gracefully."""
    mock_track_short = Mock()
    mock_track_obj = Mock()
    mock_track_short.track = mock_track_obj

    mock_chart = Mock()
    mock_chart.tracks = [mock_track_short]

    mock_chart_info = Mock()
    mock_chart_info.chart = mock_chart

    provider_mock.client.get_chart = AsyncMock(return_value=mock_chart_info)

    with patch(
        "music_assistant.providers.yandex_music.provider.parse_track",
        side_effect=InvalidDataError("Parse error"),
    ):
        result = await YandexMusicProvider._get_chart_recommendations(provider_mock)

    assert result is None
    provider_mock.logger.debug.assert_called()


@pytest.mark.asyncio
async def test_get_new_releases_recommendations_success(provider_mock: Mock) -> None:
    """Test _get_new_releases_recommendations returns data when API provides releases."""
    # Mock releases with album IDs
    mock_releases = Mock()
    mock_releases.new_releases = [123, 456, 789]

    provider_mock.client.get_new_releases = AsyncMock(return_value=mock_releases)

    # Mock get_albums to return album objects
    mock_album = Mock()
    provider_mock.client.get_albums = AsyncMock(return_value=[mock_album])

    # Mock parse_album
    mock_parsed_album = Mock(spec=Album)
    mock_parsed_album.item_id = "album_1"
    mock_parsed_album.name = "New Album"

    with patch(
        "music_assistant.providers.yandex_music.provider.parse_album",
        return_value=mock_parsed_album,
    ):
        result = await YandexMusicProvider._get_new_releases_recommendations(provider_mock)

    assert result is not None
    assert isinstance(result, RecommendationFolder)
    assert result.item_id == "new_releases"
    assert result.provider == provider_mock.instance_id
    assert result.name == _RECOMMENDATION_STRINGS["new_releases"]["name"]
    assert result.translation_key == "new_releases"
    assert result.icon == "mdi-new-box"
    assert len(result.items) > 0


@pytest.mark.asyncio
async def test_get_new_releases_recommendations_empty(provider_mock: Mock) -> None:
    """Test _get_new_releases_recommendations returns None when releases are empty."""
    provider_mock.client.get_new_releases = AsyncMock(return_value=None)

    result = await YandexMusicProvider._get_new_releases_recommendations(provider_mock)

    assert result is None


@pytest.mark.asyncio
async def test_get_new_releases_recommendations_no_releases(provider_mock: Mock) -> None:
    """Test _get_new_releases_recommendations returns None when no releases."""
    mock_releases = Mock()
    mock_releases.new_releases = []

    provider_mock.client.get_new_releases = AsyncMock(return_value=mock_releases)

    result = await YandexMusicProvider._get_new_releases_recommendations(provider_mock)

    assert result is None


@pytest.mark.asyncio
async def test_get_new_releases_recommendations_invalid_data_error(provider_mock: Mock) -> None:
    """Test _get_new_releases_recommendations handles InvalidDataError gracefully."""
    mock_releases = Mock()
    mock_releases.new_releases = [123]

    provider_mock.client.get_new_releases = AsyncMock(return_value=mock_releases)
    provider_mock.client.get_albums = AsyncMock(return_value=[Mock()])

    with patch(
        "music_assistant.providers.yandex_music.provider.parse_album",
        side_effect=InvalidDataError("Parse error"),
    ):
        result = await YandexMusicProvider._get_new_releases_recommendations(provider_mock)

    assert result is None
    provider_mock.logger.debug.assert_called()


@pytest.mark.asyncio
async def test_get_new_playlists_recommendations_success(provider_mock: Mock) -> None:
    """Test _get_new_playlists_recommendations returns data when API provides playlists."""
    # Mock playlist ID object
    mock_playlist_id = Mock()
    mock_playlist_id.uid = "user123"
    mock_playlist_id.kind = "456"

    mock_result = Mock()
    mock_result.new_playlists = [mock_playlist_id]

    provider_mock.client.get_new_playlists = AsyncMock(return_value=mock_result)

    # Mock get_playlists to return playlist objects
    mock_playlist = Mock()
    provider_mock.client.get_playlists = AsyncMock(return_value=[mock_playlist])

    # Mock parse_playlist
    mock_parsed_playlist = Mock(spec=Playlist)
    mock_parsed_playlist.item_id = "playlist_1"
    mock_parsed_playlist.name = "New Playlist"

    with patch(
        "music_assistant.providers.yandex_music.provider.parse_playlist",
        return_value=mock_parsed_playlist,
    ):
        result = await YandexMusicProvider._get_new_playlists_recommendations(provider_mock)

    assert result is not None
    assert isinstance(result, RecommendationFolder)
    assert result.item_id == "new_playlists"
    assert result.provider == provider_mock.instance_id
    assert result.name == _RECOMMENDATION_STRINGS["new_playlists"]["name"]
    assert result.translation_key == "new_playlists"
    assert result.icon == "mdi-playlist-star"
    assert len(result.items) > 0


@pytest.mark.asyncio
async def test_get_new_playlists_recommendations_empty(provider_mock: Mock) -> None:
    """Test _get_new_playlists_recommendations returns None when result is empty."""
    provider_mock.client.get_new_playlists = AsyncMock(return_value=None)

    result = await YandexMusicProvider._get_new_playlists_recommendations(provider_mock)

    assert result is None


@pytest.mark.asyncio
async def test_get_new_playlists_recommendations_no_playlists(provider_mock: Mock) -> None:
    """Test _get_new_playlists_recommendations returns None when no playlists."""
    mock_result = Mock()
    mock_result.new_playlists = []

    provider_mock.client.get_new_playlists = AsyncMock(return_value=mock_result)

    result = await YandexMusicProvider._get_new_playlists_recommendations(provider_mock)

    assert result is None


@pytest.mark.asyncio
async def test_get_new_playlists_recommendations_invalid_data_error(provider_mock: Mock) -> None:
    """Test _get_new_playlists_recommendations handles InvalidDataError gracefully."""
    mock_playlist_id = Mock()
    mock_playlist_id.uid = "user123"
    mock_playlist_id.kind = "456"

    mock_result = Mock()
    mock_result.new_playlists = [mock_playlist_id]

    provider_mock.client.get_new_playlists = AsyncMock(return_value=mock_result)
    provider_mock.client.get_playlists = AsyncMock(return_value=[Mock()])

    with patch(
        "music_assistant.providers.yandex_music.provider.parse_playlist",
        side_effect=InvalidDataError("Parse error"),
    ):
        result = await YandexMusicProvider._get_new_playlists_recommendations(provider_mock)

    assert result is None
    provider_mock.logger.debug.assert_called()


@pytest.mark.asyncio
async def test_get_top_picks_recommendations_success(provider_mock: Mock) -> None:
    """Test _get_top_picks_recommendations returns data when API provides playlists."""
    mock_playlist = Mock()
    provider_mock.client.get_tag_playlists = AsyncMock(return_value=[mock_playlist])

    # Mock parse_playlist
    mock_parsed_playlist = Mock(spec=Playlist)
    mock_parsed_playlist.item_id = "playlist_1"
    mock_parsed_playlist.name = "Top Pick"

    with patch(
        "music_assistant.providers.yandex_music.provider.parse_playlist",
        return_value=mock_parsed_playlist,
    ):
        result = await YandexMusicProvider._get_top_picks_recommendations(provider_mock)

    assert result is not None
    assert isinstance(result, RecommendationFolder)
    assert result.item_id == "top_picks"
    assert result.provider == provider_mock.instance_id
    assert result.name == _RECOMMENDATION_STRINGS["top_picks"]["name"]
    assert result.translation_key == "top_picks"
    assert result.icon == "mdi-star"
    assert len(result.items) > 0
    # Verify it called with "top" tag
    provider_mock.client.get_tag_playlists.assert_called_once_with("top")


@pytest.mark.asyncio
async def test_get_top_picks_recommendations_empty(provider_mock: Mock) -> None:
    """Test _get_top_picks_recommendations returns None when API returns empty."""
    provider_mock.client.get_tag_playlists = AsyncMock(return_value=[])

    result = await YandexMusicProvider._get_top_picks_recommendations(provider_mock)

    assert result is None


@pytest.mark.asyncio
async def test_get_top_picks_recommendations_invalid_data_error(provider_mock: Mock) -> None:
    """Test _get_top_picks_recommendations handles InvalidDataError gracefully."""
    provider_mock.client.get_tag_playlists = AsyncMock(return_value=[Mock()])

    with patch(
        "music_assistant.providers.yandex_music.provider.parse_playlist",
        side_effect=InvalidDataError("Parse error"),
    ):
        result = await YandexMusicProvider._get_top_picks_recommendations(provider_mock)

    assert result is None
    provider_mock.logger.debug.assert_called()


@pytest.mark.asyncio
async def test_get_mood_mix_recommendations_success(provider_mock: Mock) -> None:
    """Test _get_mood_mix_recommendations returns data with deterministic random choice."""
    mock_playlist = Mock()
    provider_mock.client.get_tag_playlists = AsyncMock(return_value=[mock_playlist])

    # Mock parse_playlist
    mock_parsed_playlist = Mock(spec=Playlist)
    mock_parsed_playlist.item_id = "playlist_1"
    mock_parsed_playlist.name = "Chill Playlist"

    # No need to patch random.choice - tag is now passed as argument
    with patch(
        "music_assistant.providers.yandex_music.provider.parse_playlist",
        return_value=mock_parsed_playlist,
    ):
        result = await YandexMusicProvider._get_mood_mix_recommendations(provider_mock, "chill")

    assert result is not None
    assert isinstance(result, RecommendationFolder)
    assert result.item_id == "mood_mix"
    assert result.provider == provider_mock.instance_id
    # Name should include the mood tag
    assert "Chill" in result.name or "chill" in result.name.lower()
    assert result.icon == "mdi-emoticon-outline"
    assert len(result.items) > 0
    # Verify it called with mood tag
    provider_mock.client.get_tag_playlists.assert_called_once_with("chill")


@pytest.mark.asyncio
async def test_get_mood_mix_recommendations_empty(provider_mock: Mock) -> None:
    """Test _get_mood_mix_recommendations returns None when API returns empty."""
    provider_mock.client.get_tag_playlists = AsyncMock(return_value=[])

    result = await YandexMusicProvider._get_mood_mix_recommendations(provider_mock, "sad")

    assert result is None


@pytest.mark.asyncio
async def test_get_mood_mix_recommendations_invalid_data_error(provider_mock: Mock) -> None:
    """Test _get_mood_mix_recommendations handles InvalidDataError gracefully."""
    provider_mock.client.get_tag_playlists = AsyncMock(return_value=[Mock()])

    with patch(
        "music_assistant.providers.yandex_music.provider.parse_playlist",
        side_effect=InvalidDataError("Parse error"),
    ):
        result = await YandexMusicProvider._get_mood_mix_recommendations(provider_mock, "romantic")

    assert result is None
    provider_mock.logger.debug.assert_called()


@pytest.mark.asyncio
async def test_get_activity_mix_recommendations_success(provider_mock: Mock) -> None:
    """Test _get_activity_mix_recommendations returns data with deterministic random choice."""
    mock_playlist = Mock()
    provider_mock.client.get_tag_playlists = AsyncMock(return_value=[mock_playlist])

    # Mock parse_playlist
    mock_parsed_playlist = Mock(spec=Playlist)
    mock_parsed_playlist.item_id = "playlist_1"
    mock_parsed_playlist.name = "Workout Playlist"

    # No need to patch random.choice - tag is now passed as argument
    with patch(
        "music_assistant.providers.yandex_music.provider.parse_playlist",
        return_value=mock_parsed_playlist,
    ):
        result = await YandexMusicProvider._get_activity_mix_recommendations(
            provider_mock, "workout"
        )

    assert result is not None
    assert isinstance(result, RecommendationFolder)
    assert result.item_id == "activity_mix"
    assert result.provider == provider_mock.instance_id
    # Name should include the activity tag
    assert "Workout" in result.name or "workout" in result.name.lower()
    assert result.icon == "mdi-run"
    assert len(result.items) > 0
    # Verify it called with activity tag
    provider_mock.client.get_tag_playlists.assert_called_once_with("workout")


@pytest.mark.asyncio
async def test_get_activity_mix_recommendations_empty(provider_mock: Mock) -> None:
    """Test _get_activity_mix_recommendations returns None when API returns empty."""
    provider_mock.client.get_tag_playlists = AsyncMock(return_value=[])

    result = await YandexMusicProvider._get_activity_mix_recommendations(provider_mock, "focus")

    assert result is None


@pytest.mark.asyncio
async def test_get_activity_mix_recommendations_invalid_data_error(provider_mock: Mock) -> None:
    """Test _get_activity_mix_recommendations handles InvalidDataError gracefully."""
    provider_mock.client.get_tag_playlists = AsyncMock(return_value=[Mock()])

    with patch(
        "music_assistant.providers.yandex_music.provider.parse_playlist",
        side_effect=InvalidDataError("Parse error"),
    ):
        result = await YandexMusicProvider._get_activity_mix_recommendations(
            provider_mock, "morning"
        )

    assert result is None
    provider_mock.logger.debug.assert_called()


@pytest.mark.asyncio
async def test_get_seasonal_mix_recommendations_winter(provider_mock: Mock) -> None:
    """Test _get_seasonal_mix_recommendations returns winter playlists in January."""
    mock_playlist = Mock()
    provider_mock.client.get_tag_playlists = AsyncMock(return_value=[mock_playlist])

    # Mock parse_playlist
    mock_parsed_playlist = Mock(spec=Playlist)
    mock_parsed_playlist.item_id = "playlist_1"
    mock_parsed_playlist.name = "Winter Playlist"

    # Patch datetime to return January (month 1)
    mock_utc = Mock()
    mock_utc.return_value.month = 1

    with (
        patch("music_assistant.providers.yandex_music.provider.utc", mock_utc),
        patch(
            "music_assistant.providers.yandex_music.provider.parse_playlist",
            return_value=mock_parsed_playlist,
        ),
    ):
        result = await YandexMusicProvider._get_seasonal_mix_recommendations(provider_mock)

    assert result is not None
    assert isinstance(result, RecommendationFolder)
    assert result.item_id == "seasonal_mix"
    assert result.provider == provider_mock.instance_id
    # Name should include winter
    assert "Winter" in result.name or "winter" in result.name.lower()
    assert result.icon == "mdi-weather-sunny"
    assert len(result.items) > 0
    # Verify it called with winter tag
    provider_mock.client.get_tag_playlists.assert_called_once_with("winter")


@pytest.mark.asyncio
async def test_get_seasonal_mix_recommendations_summer(provider_mock: Mock) -> None:
    """Test _get_seasonal_mix_recommendations returns summer playlists in July."""
    mock_playlist = Mock()
    provider_mock.client.get_tag_playlists = AsyncMock(return_value=[mock_playlist])

    mock_parsed_playlist = Mock(spec=Playlist)
    mock_parsed_playlist.item_id = "playlist_1"
    mock_parsed_playlist.name = "Summer Playlist"

    # Patch datetime to return July (month 7)
    mock_utc = Mock()
    mock_utc.return_value.month = 7

    with (
        patch("music_assistant.providers.yandex_music.provider.utc", mock_utc),
        patch(
            "music_assistant.providers.yandex_music.provider.parse_playlist",
            return_value=mock_parsed_playlist,
        ),
    ):
        result = await YandexMusicProvider._get_seasonal_mix_recommendations(provider_mock)

    assert result is not None
    # Verify it called with summer tag
    provider_mock.client.get_tag_playlists.assert_called_once_with("summer")


@pytest.mark.asyncio
async def test_get_seasonal_mix_recommendations_spring_fallback(provider_mock: Mock) -> None:
    """Spring with no playlists falls back to autumn (Yandex coverage gap)."""
    mock_playlist = Mock()
    # First call (spring) → empty; second call (autumn) → has playlists
    provider_mock.client.get_tag_playlists = AsyncMock(
        side_effect=[[], [mock_playlist]],
    )

    mock_parsed_playlist = Mock(spec=Playlist)
    mock_parsed_playlist.item_id = "playlist_1"
    mock_parsed_playlist.name = "Autumn Playlist"

    # Patch datetime to return March (month 3 - spring)
    mock_utc = Mock()
    mock_utc.return_value.month = 3

    with (
        patch("music_assistant.providers.yandex_music.provider.utc", mock_utc),
        patch(
            "music_assistant.providers.yandex_music.provider.parse_playlist",
            return_value=mock_parsed_playlist,
        ),
    ):
        result = await YandexMusicProvider._get_seasonal_mix_recommendations(provider_mock)

    assert result is not None
    # Verify call sequence: spring first, autumn fallback after empty result
    assert provider_mock.client.get_tag_playlists.await_args_list[0].args == ("spring",)
    assert provider_mock.client.get_tag_playlists.await_args_list[1].args == ("autumn",)


@pytest.mark.asyncio
async def test_get_seasonal_mix_recommendations_empty(provider_mock: Mock) -> None:
    """Test _get_seasonal_mix_recommendations returns None when API returns empty."""
    provider_mock.client.get_tag_playlists = AsyncMock(return_value=[])

    mock_utc = Mock()
    mock_utc.return_value.month = 6

    with patch("music_assistant.providers.yandex_music.provider.utc", mock_utc):
        result = await YandexMusicProvider._get_seasonal_mix_recommendations(provider_mock)

    assert result is None


@pytest.mark.asyncio
async def test_get_seasonal_mix_recommendations_invalid_data_error(provider_mock: Mock) -> None:
    """Test _get_seasonal_mix_recommendations handles InvalidDataError gracefully."""
    provider_mock.client.get_tag_playlists = AsyncMock(return_value=[Mock()])

    mock_utc = Mock()
    mock_utc.return_value.month = 9

    with (
        patch("music_assistant.providers.yandex_music.provider.utc", mock_utc),
        patch(
            "music_assistant.providers.yandex_music.provider.parse_playlist",
            side_effect=InvalidDataError("Parse error"),
        ),
    ):
        result = await YandexMusicProvider._get_seasonal_mix_recommendations(provider_mock)

    assert result is None
    provider_mock.logger.debug.assert_called()


# Expected row ids in the order get_recommendations() returns them.
ROW_IDS = [
    MY_WAVE_PLAYLIST_ID,
    "feed",
    "chart",
    "new_releases",
    "new_playlists",
    "top_picks",
    "mood_mix",
    "activity_mix",
    "seasonal_mix",
]


def _install_row_helper_mocks(provider_mock: Mock) -> dict[str, AsyncMock]:
    """Stub each row-building helper to return a folder holding one sentinel item."""
    helpers = {
        MY_WAVE_PLAYLIST_ID: "_get_my_wave_recommendations",
        "feed": "_get_feed_recommendations",
        "chart": "_get_chart_recommendations",
        "new_releases": "_get_new_releases_recommendations",
        "new_playlists": "_get_new_playlists_recommendations",
        "top_picks": "_get_top_picks_recommendations",
        "mood_mix": "_get_mood_mix_recommendations",
        "activity_mix": "_get_activity_mix_recommendations",
        "seasonal_mix": "_get_seasonal_mix_recommendations",
    }
    mocks: dict[str, AsyncMock] = {}
    for item_id, attr in helpers.items():
        folder = Mock(spec=RecommendationFolder)
        folder.item_id = item_id
        folder.items = UniqueList(
            [
                ItemMapping(
                    media_type=MediaType.TRACK,
                    item_id=f"{item_id}_item",
                    provider=provider_mock.instance_id,
                    name=f"{item_id} item",
                )
            ]
        )
        mock = AsyncMock(return_value=folder)
        setattr(provider_mock, attr, mock)
        mocks[item_id] = mock
    provider_mock._get_valid_tags_for_category = AsyncMock(return_value=["test_tag"])
    # run the real deterministic tag resolution; with a single valid tag it always
    # resolves to "test_tag" so the assertions below stay meaningful
    provider_mock._rotating_row_tag = YandexMusicProvider._rotating_row_tag.__get__(
        provider_mock, YandexMusicProvider
    )
    return mocks


@pytest.mark.asyncio
async def test_get_recommendations_returns_static_rows_without_backend_calls(
    provider_mock: Mock,
) -> None:
    """get_recommendations() returns all nine row descriptors with zero backend I/O."""
    row_mocks = _install_row_helper_mocks(provider_mock)

    # Pin the month so the locally derived seasonal title is deterministic (January -> winter).
    mock_utc = Mock()
    mock_utc.return_value.month = 1
    with patch("music_assistant.providers.yandex_music.provider.utc", mock_utc):
        result = await YandexMusicProvider.get_recommendations(provider_mock)

    assert [f.item_id for f in result] == ROW_IDS
    # Rows are descriptors only: no items, no backend calls, no row-helper calls.
    assert all(not f.items for f in result)
    assert provider_mock.client.mock_calls == []
    for mock in row_mocks.values():
        mock.assert_not_awaited()
    provider_mock._get_valid_tags_for_category.assert_not_awaited()

    by_id = {f.item_id: f for f in result}
    for item_id in ROW_IDS[:-1]:
        assert by_id[item_id].name == _RECOMMENDATION_STRINGS[item_id]["name"]
        assert by_id[item_id].translation_key == item_id
    # Mood/Activity row titles are static; the rotating tag only shows as subtitle
    # once the tag-list cache is warm (the mocked cache misses here, so no subtitle).
    assert by_id["mood_mix"].name == "Mood Mix"
    assert by_id["activity_mix"].name == "Activity Mix"
    assert by_id["seasonal_mix"].name == "Seasonal: Winter"
    assert by_id["seasonal_mix"].translation_key == "seasonal_mix"
    assert by_id["seasonal_mix"].translation_params == ["Winter"]
    assert [f.icon for f in result] == [
        "mdi-waveform",
        "mdi-account-music",
        "mdi-chart-line",
        "mdi-new-box",
        "mdi-playlist-star",
        "mdi-star",
        "mdi-emoticon-outline",
        "mdi-run",
        "mdi-weather-sunny",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("item_id", ROW_IDS)
async def test_get_recommendation_items_routes_to_single_row_helper(
    provider_mock: Mock, item_id: str
) -> None:
    """get_recommendation_items(row) awaits only that row's helper and returns its items."""
    row_mocks = _install_row_helper_mocks(provider_mock)

    result = await YandexMusicProvider.get_recommendation_items(provider_mock, item_id)

    row_mocks[item_id].assert_awaited_once()
    for other_id, mock in row_mocks.items():
        if other_id != item_id:
            mock.assert_not_awaited()
    assert list(result) == list(row_mocks[item_id].return_value.items)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("item_id", "category"),
    [("mood_mix", "mood"), ("activity_mix", "activity")],
)
async def test_get_recommendation_items_picks_tag_outside_cached_helper(
    provider_mock: Mock, item_id: str, category: str
) -> None:
    """Mood/Activity items derive their rotating tag and pass it to the cached row helper."""
    row_mocks = _install_row_helper_mocks(provider_mock)

    await YandexMusicProvider.get_recommendation_items(provider_mock, item_id)

    provider_mock._get_valid_tags_for_category.assert_awaited_once_with(category)
    row_mocks[item_id].assert_awaited_once_with("test_tag")


@pytest.mark.asyncio
async def test_get_recommendation_items_no_tag_returns_empty(provider_mock: Mock) -> None:
    """No valid mood tag yields an empty result without calling the mood helper."""
    row_mocks = _install_row_helper_mocks(provider_mock)
    provider_mock._get_valid_tags_for_category = AsyncMock(return_value=[])

    result = await YandexMusicProvider.get_recommendation_items(provider_mock, "mood_mix")

    assert list(result) == []
    row_mocks["mood_mix"].assert_not_awaited()


@pytest.mark.asyncio
async def test_get_recommendation_items_empty_row_returns_empty(provider_mock: Mock) -> None:
    """A row whose helper yields no folder returns an empty list."""
    _install_row_helper_mocks(provider_mock)
    provider_mock._get_chart_recommendations = AsyncMock(return_value=None)

    result = await YandexMusicProvider.get_recommendation_items(provider_mock, "chart")

    assert list(result) == []


@pytest.mark.asyncio
async def test_get_recommendation_items_unknown_id_returns_empty(provider_mock: Mock) -> None:
    """An unknown row item_id returns an empty list without awaiting any helper."""
    row_mocks = _install_row_helper_mocks(provider_mock)

    result = await YandexMusicProvider.get_recommendation_items(provider_mock, "no_such_row")

    assert list(result) == []
    for mock in row_mocks.values():
        mock.assert_not_awaited()
    assert provider_mock.client.mock_calls == []


@pytest.mark.asyncio
async def test_get_recommendation_items_chart_triggers_only_chart_fetch(
    provider_mock: Mock,
) -> None:
    """items("chart") issues only the chart backend fetch — no other row's fetch fires."""
    # Bind the real cached helper so the call goes through to the (mocked) client.
    provider_mock._get_chart_recommendations = (
        YandexMusicProvider._get_chart_recommendations.__get__(provider_mock)
    )
    mock_track_short = Mock()
    mock_track_short.track = Mock()
    mock_chart = Mock()
    mock_chart.tracks = [mock_track_short]
    mock_chart_info = Mock()
    mock_chart_info.chart = mock_chart
    provider_mock.client.get_chart = AsyncMock(return_value=mock_chart_info)

    mock_parsed_track = _media_item_mock(Track)
    mock_parsed_track.item_id = "track_1"
    with patch(
        "music_assistant.providers.yandex_music.provider.parse_track",
        return_value=mock_parsed_track,
    ):
        result = await YandexMusicProvider.get_recommendation_items(provider_mock, "chart")

    assert list(result) == [mock_parsed_track]
    provider_mock.client.get_chart.assert_awaited_once()
    provider_mock.client.get_feed.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_similar_artists_returns_parsed(provider_mock: Mock) -> None:
    """get_similar_artists parses each artist from the underlying client."""
    yandex_artists = [Mock(), Mock(), Mock()]
    provider_mock.client.get_similar_artists = AsyncMock(return_value=yandex_artists)

    parsed = [_media_item_mock(Artist) for _ in yandex_artists]
    with patch(
        "music_assistant.providers.yandex_music.provider.parse_artist",
        side_effect=parsed,
    ):
        result = await YandexMusicProvider.get_similar_artists(provider_mock, "42", limit=10)

    provider_mock.client.get_similar_artists.assert_awaited_once_with("42", limit=10)
    assert result == parsed


@pytest.mark.asyncio
async def test_get_similar_artists_skips_invalid(provider_mock: Mock) -> None:
    """get_similar_artists skips artists that fail to parse."""
    yandex_artists = [Mock(), Mock()]
    provider_mock.client.get_similar_artists = AsyncMock(return_value=yandex_artists)

    parsed_ok = _media_item_mock(Artist)
    with patch(
        "music_assistant.providers.yandex_music.provider.parse_artist",
        side_effect=[InvalidDataError("missing id"), parsed_ok],
    ):
        result = await YandexMusicProvider.get_similar_artists(provider_mock, "99")

    assert result == [parsed_ok]


@pytest.mark.asyncio
async def test_get_similar_artists_empty(provider_mock: Mock) -> None:
    """get_similar_artists returns [] when client returns no artists."""
    provider_mock.client.get_similar_artists = AsyncMock(return_value=[])

    result = await YandexMusicProvider.get_similar_artists(provider_mock, "42")

    assert result == []


# -- M18: integration coverage with the real parser path ---------------------
#
# Most tests in this file mock ``_parse_my_wave_track`` / ``parse_playlist``
# directly. That keeps them fast but leaves the orchestrator-parser seam
# untested: a regression where the orchestrator builds malformed inputs for
# the parser, or where the parser returns shape-incompatible output, would
# slip through. The tests below exercise the real parser end-to-end with a
# minimal Yandex track fixture.


def _real_yandex_track() -> Any:
    """Return a minimally-shaped Yandex track for the real ``parse_track`` to consume."""
    fixture = pathlib.Path(__file__).parent / "fixtures" / "tracks" / "with_artist_and_album.json"
    return YandexTrack.de_json(json.loads(fixture.read_text()), DE_JSON_CLIENT)


@pytest.mark.asyncio
async def test_get_my_wave_recommendations_with_real_parser(provider_mock: Mock) -> None:
    """
    End-to-end: real ``_parse_my_wave_track`` against a real Yandex track fixture.

    Mocking ``_parse_my_wave_track`` directly (as the success/duplicate/error
    tests above do) cannot catch a regression where ``parse_track`` returns a
    malformed Track, where the composite ``item_id`` is not stamped onto the
    provider_mappings, or where the wave-state lock is not respected. This
    test binds the real method and asserts on the produced ``Track``.
    """
    yt = _real_yandex_track()
    assert yt is not None

    # Bind the real method so it actually calls parse_track + composite item_id logic.
    real_parse = YandexMusicProvider._parse_my_wave_track.__get__(provider_mock)
    provider_mock._parse_my_wave_track = real_parse
    provider_mock._fetch_rotor_session_batch = AsyncMock(return_value=([yt], "batch_x"))
    # Wave-state lock is held during the loop; supply a fresh state.
    provider_mock._get_wave_state = Mock(return_value=_WaveState())
    # get_item_mapping is consumed by parse_track for provider_mappings; mock the spec
    # method to return a minimal ItemMapping-like stub.
    provider_mock.get_item_mapping = Mock(
        side_effect=lambda mt, key, name: ItemMapping(
            media_type=MediaType(mt) if isinstance(mt, str) else mt,
            item_id=key,
            provider=provider_mock.instance_id,
            name=name,
        )
    )

    result = await YandexMusicProvider._get_my_wave_recommendations(provider_mock)

    assert result is not None
    assert isinstance(result, RecommendationFolder)
    assert len(result.items) == 1
    track = result.items[0]
    assert isinstance(track, Track)
    # The orchestrator-parser contract: composite item_id, real Yandex track id.
    assert track.item_id == f"500{RADIO_TRACK_ID_SEP}{ROTOR_STATION_MY_WAVE}"
    assert track.name == "Track With Album"
    # provider_mappings carry the composite id too — not the bare track id.
    assert all(pm.item_id == track.item_id for pm in track.provider_mappings)


@pytest.mark.asyncio
async def test_rotating_row_tag_subtitle_from_warm_cache(
    provider_mock: Mock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A warm tag-list cache yields the deterministic tag's display label as subtitle."""
    # freeze the clock so the hourly tag bucket cannot flip mid-test
    monkeypatch.setattr(
        "music_assistant.providers.yandex_music.provider.utc",
        lambda: datetime(2026, 7, 24, 12, 30, tzinfo=UTC),
    )
    tags = ["chill", "focus"]
    provider_mock.mass.cache.get_with_freshness = AsyncMock(return_value=(tags, True, True))
    provider_mock._rotating_row_tag = YandexMusicProvider._rotating_row_tag.__get__(
        provider_mock, YandexMusicProvider
    )

    label = await YandexMusicProvider._rotating_row_tag_subtitle(provider_mock, "mood")

    expected = YandexMusicProvider._rotating_row_tag(provider_mock, "mood", tags)
    assert label == expected.title()
    # cache-only read: no backend client involved
    assert provider_mock.client.mock_calls == []


@pytest.mark.asyncio
async def test_rotating_row_tag_subtitle_cold_cache_returns_none(provider_mock: Mock) -> None:
    """A cold tag-list cache yields no subtitle."""
    label = await YandexMusicProvider._rotating_row_tag_subtitle(provider_mock, "mood")

    assert label is None


@pytest.mark.asyncio
async def test_rotating_row_tag_is_deterministic(
    provider_mock: Mock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The tag pick is stable within the hour and always one of the valid tags."""
    # freeze the clock so the hourly tag bucket cannot flip mid-test
    monkeypatch.setattr(
        "music_assistant.providers.yandex_music.provider.utc",
        lambda: datetime(2026, 7, 24, 12, 30, tzinfo=UTC),
    )
    pick = YandexMusicProvider._rotating_row_tag.__get__(provider_mock, YandexMusicProvider)
    tags = ["chill", "focus", "sad", "romantic"]

    first = pick("mood", tags)

    assert first in tags
    assert pick("mood", tags) == first
