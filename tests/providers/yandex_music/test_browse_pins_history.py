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


def _hist_item(track_id: int) -> object:
    """
    Build a history entry the way MarshalX actually returns it.

    `data.item_id` is a dict containing track_id, album_id, etc.; `full_model`
    is not populated by the live API. Callers batch-resolve via get_tracks.
    """
    data = type("D", (), {"item_id": {"track_id": str(track_id)}, "full_model": None})()
    return type("HistItem", (), {"type": "track", "data": data})()


@pytest.mark.asyncio
async def test_browse_history_flattens_and_deduplicates(provider_mock: Mock) -> None:
    """_browse_history flattens days→groups→tracks, de-dupes by track id, preserves order."""
    group1 = type("Group", (), {"tracks": [_hist_item(1), _hist_item(2)]})()
    group2 = type("Group", (), {"tracks": [_hist_item(2), _hist_item(3)]})()  # dup id=2
    tab1 = type("Tab", (), {"items": [group1]})()
    tab2 = type("Tab", (), {"items": [group2]})()
    history = type("MusicHistory", (), {"history_tabs": [tab1, tab2]})()
    provider_mock.client.get_music_history = AsyncMock(return_value=history)

    # Batch-hydrate returns the yandex tracks in their own order; the provider
    # re-orders them to match the de-duplicated id list.
    yt1 = type("Yt", (), {"id": 1})()
    yt2 = type("Yt", (), {"id": 2})()
    yt3 = type("Yt", (), {"id": 3})()
    provider_mock.client.get_tracks = AsyncMock(return_value=[yt3, yt1, yt2])

    parsed = [Mock(spec=Track, name="p1"), Mock(spec=Track, name="p2"), Mock(spec=Track, name="p3")]
    with patch(
        "music_assistant.providers.yandex_music.provider.parse_track",
        side_effect=parsed,
    ):
        result = await YandexMusicProvider._browse_history(provider_mock)

    provider_mock.client.get_tracks.assert_awaited_once_with(["1", "2", "3"])
    assert result == parsed


@pytest.mark.asyncio
async def test_browse_history_skips_non_track_items(provider_mock: Mock) -> None:
    """_browse_history ignores items with type != 'track'."""
    album_item = type(
        "HistItem",
        (),
        {
            "type": "album",
            "data": type("D", (), {"item_id": {"track_id": "99"}})(),
        },
    )()
    group = type("Group", (), {"tracks": [album_item]})()
    tab = type("Tab", (), {"items": [group]})()
    history = type("MusicHistory", (), {"history_tabs": [tab]})()
    provider_mock.client.get_music_history = AsyncMock(return_value=history)
    provider_mock.client.get_tracks = AsyncMock()

    with patch("music_assistant.providers.yandex_music.provider.parse_track") as parse_track:
        result = await YandexMusicProvider._browse_history(provider_mock)
        parse_track.assert_not_called()

    assert result == []
    # No IDs collected → no hydration round-trip at all
    provider_mock.client.get_tracks.assert_not_awaited()


@pytest.mark.asyncio
async def test_browse_history_skips_invalid_track(provider_mock: Mock) -> None:
    """_browse_history drops tracks where parse_track raises InvalidDataError."""
    group = type("Group", (), {"tracks": [_hist_item(1)]})()
    tab = type("Tab", (), {"items": [group]})()
    history = type("MusicHistory", (), {"history_tabs": [tab]})()
    provider_mock.client.get_music_history = AsyncMock(return_value=history)

    yt1 = type("Yt", (), {"id": 1})()
    provider_mock.client.get_tracks = AsyncMock(return_value=[yt1])

    with patch(
        "music_assistant.providers.yandex_music.provider.parse_track",
        side_effect=InvalidDataError("nope"),
    ):
        result = await YandexMusicProvider._browse_history(provider_mock)

    assert result == []
