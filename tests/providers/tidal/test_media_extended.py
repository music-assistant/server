"""Additional tests for Tidal Media Manager - Mix operations and similar tracks."""

from unittest.mock import Mock, patch

import pytest
from music_assistant_models.enums import MediaType
from music_assistant_models.errors import MediaNotFoundError

from music_assistant.providers.tidal.jsonapi import JsonApiDocument
from music_assistant.providers.tidal.media import TidalMediaManager


@patch("music_assistant.providers.tidal.media.parse_playlist")
async def test_get_playlist_mix(
    mock_parse_playlist: Mock, media_manager: TidalMediaManager, provider_mock: Mock
) -> None:
    """Test get_playlist with mix ID."""
    provider_mock.api.get.return_value = {
        "title": "My Mix",
        "rows": [
            {"modules": [{"mix": {"images": {"MEDIUM": {"url": "http://example.com/mix.jpg"}}}}]},
        ],
        "lastUpdated": "2023-01-01",
    }
    mock_parse_playlist.return_value = Mock(item_id="mix_123")

    playlist = await media_manager.get_playlist("mix_123")

    assert playlist.item_id == "mix_123"
    provider_mock.api.get.assert_called_with(
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
    provider_mock.api.get.side_effect = [
        MediaNotFoundError("Playlist not found"),
        {
            "title": "My Mix",
            "rows": [{"modules": [{"mix": {"images": {}}}]}],
        },
    ]
    mock_parse_playlist.return_value = Mock(item_id="123")

    playlist = await media_manager.get_playlist("123")

    assert playlist.item_id == "123"
    assert provider_mock.api.get.call_count == 2
    # First call as playlist
    provider_mock.api.get.assert_any_call("playlists/123")
    # Second call as mix
    provider_mock.api.get.assert_any_call(
        "pages/mix",
        params={"mixId": "123", "deviceType": "BROWSER"},
    )


@patch("music_assistant.providers.tidal.media.parse_track_v2")
async def test_get_similar_tracks(
    mock_parse_track: Mock, media_manager: TidalMediaManager, provider_mock: Mock
) -> None:
    """Test get_similar_tracks reads from the official relationship endpoint."""
    doc = JsonApiDocument(
        {
            "data": [{"type": "tracks", "id": str(i)} for i in range(10)],
            "included": [{"type": "tracks", "id": str(i), "attributes": {}} for i in range(10)],
        }
    )
    provider_mock.api.get_jsonapi.return_value = doc
    mock_parse_track.return_value = Mock(item_id="1")

    tracks = await media_manager.get_similar_tracks("123", limit=3)

    assert len(tracks) == 3
    provider_mock.api.get_jsonapi.assert_called_with(
        "tracks/123/relationships/similarTracks",
        include=["similarTracks.artists", "similarTracks.albums.coverArt"],
        replace_media="similarTracks",
    )


@patch("music_assistant.providers.tidal.media.parse_track")
async def test_get_playlist_tracks_mix(
    mock_parse_track: Mock, media_manager: TidalMediaManager, provider_mock: Mock
) -> None:
    """Test get_playlist_tracks with mix ID."""
    provider_mock.api.get.return_value = {
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
    provider_mock.api.get.assert_called_with(
        "pages/mix",
        params={"mixId": "123", "deviceType": "BROWSER"},
    )


async def test_get_mix_details_no_rows(
    media_manager: TidalMediaManager, provider_mock: Mock
) -> None:
    """Test _get_mix_details raises error when no rows."""
    provider_mock.api.get.return_value = {"rows": []}

    with pytest.raises(MediaNotFoundError, match="Mix 123 has no tracks"):
        await media_manager.get_playlist_tracks("mix_123")


@patch("music_assistant.providers.tidal.media.parse_track")
@patch("music_assistant.providers.tidal.media.parse_playlist")
async def test_mix_feed_fetched_once(
    mock_parse_playlist: Mock,
    mock_parse_track: Mock,
    media_manager: TidalMediaManager,
    provider_mock: Mock,
) -> None:
    """Test opening a mix (details then tracks) fetches the shared pages/mix feed once."""
    feed = {
        "title": "My Mix",
        "rows": [
            {"modules": [{"mix": {"images": {}}}]},
            {"modules": [{"pagedList": {"items": [{"id": 1}, {"id": 2}]}}]},
        ],
    }
    # Back the cache with a real dict so the second fetch is a hit.
    store: dict[str, object] = {}

    async def _get(key: str, **_kw: object) -> object:
        return store.get(key)

    async def _set(key: str, data: object, **_kw: object) -> None:
        store[key] = data

    provider_mock.mass.cache.get = _get
    provider_mock.mass.cache.set = _set
    provider_mock.api.get.return_value = feed
    mock_parse_playlist.return_value = Mock(item_id="mix_123")
    mock_parse_track.side_effect = [Mock(item_id="1"), Mock(item_id="2")]

    await media_manager.get_playlist("mix_123")
    await media_manager.get_playlist_tracks("mix_123")

    provider_mock.api.get.assert_called_once()


@patch("music_assistant.providers.tidal.media.parse_track")
@patch("music_assistant.providers.tidal.media.parse_playlist")
async def test_mix_modules_found_regardless_of_row_order(
    mock_parse_playlist: Mock,
    mock_parse_track: Mock,
    media_manager: TidalMediaManager,
    provider_mock: Mock,
) -> None:
    """Test the mix header and track list are located by content, not a fixed row index."""
    # pagedList in row 0, mix header in row 1 (reverse of the usual layout).
    provider_mock.api.get.return_value = {
        "title": "My Mix",
        "rows": [
            {"modules": [{"pagedList": {"items": [{"id": 1}]}}]},
            {"modules": [{"mix": {"images": {"MEDIUM": {"url": "http://img"}}}}]},
        ],
    }
    mock_parse_playlist.return_value = Mock(item_id="mix_123")
    mock_parse_track.side_effect = [Mock(item_id="1")]

    await media_manager.get_playlist("mix_123")
    tracks = await media_manager.get_playlist_tracks("mix_123")

    assert mock_parse_playlist.call_args.args[1]["images"] == {"MEDIUM": {"url": "http://img"}}
    assert [t.item_id for t in tracks] == ["1"]


async def test_search_empty_results(media_manager: TidalMediaManager, provider_mock: Mock) -> None:
    """Test search with empty results."""
    provider_mock.api.get_jsonapi.return_value = JsonApiDocument(
        {"data": {"id": "query", "type": "searchResults", "relationships": {}}}
    )

    results = await media_manager.search("query", [MediaType.ARTIST])

    assert len(results.artists) == 0
    assert len(results.albums) == 0
    assert len(results.tracks) == 0
    assert len(results.playlists) == 0
