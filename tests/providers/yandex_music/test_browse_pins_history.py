"""Tests for the Pins and Listening History browse handlers."""

from __future__ import annotations

from unittest.mock import AsyncMock, Mock, patch

import pytest
from music_assistant_models.errors import InvalidDataError, MediaNotFoundError
from music_assistant_models.media_items import Album, Artist, Playlist, Track

from music_assistant.providers.yandex_music.provider import YandexMusicProvider


@pytest.fixture
def provider_mock() -> Mock:
    """Return a mock Yandex Music provider with cache + client stubs."""
    provider = Mock(spec=YandexMusicProvider)
    provider.domain = "yandex_music"
    provider.instance_id = "yandex_music_instance"
    provider.logger = Mock()
    provider.client = AsyncMock()
    provider.client.user_id = 12345
    provider.mass = Mock()
    provider.mass.cache = AsyncMock()
    provider.mass.cache.get = AsyncMock(return_value=None)
    provider.mass.cache.set = AsyncMock()
    return provider


@pytest.mark.asyncio
async def test_browse_pins_returns_empty_when_no_pins(provider_mock: Mock) -> None:
    """_browse_pins returns [] when client returns None."""
    provider_mock.client.get_pins = AsyncMock(return_value=None)

    result = await YandexMusicProvider._browse_pins(provider_mock)

    assert result == []


@pytest.mark.asyncio
async def test_browse_pins_returns_empty_when_pins_field_missing(
    provider_mock: Mock,
) -> None:
    """_browse_pins returns [] when PinsList.pins is None."""
    provider_mock.client.get_pins = AsyncMock(return_value=type("PinsList", (), {"pins": None})())

    result = await YandexMusicProvider._browse_pins(provider_mock)

    assert result == []


@pytest.mark.asyncio
async def test_browse_pins_resolves_artist_album_playlist(provider_mock: Mock) -> None:
    """_browse_pins routes each pin type to the corresponding lookup."""
    artist_pin = type(
        "Pin",
        (),
        {"type": "artist_item", "data": type("D", (), {"id": 11})()},
    )()
    album_pin = type(
        "Pin",
        (),
        {"type": "album_item", "data": type("D", (), {"id": 22})()},
    )()
    playlist_pin = type(
        "Pin",
        (),
        {"type": "playlist_item", "data": type("D", (), {"uid": 33, "kind": 44})()},
    )()
    pins = type("PinsList", (), {"pins": [artist_pin, album_pin, playlist_pin]})()
    provider_mock.client.get_pins = AsyncMock(return_value=pins)

    artist = Mock(spec=Artist)
    album = Mock(spec=Album)
    playlist = Mock(spec=Playlist)
    provider_mock.get_artist = AsyncMock(return_value=artist)
    provider_mock.get_album = AsyncMock(return_value=album)
    provider_mock.get_playlist = AsyncMock(return_value=playlist)

    result = await YandexMusicProvider._browse_pins(provider_mock)

    provider_mock.get_artist.assert_awaited_once_with("11")
    provider_mock.get_album.assert_awaited_once_with("22")
    provider_mock.get_playlist.assert_awaited_once_with("33:44")
    assert result == [artist, album, playlist]


@pytest.mark.asyncio
async def test_browse_pins_skips_wave_and_missing_data(provider_mock: Mock) -> None:
    """_browse_pins ignores wave pins and pins with missing data."""
    wave_pin = type(
        "Pin",
        (),
        {"type": "wave_item", "data": type("D", (), {})()},
    )()
    bad_pin = type("Pin", (), {"type": "album_item", "data": None})()
    pins = type("PinsList", (), {"pins": [wave_pin, bad_pin]})()
    provider_mock.client.get_pins = AsyncMock(return_value=pins)
    provider_mock.get_album = AsyncMock()

    result = await YandexMusicProvider._browse_pins(provider_mock)

    assert result == []
    provider_mock.get_album.assert_not_called()


