"""Unit tests for YandexMusicClient (api_client.py)."""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
from unittest import mock

import pytest
from music_assistant_models.errors import ResourceTemporarilyUnavailable
from yandex_music.exceptions import NetworkError
from yandex_music.rotor.dashboard import Dashboard
from yandex_music.rotor.station_result import StationResult
from yandex_music.utils.sign_request import DEFAULT_SIGN_KEY

from music_assistant.providers.yandex_music.api_client import (
    GET_FILE_INFO_CODECS,
    YandexMusicClient,
)


def _make_client() -> tuple[YandexMusicClient, mock.AsyncMock]:
    """Create a YandexMusicClient with a mocked underlying ClientAsync.

    Also mocks connect() so that _reconnect() restores the mock client
    instead of trying to create a real connection.

    :return: Tuple of (YandexMusicClient, mock_underlying_client).
    """
    client = YandexMusicClient(token="fake_token")
    mock_underlying = mock.AsyncMock()
    client._client = mock_underlying
    client._user_id = 12345

    async def _fake_connect() -> bool:
        client._client = mock_underlying
        client._user_id = 12345
        return True

    client.connect = _fake_connect  # type: ignore[method-assign]
    return client, mock_underlying


# -- get_liked_albums: batching -------------------------------------------------


async def test_get_liked_albums_batching() -> None:
    """Albums are fetched in batch via client.albums() for full metadata."""
    client, underlying = _make_client()

    # Build 3 minimal "like" objects with album stubs (no cover_uri)
    likes = []
    for album_id in (1, 2, 3):
        album_stub = type("Album", (), {"id": album_id, "cover_uri": None})()
        like = type("Like", (), {"album": album_stub})()
        likes.append(like)

    # Full album objects returned by client.albums()
    full_albums = [
        type("Album", (), {"id": aid, "cover_uri": f"cover_{aid}"})() for aid in (1, 2, 3)
    ]

    underlying.users_likes_albums = mock.AsyncMock(return_value=likes)
    underlying.albums = mock.AsyncMock(return_value=full_albums)

    result = await client.get_liked_albums()

    underlying.albums.assert_awaited_once_with(["1", "2", "3"])
    assert result == full_albums
    assert all(a.cover_uri is not None for a in result)


async def test_get_liked_albums_batch_fallback_on_network_error() -> None:
    """When client.albums() fails, fallback returns minimal album data from likes."""
    client, underlying = _make_client()

    album_stub_1 = type("Album", (), {"id": 10, "cover_uri": None})()
    album_stub_2 = type("Album", (), {"id": 20, "cover_uri": None})()
    likes = [
        type("Like", (), {"album": album_stub_1})(),
        type("Like", (), {"album": album_stub_2})(),
    ]

    underlying.users_likes_albums = mock.AsyncMock(return_value=likes)
    underlying.albums = mock.AsyncMock(side_effect=NetworkError("timeout"))

    result = await client.get_liked_albums()

    # Should fall back to the minimal album objects from likes
    assert len(result) == 2
    assert {a.id for a in result} == {10, 20}


# -- get_tracks: retry on NetworkError -------------------------------------------


async def test_get_tracks_retry_on_network_error_then_success() -> None:
    """First call fails with NetworkError; retry succeeds."""
    client, underlying = _make_client()

    track = type("Track", (), {"id": 400, "title": "Test Track"})()
    underlying.tracks = mock.AsyncMock(side_effect=[NetworkError("timeout"), [track]])

    result = await client.get_tracks(["400"])

    assert result == [track]
    assert underlying.tracks.await_count == 2


async def test_get_tracks_retry_on_network_error_both_fail() -> None:
    """Both attempts fail with NetworkError → ResourceTemporarilyUnavailable."""
    client, underlying = _make_client()

    underlying.tracks = mock.AsyncMock(
        side_effect=[NetworkError("timeout"), NetworkError("timeout again")]
    )

    with pytest.raises(ResourceTemporarilyUnavailable):
        await client.get_tracks(["400"])

    assert underlying.tracks.await_count == 2


