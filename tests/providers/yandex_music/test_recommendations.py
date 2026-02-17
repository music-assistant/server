"""Test Yandex Music Recommendations."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, Mock, patch

import pytest
from music_assistant_models.errors import InvalidDataError
from music_assistant_models.media_items import Album, Playlist, RecommendationFolder, Track

from music_assistant.providers.yandex_music.constants import (
    BROWSE_NAMES_EN,
    MY_WAVE_PLAYLIST_ID,
    RADIO_TRACK_ID_SEP,
    ROTOR_STATION_MY_WAVE,
)
from music_assistant.providers.yandex_music.provider import YandexMusicProvider


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
    provider.mass.cache.set = AsyncMock()

    # Mock _get_browse_names to return EN names
    provider._get_browse_names = Mock(return_value=BROWSE_NAMES_EN)

    return provider


@pytest.mark.asyncio
async def test_get_my_wave_recommendations_success(provider_mock: Mock) -> None:
    """Test _get_my_wave_recommendations returns data when API provides tracks."""
    # Create mock track with required attributes
    mock_track = Mock()
    mock_track.id = "12345"
    mock_track.track_id = "12345"

    # Mock get_my_wave_tracks to return tracks
    provider_mock.client.get_my_wave_tracks = AsyncMock(return_value=([mock_track], None))

    # Mock _parse_my_wave_track to return a Track object with composite item_id
    mock_parsed_track = Mock(spec=Track)
    mock_parsed_track.item_id = f"12345{RADIO_TRACK_ID_SEP}{ROTOR_STATION_MY_WAVE}"
    mock_parsed_track.name = "Test Track"
    mock_parsed_track.provider_mappings = []
    provider_mock._parse_my_wave_track = Mock(return_value=mock_parsed_track)

    result = await YandexMusicProvider._get_my_wave_recommendations(provider_mock)

    assert result is not None
    assert isinstance(result, RecommendationFolder)
    assert result.item_id == MY_WAVE_PLAYLIST_ID
    assert result.provider == provider_mock.instance_id
    assert result.name == BROWSE_NAMES_EN[MY_WAVE_PLAYLIST_ID]
    assert result.icon == "mdi-waveform"
    assert len(result.items) > 0


@pytest.mark.asyncio
async def test_get_my_wave_recommendations_empty(provider_mock: Mock) -> None:
    """Test _get_my_wave_recommendations returns None when API returns no tracks."""
    provider_mock.client.get_my_wave_tracks = AsyncMock(return_value=([], None))

    result = await YandexMusicProvider._get_my_wave_recommendations(provider_mock)

    assert result is None


@pytest.mark.asyncio
async def test_get_my_wave_recommendations_duplicate_filtering(provider_mock: Mock) -> None:
    """Test _get_my_wave_recommendations filters duplicate tracks."""
    # Create mock tracks with same ID
    mock_track1 = Mock()
    mock_track1.id = "12345"
    mock_track1.track_id = "12345"

    mock_track2 = Mock()
    mock_track2.id = "12345"  # Same ID
    mock_track2.track_id = "12345"

    # First call returns track1, second call returns track2 (duplicate)
    provider_mock.client.get_my_wave_tracks = AsyncMock(
        side_effect=[
            ([mock_track1], None),
            ([mock_track2], None),
        ]
    )

    mock_parsed_track = Mock(spec=Track)
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
    """Test _get_my_wave_recommendations handles InvalidDataError gracefully."""
    mock_track = Mock()
    mock_track.id = "12345"
    mock_track.track_id = "12345"

    provider_mock.client.get_my_wave_tracks = AsyncMock(return_value=([mock_track], None))

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
    assert result.name == BROWSE_NAMES_EN["feed"]
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
    mock_parsed_track = Mock(spec=Track)
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
    assert result.name == BROWSE_NAMES_EN["chart"]
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
    assert result.name == BROWSE_NAMES_EN["new_releases"]
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
    assert result.name == BROWSE_NAMES_EN["new_playlists"]
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
    assert result.name == BROWSE_NAMES_EN["top_picks"]
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
    mock_datetime = Mock()
    mock_datetime.now.return_value.month = 1

    with (
        patch("music_assistant.providers.yandex_music.provider.datetime", mock_datetime),
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
    mock_datetime = Mock()
    mock_datetime.now.return_value.month = 7

    with (
        patch("music_assistant.providers.yandex_music.provider.datetime", mock_datetime),
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
    """Test _get_seasonal_mix_recommendations falls back to autumn for spring months."""
    mock_playlist = Mock()
    provider_mock.client.get_tag_playlists = AsyncMock(return_value=[mock_playlist])

    mock_parsed_playlist = Mock(spec=Playlist)
    mock_parsed_playlist.item_id = "playlist_1"
    mock_parsed_playlist.name = "Autumn Playlist"

    # Patch datetime to return March (month 3 - spring)
    mock_datetime = Mock()
    mock_datetime.now.return_value.month = 3

    # _validate_tag returns False for spring, triggering fallback to autumn
    provider_mock._validate_tag = AsyncMock(return_value=False)

    with (
        patch("music_assistant.providers.yandex_music.provider.datetime", mock_datetime),
        patch(
            "music_assistant.providers.yandex_music.provider.parse_playlist",
            return_value=mock_parsed_playlist,
        ),
    ):
        result = await YandexMusicProvider._get_seasonal_mix_recommendations(provider_mock)

    assert result is not None
    # Verify it called with autumn tag (spring fallback)
    provider_mock.client.get_tag_playlists.assert_called_once_with("autumn")


@pytest.mark.asyncio
async def test_get_seasonal_mix_recommendations_empty(provider_mock: Mock) -> None:
    """Test _get_seasonal_mix_recommendations returns None when API returns empty."""
    provider_mock.client.get_tag_playlists = AsyncMock(return_value=[])

    mock_datetime = Mock()
    mock_datetime.now.return_value.month = 6

    with patch("music_assistant.providers.yandex_music.provider.datetime", mock_datetime):
        result = await YandexMusicProvider._get_seasonal_mix_recommendations(provider_mock)

    assert result is None


@pytest.mark.asyncio
async def test_get_seasonal_mix_recommendations_invalid_data_error(provider_mock: Mock) -> None:
    """Test _get_seasonal_mix_recommendations handles InvalidDataError gracefully."""
    provider_mock.client.get_tag_playlists = AsyncMock(return_value=[Mock()])

    mock_datetime = Mock()
    mock_datetime.now.return_value.month = 9

    with (
        patch("music_assistant.providers.yandex_music.provider.datetime", mock_datetime),
        patch(
            "music_assistant.providers.yandex_music.provider.parse_playlist",
            side_effect=InvalidDataError("Parse error"),
        ),
    ):
        result = await YandexMusicProvider._get_seasonal_mix_recommendations(provider_mock)

    assert result is None
    provider_mock.logger.debug.assert_called()


@pytest.mark.asyncio
async def test_recommendations_aggregates_all_folders(provider_mock: Mock) -> None:
    """Test recommendations() aggregates all recommendation folders."""
    # Mock all individual recommendation methods to return folders
    mock_folder = Mock(spec=RecommendationFolder)
    mock_folder.item_id = "test_folder"
    mock_folder.name = "Test Folder"

    async def return_folder(*_args: Any, **_kwargs: Any) -> RecommendationFolder:
        return mock_folder

    async def return_tag(_category: str) -> str:
        return "test_tag"

    # Set the methods directly on the provider mock instance
    provider_mock._get_my_wave_recommendations = return_folder
    provider_mock._get_feed_recommendations = return_folder
    provider_mock._get_chart_recommendations = return_folder
    provider_mock._get_new_releases_recommendations = return_folder
    provider_mock._get_new_playlists_recommendations = return_folder
    provider_mock._get_top_picks_recommendations = return_folder
    provider_mock._get_mood_mix_recommendations = return_folder
    provider_mock._get_activity_mix_recommendations = return_folder
    provider_mock._get_seasonal_mix_recommendations = return_folder
    provider_mock._pick_random_tag_for_category = return_tag

    result = await YandexMusicProvider.recommendations(provider_mock)

    assert len(result) == 9  # All 9 methods returned folders


@pytest.mark.asyncio
async def test_recommendations_filters_none_folders(provider_mock: Mock) -> None:
    """Test recommendations() filters out None results from individual methods."""
    mock_folder = Mock(spec=RecommendationFolder)
    mock_folder.item_id = "test_folder"
    mock_folder.name = "Test Folder"

    # Create async functions that return the desired values
    async def return_folder(*_args: Any, **_kwargs: Any) -> RecommendationFolder:
        return mock_folder

    async def return_none(*_args: Any, **_kwargs: Any) -> None:
        return None

    async def return_tag(_category: str) -> str:
        return "test_tag"

    # Set the methods directly on the provider mock instance
    provider_mock._get_my_wave_recommendations = return_folder
    provider_mock._get_feed_recommendations = return_none
    provider_mock._get_chart_recommendations = return_folder
    provider_mock._get_new_releases_recommendations = return_none
    provider_mock._get_new_playlists_recommendations = return_folder
    provider_mock._get_top_picks_recommendations = return_none
    provider_mock._get_mood_mix_recommendations = return_folder
    provider_mock._get_activity_mix_recommendations = return_none
    provider_mock._get_seasonal_mix_recommendations = return_folder
    provider_mock._pick_random_tag_for_category = return_tag

    result = await YandexMusicProvider.recommendations(provider_mock)

    # Should only return 5 folders (4 None were filtered out)
    assert len(result) == 5


@pytest.mark.asyncio
async def test_recommendations_returns_empty_list_when_all_none(provider_mock: Mock) -> None:
    """Test recommendations() returns empty list when all methods return None."""

    async def return_none(*_args: Any, **_kwargs: Any) -> None:
        return None

    async def return_no_tag(_category: str) -> None:
        return None

    # Set the methods directly on the provider mock instance
    provider_mock._get_my_wave_recommendations = return_none
    provider_mock._get_feed_recommendations = return_none
    provider_mock._get_chart_recommendations = return_none
    provider_mock._get_new_releases_recommendations = return_none
    provider_mock._get_new_playlists_recommendations = return_none
    provider_mock._get_top_picks_recommendations = return_none
    provider_mock._get_mood_mix_recommendations = return_none
    provider_mock._get_activity_mix_recommendations = return_none
    provider_mock._get_seasonal_mix_recommendations = return_none
    provider_mock._pick_random_tag_for_category = return_no_tag

    result = await YandexMusicProvider.recommendations(provider_mock)

    assert result == []