@pytest.mark.asyncio
async def test_browse_pins_skips_lookup_errors(provider_mock: Mock) -> None:
    """_browse_pins survives MediaNotFoundError during single-item lookups."""
    album_pin = type(
        "Pin",
        (),
        {"type": "album_item", "data": type("D", (), {"id": 22})()},
    )()
    pins = type("PinsList", (), {"pins": [album_pin]})()
    provider_mock.client.get_pins = AsyncMock(return_value=pins)
    provider_mock.get_album = AsyncMock(side_effect=MediaNotFoundError("gone"))

    result = await YandexMusicProvider._browse_pins(provider_mock)

    assert result == []


@pytest.mark.asyncio
async def test_browse_history_returns_empty_when_no_history(
    provider_mock: Mock,
) -> None:
    """_browse_history returns [] when client returns None."""
    provider_mock.client.get_music_history = AsyncMock(return_value=None)

    result = await YandexMusicProvider._browse_history(provider_mock)

    assert result == []


@pytest.mark.asyncio
async def test_browse_history_flattens_and_deduplicates(provider_mock: Mock) -> None:
    """_browse_history flattens days→groups→tracks and de-dupes by track id."""

    def make_track(track_id: int) -> object:
        full = type("Track", (), {"id": track_id, "title": f"T{track_id}"})()
        data = type("D", (), {"full_model": full})()
        return type("HistItem", (), {"type": "track", "data": data})()

    group1 = type("Group", (), {"tracks": [make_track(1), make_track(2)]})()
    group2 = type("Group", (), {"tracks": [make_track(2), make_track(3)]})()  # dup id=2
    tab1 = type("Tab", (), {"items": [group1]})()
    tab2 = type("Tab", (), {"items": [group2]})()
    history = type("MusicHistory", (), {"history_tabs": [tab1, tab2]})()
    provider_mock.client.get_music_history = AsyncMock(return_value=history)

    parsed = [Mock(spec=Track), Mock(spec=Track), Mock(spec=Track)]
    with patch(
        "music_assistant.providers.yandex_music.provider.parse_track",
        side_effect=parsed,
    ):
        result = await YandexMusicProvider._browse_history(provider_mock)

    assert result == parsed  # 3 unique tracks across two days


@pytest.mark.asyncio
async def test_browse_history_skips_non_track_items(provider_mock: Mock) -> None:
    """_browse_history ignores items with type != 'track'."""
    album_item = type(
        "HistItem",
        (),
        {
            "type": "album",
            "data": type("D", (), {"full_model": type("X", (), {"id": 99})()})(),
        },
    )()
    group = type("Group", (), {"tracks": [album_item]})()
    tab = type("Tab", (), {"items": [group]})()
    history = type("MusicHistory", (), {"history_tabs": [tab]})()
    provider_mock.client.get_music_history = AsyncMock(return_value=history)

    with patch("music_assistant.providers.yandex_music.provider.parse_track") as parse_track:
        result = await YandexMusicProvider._browse_history(provider_mock)
        parse_track.assert_not_called()

    assert result == []


@pytest.mark.asyncio
async def test_browse_history_skips_invalid_track(provider_mock: Mock) -> None:
    """_browse_history drops tracks where parse_track raises InvalidDataError."""
    full = type("Track", (), {"id": 1, "title": "T1"})()
    data = type("D", (), {"full_model": full})()
    item = type("HistItem", (), {"type": "track", "data": data})()
    group = type("Group", (), {"tracks": [item]})()
    tab = type("Tab", (), {"items": [group]})()
    history = type("MusicHistory", (), {"history_tabs": [tab]})()
    provider_mock.client.get_music_history = AsyncMock(return_value=history)

    with patch(
        "music_assistant.providers.yandex_music.provider.parse_track",
        side_effect=InvalidDataError("nope"),
    ):
        result = await YandexMusicProvider._browse_history(provider_mock)

    assert result == []