# -- get_my_wave_tracks --------------------------------------------------------


async def test_get_my_wave_tracks_returns_tracks_and_batch_id() -> None:
    """get_my_wave_tracks calls rotor_station_tracks and returns ordered tracks and batch_id."""
    client, underlying = _make_client()

    seq_track = type("TrackShort", (), {"id": 100, "track_id": 100})()
    sequence_item = type("SequenceItem", (), {"track": seq_track})()
    result_obj = type(
        "StationTracksResult",
        (),
        {"sequence": [sequence_item], "batch_id": "batch_abc"},
    )()
    underlying.rotor_station_tracks = mock.AsyncMock(return_value=result_obj)

    full_track = type("Track", (), {"id": 100, "title": "My Wave Track"})()
    underlying.tracks = mock.AsyncMock(return_value=[full_track])

    tracks, batch_id = await client.get_my_wave_tracks()

    underlying.rotor_station_tracks.assert_awaited_once()
    assert batch_id == "batch_abc"
    assert len(tracks) == 1
    assert tracks[0].id == 100


async def test_get_my_wave_tracks_empty_sequence_returns_empty() -> None:
    """When rotor returns no sequence, get_my_wave_tracks returns ([], batch_id or None)."""
    client, underlying = _make_client()

    result_obj = type("StationTracksResult", (), {"sequence": [], "batch_id": None})()
    underlying.rotor_station_tracks = mock.AsyncMock(return_value=result_obj)

    tracks, batch_id = await client.get_my_wave_tracks()

    assert tracks == []
    assert batch_id is None
    underlying.tracks.assert_not_awaited()


async def test_send_rotor_station_feedback_posts() -> None:
    """send_rotor_station_feedback POSTs to rotor feedback endpoint."""
    client, underlying = _make_client()

    underlying._request = mock.AsyncMock()
    underlying.base_url = "https://api.music.yandex.net"

    result = await client.send_rotor_station_feedback(
        "user:onyourwave",
        "trackStarted",
        track_id="12345",
        batch_id="batch_xyz",
    )

    assert result is True
    underlying._request.post.assert_awaited_once()
    call_args = underlying._request.post.await_args
    assert "rotor/station/user:onyourwave/feedback" in call_args[0][0]
    body = call_args[0][1]
    assert body["type"] == "trackStarted"
    assert body["trackId"] == "12345"
    assert body["batchId"] == "batch_xyz"


# -- LRC regex tests ---------------------------------------------------------


def test_lrc_regex_matches_valid_synced_lyrics() -> None:
    """LRC regex matches valid synced lyrics with proper format [mm:ss.xx].

    Uses re.search (no ^ anchor) matching the implementation in api_client.py,
    which intentionally allows timestamps anywhere in the text so that LRC
    metadata lines like [ar:Artist] before the first timestamp don't prevent
    detection.
    """
    pattern = r"\[\d{2}:\d{2}(?:\.\d{2,3})?\]"

    # Valid LRC formats that should match
    valid_cases = [
        "[00:12]",  # Basic format (no fractional part)
        "[00:12.34]",  # With centiseconds (2-digit fractional part — lower bound of \d{2,3})
        "[00:12.345]",  # With milliseconds (3-digit fractional part — upper bound of \d{2,3})
        "[12:34]",  # Another basic format
        "[99:59.99]",  # Edge case
        "Some [00:12] text",  # Timestamp embedded in text — re.search finds it
    ]

    for case in valid_cases:
        assert re.search(pattern, case), f"Should match: {case}"


