"""Additional tests for Tidal Media Manager - Mix operations and similar tracks."""

from unittest.mock import AsyncMock, Mock, patch

import pytest
from music_assistant_models.enums import MediaType
from music_assistant_models.errors import MediaNotFoundError
from music_assistant_models.media_items import ItemMapping

from music_assistant.providers.tidal.media import TidalMediaManager


@pytest.fixture
def provider_mock() -> Mock:
    """Return a mock provider."""
    provider = Mock()
    provider.domain = "tidal"
    provider.instance_id = "tidal_instance"
    provider.auth.user_id = "12345"
    provider.auth.country_code = "US"
    provider.api = AsyncMock()
    provider.api.get_data.return_value = {}
    provider.logger = Mock()

    def get_item_mapping(media_type: MediaType, key: str, name: str) -> ItemMapping:
        return ItemMapping(
            media_type=media_type,
            item_id=key,
            provider=provider.instance_id,
            name=name,
        )

    provider.get_item_mapping.side_effect = get_item_mapping

    return provider


@pytest.fixture
def media_manager(provider_mock: Mock) -> TidalMediaManager:
    """Return a TidalMediaManager instance."""
    return TidalMediaManager(provider_mock)


@patch("music_assistant.providers.tidal.media.parse_playlist")
async def test_get_playlist_mix(
    mock_parse_playlist: Mock, media_manager: TidalMediaManager, provider_mock: Mock
) -> None:
    """Test get_playlist with mix ID."""
    provider_mock.api.get_data.return_value = {
        "title": "My Mix",
        "rows": [
            {"modules": [{"mix": {"images": {"MEDIUM": {"url": "http://example.com/mix.jpg"}}}}]},
        ],
        "lastUpdated": "2023-01-01",
    }
    mock_parse_playlist.return_value = Mock(item_id="mix_123")

    playlist = await media_manager.get_playlist("mix_123")

    assert playlist.item_id == "mix_123"
    provider_mock.api.get_data.assert_called_with(
        "pages/mix",
        params={"mixId": "123", "deviceType": "BROWSER"},
    )
    mock_parse_playlist.assert_called_once()
    # Verify is_mix=True was passed
    assert mock_parse_playlist.call_args[1]["is_mix"] is True


@patch("music_assistant.providers.tidal.media.parse_playlist")
async def test_get_playlist_fallback_to_mix(
    mock_parse_playlist: Mock, media_manager: TidalMediaManager, provider_mock: Mock
) -> None:
    """Test get_playlist falls back to mix lookup on MediaNotFoundError."""
    # First call raises error, second succeeds
    provider_mock.api.get_data.side_effect = [
        MediaNotFoundError("Playlist not found"),
        {
            "title": "My Mix",
            "rows": [{"modules": [{"mix": {"images": {}}}]}],
        },
    ]
    mock_parse_playlist.return_value = Mock(item_id="123")

    playlist = await media_manager.get_playlist("123")

    assert playlist.item_id == "123"
    assert provider_mock.api.get_data.call_count == 2
    # First call as playlist
    provider_mock.api.get_data.assert_any_call("playlists/123")
    # Second call as mix
    provider_mock.api.get_data.assert_any_call(
        "pages/mix",
        params={"mixId": "123", "deviceType": "BROWSER"},
    )


@patch("music_assistant.providers.tidal.media.parse_track")
async def test_get_similar_tracks(
    mock_parse_track: Mock, media_manager: TidalMediaManager, provider_mock: Mock
) -> None:
    """Test get_similar_tracks."""
    provider_mock.api.get_data.return_value = {"items": [{"id": 1}, {"id": 2}, {"id": 3}]}
    mock_parse_track.return_value = Mock(item_id="1")

    tracks = await media_manager.get_similar_tracks("123", limit=25)

    assert len(tracks) == 3
    provider_mock.api.get_data.assert_called_with(
        "tracks/123/radio",
        params={"limit": 25},
    )


@patch("music_assistant.providers.tidal.media.parse_track")
async def test_get_playlist_tracks_mix(
    mock_parse_track: Mock, media_manager: TidalMediaManager, provider_mock: Mock
) -> None:
    """Test get_playlist_tracks with mix ID."""
    provider_mock.api.get_data.return_value = {
        "rows": [
            {},  # First row is mix info
            {  # Second row has tracks
                "modules": [{"pagedList": {"items": [{"id": 1}, {"id": 2}]}}]
            },
        ]
    }

    # Mock track with position attribute
    def create_track(item_id: int, position: int) -> Mock:
        track = Mock(item_id=str(item_id))
        track.position = position
        return track

    mock_parse_track.side_effect = [
        create_track(1, 1),
        create_track(2, 2),
    ]

    tracks = await media_manager.get_playlist_tracks("mix_123")

    assert len(tracks) == 2
    assert tracks[0].position == 1
    assert tracks[1].position == 2
    provider_mock.api.get_data.assert_called_with(
        "pages/mix",
        params={"mixId": "123", "deviceType": "BROWSER"},
    )


async def test_get_mix_details_no_rows(
    media_manager: TidalMediaManager, provider_mock: Mock
) -> None:
    """Test _get_mix_details raises error when no rows."""
    provider_mock.api.get_data.return_value = {"rows": []}

    with pytest.raises(MediaNotFoundError, match="Mix 123 has no tracks"):
        await media_manager.get_playlist_tracks("mix_123")


async def test_search_empty_results(media_manager: TidalMediaManager, provider_mock: Mock) -> None:
    """Test search with empty results."""
    provider_mock.api.get_data.return_value = {}

    results = await media_manager.search("query", [MediaType.ARTIST])

    assert len(results.artists) == 0
    assert len(results.albums) == 0
    assert len(results.tracks) == 0
    assert len(results.playlists) == 0
