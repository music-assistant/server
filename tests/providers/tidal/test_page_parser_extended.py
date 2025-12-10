"""Extended tests for Tidal Page Parser."""

from typing import Any
from unittest.mock import AsyncMock, Mock, patch

import pytest
from music_assistant_models.enums import MediaType
from music_assistant_models.media_items import ItemMapping

from music_assistant.providers.tidal.tidal_page_parser import TidalPageParser


@pytest.fixture
def provider_mock() -> Mock:
    """Return a mock provider."""
    provider = Mock()
    provider.domain = "tidal"
    provider.instance_id = "tidal_instance"
    provider.auth.user_id = "12345"
    provider.logger = Mock()
    provider.mass = Mock()
    provider.mass.cache.get = AsyncMock(return_value=None)
    provider.mass.cache.set = AsyncMock()

    def get_item_mapping(media_type: MediaType, key: str, name: str) -> ItemMapping:
        return ItemMapping(
            media_type=media_type,
            item_id=key,
            provider=provider.instance_id,
            name=name,
        )

    provider.get_item_mapping.side_effect = get_item_mapping
    return provider


def test_parser_initialization(provider_mock: Mock) -> None:
    """Test parser initialization."""
    parser = TidalPageParser(provider_mock)

    assert parser.provider == provider_mock
    assert parser.logger == provider_mock.logger
    assert "MIX" in parser._content_map
    assert "PLAYLIST" in parser._content_map
    assert "ALBUM" in parser._content_map
    assert "TRACK" in parser._content_map
    assert "ARTIST" in parser._content_map
    assert len(parser._module_map) == 0
    assert parser._page_path is None
    assert parser._parsed_at == 0


@patch("music_assistant.providers.tidal.tidal_page_parser.parse_track")
def test_process_track_list(mock_parse_track: Mock, provider_mock: Mock) -> None:
    """Test processing TRACK_LIST module."""
    mock_track = Mock()
    mock_track.name = "Test Track"
    mock_parse_track.return_value = mock_track

    page_data = {
        "rows": [
            {
                "modules": [
                    {
                        "title": "Top Tracks",
                        "type": "TRACK_LIST",
                        "pagedList": {
                            "items": [
                                {"id": 1, "title": "Track 1"},
                                {"id": 2, "title": "Track 2"},
                            ]
                        },
                    }
                ]
            }
        ]
    }

    parser = TidalPageParser(provider_mock)
    parser.parse_page_structure(page_data, "pages/test")

    module_info = parser._module_map[0]
    items, content_type = parser.get_module_items(module_info)

    assert content_type == MediaType.TRACK
    assert len(items) == 2
    assert mock_parse_track.call_count == 2


@patch("music_assistant.providers.tidal.tidal_page_parser.parse_artist")
def test_process_artist_list(mock_parse_artist: Mock, provider_mock: Mock) -> None:
    """Test processing ARTIST_LIST module."""
    mock_artist = Mock()
    mock_artist.name = "Test Artist"
    mock_parse_artist.return_value = mock_artist

    page_data = {
        "rows": [
            {
                "modules": [
                    {
                        "title": "Popular Artists",
                        "type": "ARTIST_LIST",
                        "pagedList": {
                            "items": [
                                {"id": 1, "name": "Artist 1"},
                                {"id": 2, "name": "Artist 2"},
                                {"id": 3, "name": "Artist 3"},
                            ]
                        },
                    }
                ]
            }
        ]
    }

    parser = TidalPageParser(provider_mock)
    parser.parse_page_structure(page_data, "pages/test")

    module_info = parser._module_map[0]
    items, content_type = parser.get_module_items(module_info)

    assert content_type == MediaType.ARTIST
    assert len(items) == 3
    assert mock_parse_artist.call_count == 3


@patch("music_assistant.providers.tidal.tidal_page_parser.parse_playlist")
def test_process_mix_list(mock_parse_playlist: Mock, provider_mock: Mock) -> None:
    """Test processing MIX_LIST module."""
    mock_mix = Mock()
    mock_mix.name = "Daily Mix"
    mock_parse_playlist.return_value = mock_mix

    page_data = {
        "rows": [
            {
                "modules": [
                    {
                        "title": "Your Mixes",
                        "type": "MIX_LIST",
                        "pagedList": {
                            "items": [
                                {"id": "mix1", "title": "Mix 1"},
                            ]
                        },
                    }
                ]
            }
        ]
    }

    parser = TidalPageParser(provider_mock)
    parser.parse_page_structure(page_data, "pages/test")

    module_info = parser._module_map[0]
    items, content_type = parser.get_module_items(module_info)

    assert content_type == MediaType.PLAYLIST
    assert len(items) == 1
    mock_parse_playlist.assert_called_with(
        provider_mock, {"id": "mix1", "title": "Mix 1"}, is_mix=True
    )