def test_lrc_regex_rejects_invalid_formats() -> None:
    """LRC regex rejects invalid formats (no closing bracket, wrong format)."""
    pattern = r"\[\d{2}:\d{2}(?:\.\d{2,3})?\]"

    # Invalid formats that should NOT match
    invalid_cases = [
        "[00:12",  # Missing closing bracket
        "00:12]",  # Missing opening bracket
        "[0:12]",  # Single digit minute
        "[00:1]",  # Single digit second
        "[00:12.1]",  # Single digit centiseconds (should be 2-3 digits)
        "[00:12.1234]",  # Four digit milliseconds
    ]

    for case in invalid_cases:
        assert not re.search(pattern, case), f"Should NOT match: {case}"


# -- HMAC sign construction tests --------------------------------------------


def test_hmac_sign_construction_explicit() -> None:
    """HMAC sign is constructed explicitly with commas stripped from codecs."""
    # Simulate the parameters
    timestamp = 1234567890
    track_id = "12345"

    # The correct way (explicit construction)
    codecs_for_sign = GET_FILE_INFO_CODECS.replace(",", "")
    param_string = f"{timestamp}{track_id}lossless{codecs_for_sign}encraw"

    # Verify codecs_for_sign has no commas
    assert "," not in codecs_for_sign

    # Verify the construction is correct
    expected = f"1234567890{track_id}lossless{codecs_for_sign}encraw"
    assert param_string == expected

    # Verify HMAC can be constructed
    hmac_sign = hmac.new(
        DEFAULT_SIGN_KEY.encode(),
        param_string.encode(),
        hashlib.sha256,
    )
    sign = base64.b64encode(hmac_sign.digest()).decode()[:-1]

    # Verify sign is 43 characters (SHA-256 base64 with one "=" removed)
    assert len(sign) == 43
    assert not sign.endswith("=")


# -- get_dashboard_stations --------------------------------------------------


async def test_get_dashboard_stations_returns_personalized_stations() -> None:
    """get_dashboard_stations() returns stations from rotor/stations/dashboard."""
    client, underlying = _make_client()

    _de_client = type("C", (), {"report_unknown_fields": False})()

    station_result = StationResult.de_json(
        {
            "station": {
                "id": {"type": "mood", "tag": "sad"},
                "name": "Грустное",
                "restrictions": {},
                "restrictions2": {},
                "full_image_url": None,
                "id_for_from": "mood-sad",
                "icon": None,
            },
            "settings": None,
            "settings2": None,
            "ad_params": None,
            "rup_title": "Sad Songs",
            "rup_description": "",
        },
        _de_client,
    )

    dashboard = mock.MagicMock(spec=Dashboard)
    dashboard.stations = [station_result]
    underlying.rotor_stations_dashboard.return_value = dashboard

    stations = await client.get_dashboard_stations()

    assert len(stations) == 1
    station_id, name, _image_url = stations[0]
    assert station_id == "mood:sad"
    assert name == "Грустное"  # station.name takes priority over rup_title
    underlying.rotor_stations_dashboard.assert_called_once()


async def test_get_dashboard_stations_empty_on_error() -> None:
    """get_dashboard_stations() returns empty list on network error."""
    client, underlying = _make_client()
    underlying.rotor_stations_dashboard.side_effect = NetworkError("timeout")

    stations = await client.get_dashboard_stations()

    assert stations == []


async def test_get_dashboard_stations_skips_user_type() -> None:
    """get_dashboard_stations() filters out personal 'user' type stations."""
    client, underlying = _make_client()

    _de_client = type("C", (), {"report_unknown_fields": False})()

    personal_station = StationResult.de_json(
        {
            "station": {
                "id": {"type": "user", "tag": "onyourwave"},
                "name": "My Wave",
                "restrictions": {},
                "restrictions2": {},
                "full_image_url": None,
                "id_for_from": "user-onyourwave",
                "icon": None,
            },
            "settings": None,
            "settings2": None,
            "ad_params": None,
            "rup_title": "My Wave",
            "rup_description": "",
        },
        _de_client,
    )

    dashboard = mock.MagicMock(spec=Dashboard)
    dashboard.stations = [personal_station]
    underlying.rotor_stations_dashboard.return_value = dashboard

    stations = await client.get_dashboard_stations()

    assert stations == []
