"""Unit tests for YandexMusicClient (api_client.py)."""

from __future__ import annotations

from unittest import mock

import pytest
from music_assistant_models.errors import ResourceTemporarilyUnavailable
from yandex_music.exceptions import NetworkError

from music_assistant.providers.yandex_music.api_client import YandexMusicClient


def _make_client() -> tuple[YandexMusicClient, mock.AsyncMock]:
    """Create a YandexMusicClient with a mocked underlying ClientAsync.

    :return: Tuple of (YandexMusicClient, mock_underlying_client).
    """
    client = YandexMusicClient(token="fake_token")
    mock_underlying = mock.AsyncMock()
    client._client = mock_underlying
    client._user_id = 12345
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