@patch("music_assistant.providers.tidal.tidal_page_parser.parse_track")
def test_process_track_list_with_error(mock_parse_track: Mock, provider_mock: Mock) -> None:
    """Test TRACK_LIST with parsing error."""
    mock_parse_track.side_effect = [
        Mock(name="Track 1"),
        KeyError("Missing field"),
        Mock(name="Track 3"),
    ]

    page_data = {
        "rows": [
            {
                "modules": [
                    {
                        "title": "Tracks",
                        "type": "TRACK_LIST",
                        "pagedList": {
                            "items": [
                                {"id": 1},
                                {"id": 2},
                                {"id": 3},
                            ]
                        },
                    }
                ]
            }
        ]
    }

    parser = TidalPageParser(provider_mock)
    parser.parse_page_structure(page_data, "pages/test")

    module_info = parser._module_map[0]
    items, _ = parser.get_module_items(module_info)

    # Should have 2 items (one failed)
    assert len(items) == 2
    provider_mock.logger.warning.assert_called()


def test_process_track_list_with_non_dict_items(provider_mock: Mock) -> None:
    """Test TRACK_LIST with non-dict items (should be skipped)."""
    page_data = {
        "rows": [
            {
                "modules": [
                    {
                        "title": "Tracks",
                        "type": "TRACK_LIST",
                        "pagedList": {
                            "items": [
                                "not a dict",
                                12345,
                                None,
                            ]
                        },
                    }
                ]
            }
        ]
    }

    parser = TidalPageParser(provider_mock)
    parser.parse_page_structure(page_data, "pages/test")

    module_info = parser._module_map[0]
    items, _ = parser.get_module_items(module_info)

    # All items should be skipped
    assert len(items) == 0


async def test_from_cache_success(provider_mock: Mock) -> None:
    """Test loading parser from cache."""
    cached_data = {
        "module_map": [{"title": "Test Module"}],
        "content_map": {"PLAYLIST": {}},
        "parsed_at": 1234567890,
    }
    provider_mock.mass.cache.get.return_value = cached_data

    parser = await TidalPageParser.from_cache(provider_mock, "pages/home")

    assert parser is not None
    assert len(parser._module_map) == 1
    assert parser._parsed_at == 1234567890
    provider_mock.mass.cache.get.assert_called_with(
        "pages/home",
        provider=provider_mock.instance_id,
        category=1,  # CACHE_CATEGORY_RECOMMENDATIONS
    )


async def test_from_cache_miss(provider_mock: Mock) -> None:
    """Test cache miss returns None."""
    provider_mock.mass.cache.get.return_value = None

    parser = await TidalPageParser.from_cache(provider_mock, "pages/home")

    assert parser is None


async def test_from_cache_invalid_data(provider_mock: Mock) -> None:
    """Test cache with invalid data returns None."""
    # from_cache expects dict, won't handle invalid data gracefully
    # The method will fail on .get() calls if data is invalid
    provider_mock.mass.cache.get.return_value = {}  # Empty dict is valid but has no data

    parser = await TidalPageParser.from_cache(provider_mock, "pages/home")

    # Parser should be None because empty dict evaluates to False
    assert parser is None


@patch("music_assistant.providers.tidal.tidal_page_parser.parse_playlist")
def test_playlist_list_with_mix_detection(mock_parse_playlist: Mock, provider_mock: Mock) -> None:
    """Test PLAYLIST_LIST detects mixes."""
    mock_playlist = Mock()
    mock_parse_playlist.return_value = mock_playlist

    page_data = {
        "rows": [
            {
                "modules": [
                    {
                        "title": "Playlists",
                        "type": "PLAYLIST_LIST",
                        "pagedList": {
                            "items": [
                                {"uuid": "1", "title": "Regular Playlist"},
                                {"mixId": "mix_123", "title": "Mix", "mixType": "DISCOVERY"},
                            ]
                        },
                    }
                ]
            }
        ]
    }

    parser = TidalPageParser(provider_mock)
    parser.parse_page_structure(page_data, "pages/test")

    module_info = parser._module_map[0]
    items, _ = parser.get_module_items(module_info)

    assert len(items) == 2
    # First call should be is_mix=False, second should be is_mix=True
    assert mock_parse_playlist.call_args_list[0][1]["is_mix"] is False
    assert mock_parse_playlist.call_args_list[1][1]["is_mix"] is True


def test_empty_page_data(provider_mock: Mock) -> None:
    """Test parsing empty page data."""
    parser = TidalPageParser(provider_mock)
    parser.parse_page_structure({}, "pages/empty")

    assert len(parser._module_map) == 0
    assert parser._page_path == "pages/empty"


def test_page_with_no_modules(provider_mock: Mock) -> None:
    """Test page with rows but no modules."""
    page_data: dict[str, Any] = {
        "rows": [
            {},
            {"modules": []},
        ]
    }

    parser = TidalPageParser(provider_mock)
    parser.parse_page_structure(page_data, "pages/test")

    assert len(parser._module_map) == 0


def test_multiple_module_types_in_one_page(provider_mock: Mock) -> None:
    """Test page with multiple different module types."""
    with (
        patch("music_assistant.providers.tidal.tidal_page_parser.parse_playlist") as mock_pl,
        patch("music_assistant.providers.tidal.tidal_page_parser.parse_album") as mock_al,
        patch("music_assistant.providers.tidal.tidal_page_parser.parse_track") as mock_tr,
    ):
        mock_pl.return_value = Mock(name="Playlist")
        mock_al.return_value = Mock(name="Album")
        mock_tr.return_value = Mock(name="Track")

        page_data = {
            "rows": [
                {
                    "modules": [
                        {
                            "title": "Playlists",
                            "type": "PLAYLIST_LIST",
                            "pagedList": {"items": [{"uuid": "1"}]},
                        },
                        {
                            "title": "Albums",
                            "type": "ALBUM_LIST",
                            "pagedList": {"items": [{"id": 1}]},
                        },
                    ]
                },
                {
                    "modules": [
                        {
                            "title": "Tracks",
                            "type": "TRACK_LIST",
                            "pagedList": {"items": [{"id": 1}]},
                        }
                    ]
                },
            ]
        }

        parser = TidalPageParser(provider_mock)
        parser.parse_page_structure(page_data, "pages/test")

        assert len(parser._module_map) == 3

        # Verify each module
        _, type1 = parser.get_module_items(parser._module_map[0])
        assert type1 == MediaType.PLAYLIST

        _, type2 = parser.get_module_items(parser._module_map[1])
        assert type2 == MediaType.ALBUM

        _, type3 = parser.get_module_items(parser._module_map[2])
        assert type3 == MediaType.TRACK


def test_module_info_structure(provider_mock: Mock) -> None:
    """Test module_info contains correct metadata."""
    page_data = {
        "rows": [
            {
                "modules": [
                    {
                        "title": "Test Module",
                        "type": "PLAYLIST_LIST",
                        "pagedList": {"items": []},
                    }
                ]
            }
        ]
    }

    parser = TidalPageParser(provider_mock)
    parser.parse_page_structure(page_data, "pages/test")

    module_info = parser._module_map[0]
    assert module_info["title"] == "Test Module"
    assert module_info["type"] == "PLAYLIST_LIST"
    assert module_info["module_idx"] == 0
    assert module_info["row_idx"] == 0
    assert "raw_data" in module_info


@patch("music_assistant.providers.tidal.tidal_page_parser.parse_playlist")
def test_process_highlight_module(mock_parse_playlist: Mock, provider_mock: Mock) -> None:
    """Test processing HIGHLIGHT_MODULE."""
    mock_playlist = Mock()
    mock_playlist.name = "Highlight Playlist"
    mock_parse_playlist.return_value = mock_playlist

    page_data = {
        "rows": [
            {
                "modules": [
                    {
                        "title": "Highlights",
                        "type": "HIGHLIGHT_MODULE",
                        "highlight": [
                            {"type": "PLAYLIST", "item": {"uuid": "1", "title": "Highlight 1"}}
                        ],
                    }
                ]
            }
        ]
    }

    parser = TidalPageParser(provider_mock)
    parser.parse_page_structure(page_data, "pages/test")

    module_info = parser._module_map[0]
    items, content_type = parser.get_module_items(module_info)

    assert content_type == MediaType.PLAYLIST
    assert len(items) == 1
    mock_parse_playlist.assert_called_once()


def test_process_generic_items(provider_mock: Mock) -> None:
    """Test processing generic items with type inference."""
    with (
        patch("music_assistant.providers.tidal.tidal_page_parser.parse_track") as mock_track,
        patch("music_assistant.providers.tidal.tidal_page_parser.parse_album") as mock_album,
    ):
        mock_track.return_value = Mock(media_type=MediaType.TRACK)
        mock_album.return_value = Mock(media_type=MediaType.ALBUM)

        page_data = {
            "rows": [
                {
                    "modules": [
                        {
                            "title": "Generic",
                            "type": "UNKNOWN_LIST",
                            "pagedList": {
                                "items": [
                                    {
                                        "id": 1,
                                        "title": "Track",
                                        "duration": 100,
                                        "album": {},
                                    },  # Inferred TRACK
                                    {
                                        "id": 2,
                                        "title": "Album",
                                        "numberOfTracks": 10,
                                        "artists": [],
                                    },  # Inferred ALBUM
                                ]
                            },
                        }
                    ]
                }
            ]
        }

        parser = TidalPageParser(provider_mock)
        parser.parse_page_structure(page_data, "pages/test")

        module_info = parser._module_map[0]
        items, _ = parser.get_module_items(module_info)

        assert len(items) == 2
        mock_track.assert_called_once()
        mock_album.assert_called_once()


def test_content_stats(provider_mock: Mock) -> None:
    """Test content_stats property."""
    parser = TidalPageParser(provider_mock)
    parser._module_map = [{"title": "Test"}]
    parser._parsed_at = 1234567890
    parser._content_map["PLAYLIST"] = {"1": {}}

    stats = parser.content_stats

    assert stats["modules"] == 1
    assert stats["playlist_count"] == 1
    assert stats["album_count"] == 0
    assert "cache_age_minutes" in stats
