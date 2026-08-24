"""Unit tests for YandexMusicClient (api_client.py)."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import re
import time
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, cast
from unittest import mock

import pytest
from music_assistant_models.errors import LoginFailed, ResourceTemporarilyUnavailable
from ya_passport_auth import SecretStr
from yandex_music.exceptions import BadRequestError, NetworkError, UnauthorizedError
from yandex_music.rotor.dashboard import Dashboard
from yandex_music.rotor.station_result import StationResult
from yandex_music.utils.sign_request import DEFAULT_SIGN_KEY

from music_assistant.helpers.throttle_retry import BYPASS_THROTTLER
from music_assistant.providers.yandex_music.api_client import (
    GET_FILE_INFO_CODECS,
    YandexMusicClient,
)
from music_assistant.providers.yandex_music.constants import (
    CAPTCHA_COOLDOWN_LADDER_S,
    INITIAL_SYNC_JITTER_S,
    INITIAL_SYNC_WINDOW_S,
    RESTRICTIVE_GLOBAL_CONCURRENCY,
    THROTTLE_DEFAULT_RPS,
    THROTTLE_METADATA_RPS,
)


def _make_client() -> tuple[YandexMusicClient, mock.AsyncMock]:
    """
    Create a YandexMusicClient with a mocked underlying ClientAsync.

    Also mocks connect() so that _reconnect() restores the mock client
    instead of trying to create a real connection.

    :return: Tuple of (YandexMusicClient, mock_underlying_client).
    """
    client = YandexMusicClient(token=SecretStr("fake_token"))
    mock_underlying = mock.AsyncMock()
    client._client = mock_underlying
    client._user_id = 12345
    # Disable throttling in unit tests — replace every kind with an AsyncMock.
    for kind in client._throttlers:
        client._throttlers[kind] = mock.AsyncMock()

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


async def test_send_rotor_station_feedback_track_started() -> None:
    """send_rotor_station_feedback delegates trackStarted to public helper."""
    client, underlying = _make_client()
    underlying.rotor_station_feedback_track_started = mock.AsyncMock(return_value=True)

    result = await client.send_rotor_station_feedback(
        "user:onyourwave",
        "trackStarted",
        track_id="12345",
        batch_id="batch_xyz",
    )

    assert result is True
    underlying.rotor_station_feedback_track_started.assert_awaited_once()
    args, kwargs = underlying.rotor_station_feedback_track_started.await_args
    assert args[0] == "user:onyourwave"
    assert kwargs["track_id"] == "12345"
    assert kwargs["batch_id"] == "batch_xyz"
    assert "timestamp" in kwargs


async def test_send_rotor_station_feedback_radio_started() -> None:
    """send_rotor_station_feedback delegates radioStarted to public helper with from_."""
    client, underlying = _make_client()
    underlying.rotor_station_feedback_radio_started = mock.AsyncMock(return_value=True)

    result = await client.send_rotor_station_feedback(
        "user:onyourwave",
        "radioStarted",
        batch_id="batch_xyz",
    )

    assert result is True
    underlying.rotor_station_feedback_radio_started.assert_awaited_once()
    _, kwargs = underlying.rotor_station_feedback_radio_started.await_args
    assert kwargs["from_"] == "YandexMusicDesktopAppWindows"
    assert kwargs["batch_id"] == "batch_xyz"


async def test_send_rotor_station_feedback_track_finished() -> None:
    """send_rotor_station_feedback delegates trackFinished with total_played_seconds."""
    client, underlying = _make_client()
    underlying.rotor_station_feedback_track_finished = mock.AsyncMock(return_value=True)

    result = await client.send_rotor_station_feedback(
        "user:onyourwave",
        "trackFinished",
        track_id="12345",
        total_played_seconds=42,
        batch_id="batch_xyz",
    )

    assert result is True
    underlying.rotor_station_feedback_track_finished.assert_awaited_once()
    _, kwargs = underlying.rotor_station_feedback_track_finished.await_args
    assert kwargs["track_id"] == "12345"
    assert kwargs["total_played_seconds"] == 42.0
    assert kwargs["batch_id"] == "batch_xyz"


async def test_send_rotor_station_feedback_skip() -> None:
    """send_rotor_station_feedback delegates skip to public helper."""
    client, underlying = _make_client()
    underlying.rotor_station_feedback_skip = mock.AsyncMock(return_value=True)

    result = await client.send_rotor_station_feedback(
        "user:onyourwave",
        "skip",
        track_id="12345",
        total_played_seconds=10,
    )

    assert result is True
    underlying.rotor_station_feedback_skip.assert_awaited_once()
    _, kwargs = underlying.rotor_station_feedback_skip.await_args
    assert kwargs["track_id"] == "12345"
    assert kwargs["total_played_seconds"] == 10.0


# -- rotor session API (/rotor/session/*) --------------------------------------


def _patch_rotor_session_request(client: YandexMusicClient, response: object) -> mock.AsyncMock:
    """Install a mocked _rotor_session_request on the client and return the mock."""
    req_mock = mock.AsyncMock(return_value=response)
    client._rotor_session_request = req_mock  # type: ignore[method-assign]
    return req_mock


def _patch_get_tracks(client: YandexMusicClient, tracks: list[object]) -> mock.AsyncMock:
    """Install a mocked get_tracks on the client and return the mock."""
    tracks_mock = mock.AsyncMock(return_value=tracks)
    client.get_tracks = tracks_mock  # type: ignore[method-assign]
    return tracks_mock


def _call_args(m: mock.AsyncMock) -> tuple[tuple[Any, ...], Mapping[str, Any]]:
    """
    Return (args, kwargs) from the most recent await on ``m``.

    Raises AssertionError when the mock was never awaited — intentionally
    surfacing missed setup rather than letting mypy's `None is not iterable`
    propagate into destructuring sites.
    """
    call = m.await_args
    assert call is not None, "mock was not awaited"
    return call.args, call.kwargs


async def test_rotor_session_new_posts_expected_body_and_returns_session() -> None:
    """rotor_session_new POSTs to /rotor/session/new with wave-model flags and parses result."""
    client, underlying = _make_client()
    del underlying  # unused; session API bypasses MarshalX client
    response = {
        "radioSessionId": "sess_abc",
        "batchId": "batch_1",
        "sequence": [{"track": {"id": 100, "title": "T"}, "liked": False}],
    }
    req_mock = _patch_rotor_session_request(client, response)
    _patch_get_tracks(client, [type("T", (), {"id": 100})()])

    session_id, tracks, batch_id = await client.rotor_session_new("user:onyourwave")

    req_mock.assert_awaited_once()
    args, _ = _call_args(req_mock)
    path, body = args[0], args[1]
    assert path == "new"
    assert body["seeds"] == ["user:onyourwave"]
    assert body["queue"] == []
    assert body["includeTracksInResponse"] is True
    assert body["includeWaveModel"] is True
    assert body["interactive"] is True
    assert session_id == "sess_abc"
    assert batch_id == "batch_1"
    assert len(tracks) == 1
    assert tracks[0].id == 100


async def test_rotor_session_new_appends_settings_as_seeds() -> None:
    """rotor_session_new appends settingDiversity / settingMoodEnergy / settingLanguage seeds."""
    client, underlying = _make_client()
    del underlying
    req_mock = _patch_rotor_session_request(
        client, {"radioSessionId": "s1", "batchId": "b1", "sequence": []}
    )
    _patch_get_tracks(client, [])

    await client.rotor_session_new(
        "user:onyourwave",
        settings={"diversity": "discover", "moodEnergy": "calm", "language": "russian"},
    )

    args, _ = _call_args(req_mock)
    body = args[1]
    assert body["seeds"] == [
        "user:onyourwave",
        "settingDiversity:discover",
        "settingMoodEnergy:calm",
        "settingLanguage:russian",
    ]


async def test_rotor_session_new_returns_empty_on_missing_session_id() -> None:
    """If the response lacks radioSessionId the call returns (None, [], None) without raising."""
    client, underlying = _make_client()
    del underlying
    _patch_rotor_session_request(client, None)

    session_id, tracks, batch_id = await client.rotor_session_new("user:onyourwave")

    assert session_id is None
    assert tracks == []
    assert batch_id is None


async def test_rotor_session_tracks_posts_current_track_queue() -> None:
    """rotor_session_tracks POSTs {queue: [current_track_id]} and returns tracks + batch_id."""
    client, underlying = _make_client()
    del underlying
    response = {
        "batchId": "batch_2",
        "sequence": [{"track": {"id": 200}}, {"track": {"id": 201}}],
    }
    req_mock = _patch_rotor_session_request(client, response)
    _patch_get_tracks(client, [type("T", (), {"id": 200})(), type("T", (), {"id": 201})()])

    tracks, batch_id = await client.rotor_session_tracks("sess_abc", current_track_id="100")

    args, _ = _call_args(req_mock)
    path, body = args[0], args[1]
    assert path == "sess_abc/tracks"
    assert body == {"queue": ["100"]}
    assert batch_id == "batch_2"
    assert [t.id for t in tracks] == [200, 201]


async def test_rotor_session_feedback_radio_started_sends_from_field() -> None:
    """RadioStarted event uses event.from=track_id (not trackId)."""
    client, underlying = _make_client()
    del underlying
    req_mock = _patch_rotor_session_request(client, {"result": "ok"})

    result = await client.rotor_session_feedback(
        "sess_abc", "radioStarted", track_id="100", batch_id="batch_1"
    )

    assert result is True
    args, _ = _call_args(req_mock)
    path, body = args[0], args[1]
    assert path == "sess_abc/feedback"
    assert body["batchId"] == "batch_1"
    event = body["event"]
    assert event["type"] == "radioStarted"
    assert event["from"] == "100"
    assert "trackId" not in event
    assert "timestamp" in event
    assert re.match(r"^\d{4}-\d{2}-\d{2}T", event["timestamp"])


async def test_rotor_session_feedback_track_started_sends_track_id() -> None:
    """TrackStarted event uses event.trackId (not from)."""
    client, underlying = _make_client()
    del underlying
    req_mock = _patch_rotor_session_request(client, {"result": "ok"})

    await client.rotor_session_feedback(
        "sess_abc", "trackStarted", track_id="100", batch_id="batch_1"
    )

    args, _ = _call_args(req_mock)
    body = args[1]
    event = body["event"]
    assert event["type"] == "trackStarted"
    assert event["trackId"] == "100"
    assert "from" not in event
    assert "totalPlayedSeconds" not in event


async def test_rotor_session_feedback_track_finished_includes_seconds() -> None:
    """TrackFinished event includes totalPlayedSeconds."""
    client, underlying = _make_client()
    del underlying
    req_mock = _patch_rotor_session_request(client, {"result": "ok"})

    await client.rotor_session_feedback(
        "sess_abc",
        "trackFinished",
        track_id="100",
        total_played_seconds=42,
        batch_id="batch_1",
    )

    args, _ = _call_args(req_mock)
    body = args[1]
    event = body["event"]
    assert event["type"] == "trackFinished"
    assert event["trackId"] == "100"
    assert event["totalPlayedSeconds"] == 42


async def test_rotor_session_feedback_skip_includes_seconds() -> None:
    """Skip event includes totalPlayedSeconds and trackId."""
    client, underlying = _make_client()
    del underlying
    req_mock = _patch_rotor_session_request(client, {"result": "ok"})

    await client.rotor_session_feedback(
        "sess_abc", "skip", track_id="100", total_played_seconds=10, batch_id="batch_1"
    )

    args, _ = _call_args(req_mock)
    body = args[1]
    event = body["event"]
    assert event["type"] == "skip"
    assert event["trackId"] == "100"
    assert event["totalPlayedSeconds"] == 10


async def test_rotor_session_feedback_like_uses_trackid_without_seconds() -> None:
    """like/dislike events use trackId but do NOT include totalPlayedSeconds."""
    client, underlying = _make_client()
    del underlying
    req_mock = _patch_rotor_session_request(client, {"result": "ok"})

    await client.rotor_session_feedback("sess_abc", "like", track_id="100", batch_id="batch_1")

    args, _ = _call_args(req_mock)
    body = args[1]
    event = body["event"]
    assert event["type"] == "like"
    assert event["trackId"] == "100"
    assert "totalPlayedSeconds" not in event


async def test_rotor_session_request_maps_unauthorized_to_login_failed() -> None:
    """
    Expired/invalid token during /rotor/session/* surfaces as LoginFailed.

    Without this mapping the raw ``UnauthorizedError`` from the MarshalX
    client would bubble up through browse / play paths and crash the
    provider instead of triggering MA's re-auth prompt.
    """
    client, underlying = _make_client()
    # _do is awaited via _call_with_retry → _ensure_connected → returns our
    # AsyncMock underlying client. The underlying client's ._request.post is
    # what actually raises.
    underlying._request = mock.MagicMock()
    underlying._request.post = mock.AsyncMock(side_effect=UnauthorizedError("stale token"))

    with pytest.raises(LoginFailed):
        await client._rotor_session_request("new", {"seeds": ["user:onyourwave"]})


# -- get_similar_artists ------------------------------------------------------


async def test_get_similar_artists_returns_list() -> None:
    """get_similar_artists returns the similar_artists list from artists_similar()."""
    client, underlying = _make_client()
    similar = [type("Artist", (), {"id": i, "name": f"A{i}"})() for i in (1, 2, 3)]
    result_obj = type("ArtistSimilar", (), {"similar_artists": similar})()
    underlying.artists_similar = mock.AsyncMock(return_value=result_obj)

    result = await client.get_similar_artists("100")

    underlying.artists_similar.assert_awaited_once_with("100")
    assert result == similar


async def test_get_similar_artists_respects_limit() -> None:
    """get_similar_artists truncates results to the requested limit."""
    client, underlying = _make_client()
    similar = [type("Artist", (), {"id": i})() for i in range(10)]
    result_obj = type("ArtistSimilar", (), {"similar_artists": similar})()
    underlying.artists_similar = mock.AsyncMock(return_value=result_obj)

    result = await client.get_similar_artists("100", limit=3)

    assert len(result) == 3
    assert [a.id for a in result] == [0, 1, 2]


async def test_get_similar_artists_handles_none_response() -> None:
    """get_similar_artists returns [] when underlying call returns None."""
    client, underlying = _make_client()
    underlying.artists_similar = mock.AsyncMock(return_value=None)

    result = await client.get_similar_artists("100")

    assert result == []


async def test_get_similar_artists_handles_empty_field() -> None:
    """get_similar_artists returns [] when similar_artists is empty/None."""
    client, underlying = _make_client()
    result_obj = type("ArtistSimilar", (), {"similar_artists": None})()
    underlying.artists_similar = mock.AsyncMock(return_value=result_obj)

    result = await client.get_similar_artists("100")

    assert result == []


async def test_get_similar_artists_returns_empty_on_network_error() -> None:
    """get_similar_artists returns [] when underlying raises NetworkError."""
    client, underlying = _make_client()
    underlying.artists_similar = mock.AsyncMock(
        side_effect=[NetworkError("timeout"), NetworkError("again")]
    )

    result = await client.get_similar_artists("100")

    assert result == []


# -- get_pins / get_music_history / get_artist_about -------------------------


async def test_get_pins_returns_list_object() -> None:
    """get_pins forwards the underlying pins() result."""
    client, underlying = _make_client()
    pins_obj = type("PinsList", (), {"pins": [type("Pin", (), {"type": "album_item"})()]})()
    underlying.pins = mock.AsyncMock(return_value=pins_obj)

    result = await client.get_pins()

    underlying.pins.assert_awaited_once_with()
    assert result is pins_obj


async def test_get_pins_returns_none_on_network_error() -> None:
    """get_pins returns None when retries are exhausted."""
    client, underlying = _make_client()
    underlying.pins = mock.AsyncMock(side_effect=NetworkError("boom"))

    result = await client.get_pins()

    assert result is None


async def test_get_music_history_returns_object() -> None:
    """get_music_history forwards the underlying music_history() result."""
    client, underlying = _make_client()
    history = type("MusicHistory", (), {"history_tabs": []})()
    underlying.music_history = mock.AsyncMock(return_value=history)

    result = await client.get_music_history()

    underlying.music_history.assert_awaited_once_with()
    assert result is history


async def test_get_music_history_returns_none_on_network_error() -> None:
    """get_music_history returns None on persistent NetworkError."""
    client, underlying = _make_client()
    underlying.music_history = mock.AsyncMock(side_effect=NetworkError("boom"))

    assert await client.get_music_history() is None


async def test_get_artist_about_returns_object() -> None:
    """get_artist_about forwards the underlying artists_about() result."""
    client, underlying = _make_client()
    about = type("ArtistAbout", (), {"description": "x", "stats": None})()
    underlying.artists_about = mock.AsyncMock(return_value=about)

    result = await client.get_artist_about("42")

    underlying.artists_about.assert_awaited_once_with("42")
    assert result is about


async def test_get_artist_about_returns_none_on_network_error() -> None:
    """get_artist_about returns None on persistent NetworkError."""
    client, underlying = _make_client()
    underlying.artists_about = mock.AsyncMock(side_effect=NetworkError("boom"))

    assert await client.get_artist_about("42") is None


# -- LRC regex tests ---------------------------------------------------------


def test_lrc_regex_matches_valid_synced_lyrics() -> None:
    """
    LRC regex matches valid synced lyrics with proper format [mm:ss.xx].

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


# -- rate-limit detection -----------------------------------------------------


def test_is_rate_limit_error_detects_429() -> None:
    """_is_rate_limit_error returns True for NetworkError with '429' in the message."""
    client, _ = _make_client()
    err = NetworkError("Bad Request (429): Too Many Requests")
    assert client._is_rate_limit_error(err) is True


def test_is_rate_limit_error_detects_too_many() -> None:
    """_is_rate_limit_error returns True when message contains 'too many requests'."""
    client, _ = _make_client()
    err = NetworkError("too many requests from this IP")
    assert client._is_rate_limit_error(err) is True


def test_is_rate_limit_error_false_for_ordinary_network_error() -> None:
    """_is_rate_limit_error returns False for ordinary connection errors."""
    client, _ = _make_client()
    err = NetworkError("timeout")
    assert client._is_rate_limit_error(err) is False


def test_is_rate_limit_error_false_for_non_network_error() -> None:
    """_is_rate_limit_error returns False for non-NetworkError, even with 'too many' in msg."""
    client, _ = _make_client()
    err = ValueError("too many values to unpack")
    assert client._is_rate_limit_error(err) is False


async def test_call_with_retry_raises_resource_unavailable_on_rate_limit() -> None:
    """_call_with_retry raises ResourceTemporarilyUnavailable when rate-limit is detected."""
    client, underlying = _make_client()

    underlying.tracks = mock.AsyncMock(
        side_effect=NetworkError("Bad Request (429): Too Many Requests")
    )

    with pytest.raises(ResourceTemporarilyUnavailable) as exc_info:
        await client.get_tracks(["42"])

    assert exc_info.value.backoff_time == 60
    # Should not have retried — rate limit errors are not connection errors
    assert underlying.tracks.await_count == 1


async def test_is_connection_error_excludes_rate_limit() -> None:
    """_is_connection_error returns False for rate-limit NetworkErrors (they have 429 in msg)."""
    client, _ = _make_client()
    err = NetworkError("Bad Request (429): Too Many Requests")
    # Rate limit errors should NOT trigger reconnect logic
    assert client._is_connection_error(err) is False


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


# -- get_track_file_info: response key normalization -------------------------


async def test_get_track_file_info_parses_camelcase_download_info() -> None:
    """
    get_track_file_info parses the v3-style camelCase ``downloadInfo`` key.

    The yandex-music v3 client no longer recursively normalises camelCase keys
    inside ``Response.result``. The raw JSON for /get-file-info comes back as
    ``{"downloadInfo": {...}}`` — the provider must accept both shapes.
    """
    client, underlying = _make_client()

    raw_response = {
        "downloadInfo": {
            "trackId": "132401416",
            "quality": "lossless",
            "codec": "flac-mp4",
            "bitrate": 0,
            "transport": "raw",
            "url": "https://example.com/flac-mp4.bin",
            "realId": "132401416",
        }
    }
    underlying._request = mock.MagicMock()
    underlying._request.get = mock.AsyncMock(return_value=raw_response)
    underlying.base_url = "https://api.music.yandex.net"

    result = await client.get_track_file_info("132401416")

    assert result is not None
    assert result["url"] == "https://example.com/flac-mp4.bin"
    assert result["codec"] == "flac-mp4"
    assert result["needs_decryption"] is False


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


# -- _classify_429 + _truncate_err_msg ----------------------------------------


_CAPTCHA_HTML_SNIPPET = (
    "HTTPError (429): <!DOCTYPE html><html><head><title>429</title></head>"
    '<body class="smart-captcha">'
    '<script src="/captcha_smart_qrcode.min.js"></script>'
    'See <a href="https://yandex.ru/support/smart-captcha/about-429.html">'
    "service support form</a>. Доступ к сервису временно запрещён — Yandex "
    "anti-bot edge protection. Try again in a few minutes."
)
# Padding for the captcha truncation test — we need >200 chars to trigger
# the _truncate_err_msg cap and verify production behaviour.
assert len(_CAPTCHA_HTML_SNIPPET) > 200, "captcha snippet must exceed truncate limit"


def test_classify_429_captcha_detects_smart_captcha_html() -> None:
    """_classify_429 returns 'captcha' when the body contains smart-captcha markers."""
    client, _ = _make_client()
    err = NetworkError(_CAPTCHA_HTML_SNIPPET)
    assert client._classify_429(err) == "captcha"


def test_classify_429_plain_429_returns_rate_limit() -> None:
    """_classify_429 returns 'rate_limit' for a bare 429 without captcha markers."""
    client, _ = _make_client()
    err = NetworkError("Bad Request (429): Too Many Requests")
    assert client._classify_429(err) == "rate_limit"


def test_classify_429_non_network_error_returns_other() -> None:
    """_classify_429 returns 'other' for non-NetworkError exceptions even with '429' in msg."""
    client, _ = _make_client()
    err = ValueError("HTTP 429 from some other source")
    assert client._classify_429(err) == "other"


def test_truncate_err_msg_caps_long_html() -> None:
    """_truncate_err_msg never leaks more than `limit` characters of the payload."""
    big = NetworkError("X" * 5000)
    truncated = YandexMusicClient._truncate_err_msg(big, limit=200)
    assert len(truncated) <= 200 + len("...[truncated]")
    assert truncated.endswith("...[truncated]")


# -- captcha vs plain 429 in _call_with_retry ---------------------------------


async def test_call_with_retry_captcha_raises_with_first_strike_backoff() -> None:
    """Captcha response triggers a 15s cooldown on first strike and the HTML body is truncated out."""
    client, underlying = _make_client()
    underlying.tracks = mock.AsyncMock(side_effect=NetworkError(_CAPTCHA_HTML_SNIPPET))

    with pytest.raises(ResourceTemporarilyUnavailable) as exc_info:
        await client.get_tracks(["42"])

    assert exc_info.value.backoff_time == 15
    # The "default" kind owns c.tracks() — block deadline must be set.
    assert client._block_until["default"] > 0
    # The other kinds must remain untouched.
    assert client._block_until["file_info"] == 0.0
    assert client._block_until["rotor"] == 0.0
    # The exception chain must carry a truncated message, not the full HTML.
    cause = exc_info.value.__cause__
    assert cause is not None
    cause_str = str(cause)
    assert cause_str.endswith("...[truncated]")
    # Truncated length is bounded — limit=200 + the truncation suffix.
    assert len(cause_str) <= 200 + len("...[truncated]")


async def test_call_with_retry_plain_429_keeps_60s_backoff_and_no_block() -> None:
    """Plain 429 (no captcha markers) raises with 60s backoff but does NOT engage a block."""
    client, underlying = _make_client()
    underlying.tracks = mock.AsyncMock(
        side_effect=NetworkError("Bad Request (429): Too Many Requests")
    )

    with pytest.raises(ResourceTemporarilyUnavailable) as exc_info:
        await client.get_tracks(["42"])

    assert exc_info.value.backoff_time == 60
    # No kind should be quarantined for a plain 429.
    assert all(v == 0.0 for v in client._block_until.values())


# -- per-kind circuit breaker --------------------------------------------------


async def test_circuit_breaker_blocks_only_affected_kind() -> None:
    """A captcha on 'default' must NOT block 'file_info' or 'rotor' calls."""
    client, underlying = _make_client()
    client._block_until["default"] = time.monotonic() + 600

    # 'default' kind: c.tracks() must fail fast without ever being awaited.
    underlying.tracks = mock.AsyncMock(return_value=[])
    with pytest.raises(ResourceTemporarilyUnavailable) as exc_info:
        await client.get_tracks(["1"])
    assert "default" in str(exc_info.value) or "cooldown" in str(exc_info.value)
    underlying.tracks.assert_not_awaited()

    # 'rotor' kind: rotor_stations_dashboard must still reach the network.
    dashboard = mock.MagicMock(spec=Dashboard)
    dashboard.stations = []
    underlying.rotor_stations_dashboard = mock.AsyncMock(return_value=dashboard)
    _ = await client.get_dashboard_stations()
    underlying.rotor_stations_dashboard.assert_awaited()


async def test_circuit_breaker_captcha_on_file_info_doesnt_block_default() -> None:
    """A captcha-driven file_info block must not affect default-kind calls."""
    client, underlying = _make_client()
    client._block_until["file_info"] = time.monotonic() + 600
    underlying.tracks = mock.AsyncMock(return_value=[])

    # default kind call should pass through.
    await client.get_tracks(["1"])
    underlying.tracks.assert_awaited()


async def test_circuit_breaker_clears_after_deadline() -> None:
    """Once monotonic time passes _block_until, the call proceeds normally."""
    client, underlying = _make_client()
    client._block_until["default"] = time.monotonic() - 1.0  # past
    underlying.tracks = mock.AsyncMock(return_value=[])

    await client.get_tracks(["1"])
    underlying.tracks.assert_awaited()


async def test_bypass_throttler_bypasses_block() -> None:
    """BYPASS_THROTTLER must allow refresh paths through even while a kind is blocked."""
    client, underlying = _make_client()
    client._block_until["file_info"] = time.monotonic() + 600

    raw_response = {
        "downloadInfo": {
            "url": "https://example.com/x",
            "codec": "flac-mp4",
        }
    }
    underlying._request = mock.MagicMock()
    underlying._request.get = mock.AsyncMock(return_value=raw_response)
    underlying.base_url = "https://api.music.yandex.net"

    token = BYPASS_THROTTLER.set(True)
    try:
        result = await client.get_track_file_info("42")
    finally:
        BYPASS_THROTTLER.reset(token)

    assert result is not None
    assert result["url"] == "https://example.com/x"


async def test_captcha_during_bypass_still_engages_block() -> None:
    """
    Captcha received during a BYPASS_THROTTLER call must still quarantine the kind.

    Stream URL refresh runs under BYPASS_THROTTLER to keep an in-flight track
    alive — but if Yandex returns smart-captcha on that very refresh, we DO
    want the file_info kind quarantined so that subsequent NEW-track plays
    fail fast instead of hitting Yandex and prolonging the edge ban.
    The bypass itself still works for the next refresh of the same track.
    """
    client, underlying = _make_client()
    underlying._request = mock.MagicMock()
    underlying._request.get = mock.AsyncMock(side_effect=NetworkError(_CAPTCHA_HTML_SNIPPET))
    underlying.base_url = "https://api.music.yandex.net"

    # Pre-condition: file_info kind is NOT blocked.
    assert client._block_until["file_info"] == 0.0

    token = BYPASS_THROTTLER.set(True)
    try:
        # get_track_file_info swallows ResourceTemporarilyUnavailable and returns None.
        result = await client.get_track_file_info("42")
    finally:
        BYPASS_THROTTLER.reset(token)

    assert result is None
    # The block must have been engaged despite the bypass.
    assert client._block_until["file_info"] > time.monotonic() + 10
    # Other kinds remain free.
    assert client._block_until["default"] == 0.0
    assert client._block_until["rotor"] == 0.0


# -- per-kind throttler routing ------------------------------------------------


async def test_file_info_kind_routes_to_file_info_throttler() -> None:
    """get_track_file_info must acquire the file_info throttler, not default."""
    client, underlying = _make_client()

    raw_response = {
        "downloadInfo": {
            "url": "https://example.com/x",
            "codec": "flac-mp4",
        }
    }
    underlying._request = mock.MagicMock()
    underlying._request.get = mock.AsyncMock(return_value=raw_response)
    underlying.base_url = "https://api.music.yandex.net"

    await client.get_track_file_info("42")

    file_info_acquire = cast("mock.AsyncMock", client._throttlers["file_info"].acquire)
    default_acquire = cast("mock.AsyncMock", client._throttlers["default"].acquire)
    file_info_acquire.assert_awaited()
    default_acquire.assert_not_awaited()


async def test_rotor_kind_routes_to_rotor_throttler() -> None:
    """get_dashboard_stations must acquire the rotor throttler, not default."""
    client, underlying = _make_client()

    dashboard = mock.MagicMock(spec=Dashboard)
    dashboard.stations = []
    underlying.rotor_stations_dashboard = mock.AsyncMock(return_value=dashboard)

    await client.get_dashboard_stations()

    rotor_acquire = cast("mock.AsyncMock", client._throttlers["rotor"].acquire)
    default_acquire = cast("mock.AsyncMock", client._throttlers["default"].acquire)
    rotor_acquire.assert_awaited()
    default_acquire.assert_not_awaited()


# -- get_track_file_info short-TTL cache --------------------------------------


def _make_file_info_response(url: str = "https://example.com/x") -> dict[str, Any]:
    return {
        "downloadInfo": {
            "url": url,
            "codec": "flac-mp4",
            "quality": "lossless",
            "transport": "raw",
        }
    }


async def test_file_info_cache_hit_skips_network() -> None:
    """Second call within TTL returns the cached entry and doesn't hit the network."""
    client, underlying = _make_client()
    underlying._request = mock.MagicMock()
    underlying._request.get = mock.AsyncMock(return_value=_make_file_info_response())
    underlying.base_url = "https://api.music.yandex.net"

    first = await client.get_track_file_info("42")
    second = await client.get_track_file_info("42")

    assert first == second
    underlying._request.get.assert_awaited_once()


async def test_file_info_cache_separates_entries_by_codecs() -> None:
    """
    Different codec preference lists must NOT share a cache slot.

    Yandex picks the codec (and download URL) based on the codec order, so a
    cached response for codecs="flac-mp4,flac" must not be reused when the
    caller requests codecs="mp3".
    """
    client, underlying = _make_client()
    underlying._request = mock.MagicMock()
    underlying._request.get = mock.AsyncMock(return_value=_make_file_info_response())
    underlying.base_url = "https://api.music.yandex.net"

    await client.get_track_file_info("42", codecs="flac-mp4,flac")
    await client.get_track_file_info("42", codecs="mp3")

    # Two different codec lists → two distinct cache entries and two network hits.
    assert underlying._request.get.await_count == 2
    assert ("42", "lossless", "flac-mp4,flac", "raw") in client._file_info_cache
    assert ("42", "lossless", "mp3", "raw") in client._file_info_cache


async def test_file_info_cache_expiry_hits_network_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the cached entry's TTL has elapsed, the next call goes back to network."""
    client, underlying = _make_client()
    underlying._request = mock.MagicMock()
    underlying._request.get = mock.AsyncMock(return_value=_make_file_info_response())
    underlying.base_url = "https://api.music.yandex.net"

    base = time.monotonic()
    current = {"t": base}

    def _fake_monotonic() -> float:
        return current["t"]

    monkeypatch.setattr(
        "music_assistant.providers.yandex_music.api_client.time.monotonic",
        _fake_monotonic,
    )

    await client.get_track_file_info("42")
    current["t"] = base + 9999.0  # well past the TTL
    await client.get_track_file_info("42")

    assert underlying._request.get.await_count == 2


async def test_file_info_cache_invalidated_on_bad_request() -> None:
    """A BadRequestError on the underlying call invalidates the cache for that track."""
    client, underlying = _make_client()
    underlying._request = mock.MagicMock()
    underlying.base_url = "https://api.music.yandex.net"

    cache_key = ("42", "lossless", GET_FILE_INFO_CODECS, "raw")

    # First call: populate cache.
    underlying._request.get = mock.AsyncMock(return_value=_make_file_info_response())
    await client.get_track_file_info("42")
    assert cache_key in client._file_info_cache

    # Trigger the BadRequest code path. A second call WITHOUT bypass would
    # short-circuit on the cache hit and never reach the network — so we use
    # BYPASS_THROTTLER (the same context that stream URL refresh uses) to skip
    # the cache lookup. The 4xx-invalidation runs regardless of bypass.
    underlying._request.get = mock.AsyncMock(side_effect=BadRequestError("nope"))
    token = BYPASS_THROTTLER.set(True)
    try:
        result = await client.get_track_file_info("42")
    finally:
        BYPASS_THROTTLER.reset(token)
    assert result is None
    # Cache invalidated by the BadRequest handler.
    assert cache_key not in client._file_info_cache


async def test_file_info_cache_bypassed_when_bypass_throttler_set() -> None:
    """Under BYPASS_THROTTLER, refresh must hit the network even with cached entry."""
    client, underlying = _make_client()
    underlying._request = mock.MagicMock()
    underlying._request.get = mock.AsyncMock(return_value=_make_file_info_response())
    underlying.base_url = "https://api.music.yandex.net"

    await client.get_track_file_info("42")  # populate cache

    token = BYPASS_THROTTLER.set(True)
    try:
        await client.get_track_file_info("42")
    finally:
        BYPASS_THROTTLER.reset(token)

    assert underlying._request.get.await_count == 2


async def test_file_info_cache_lru_eviction(monkeypatch: pytest.MonkeyPatch) -> None:
    """When the cache exceeds FILE_INFO_CACHE_MAX, the oldest entry is evicted."""
    monkeypatch.setattr(
        "music_assistant.providers.yandex_music.api_client.FILE_INFO_CACHE_MAX",
        2,
    )

    client, underlying = _make_client()
    underlying._request = mock.MagicMock()
    underlying.base_url = "https://api.music.yandex.net"

    counter = {"n": 0}

    async def _vary_response(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        # Return distinct URLs so the cache entries are distinguishable.
        return _make_file_info_response(url=f"https://example.com/{counter['n']}")

    underlying._request.get = mock.AsyncMock(side_effect=_vary_response)

    for tid in ("1", "2", "3"):
        counter["n"] += 1
        await client.get_track_file_info(tid)

    assert len(client._file_info_cache) == 2
    # Oldest ("1") must have been evicted.
    assert ("1", "lossless", GET_FILE_INFO_CODECS, "raw") not in client._file_info_cache
    assert ("2", "lossless", GET_FILE_INFO_CODECS, "raw") in client._file_info_cache
    assert ("3", "lossless", GET_FILE_INFO_CODECS, "raw") in client._file_info_cache


# -- regression tests for upstream Copilot review (PR #3882) -----------------


async def test_check_block_runs_again_after_throttler_acquire() -> None:
    """
    A concurrent request that passed the pre-check must bail after acquire().

    Without the post-acquire re-check, requests already queued in the
    throttler when another request engages the cooldown would proceed to
    the network and prolong Yandex's edge ban.
    """
    client, underlying = _make_client()
    underlying.tracks = mock.AsyncMock(return_value=[])

    # Simulate the race: while we're queued in acquire(), another request
    # engages the captcha block. Model this by setting _block_until as a
    # side effect of the throttler's acquire().
    async def _engage_block_during_queue() -> None:
        client._block_until["default"] = time.monotonic() + 600

    default_acquire = cast("mock.AsyncMock", client._throttlers["default"].acquire)
    default_acquire.side_effect = _engage_block_during_queue

    with pytest.raises(ResourceTemporarilyUnavailable):
        await client.get_tracks(["42"])

    # The actual network call must NEVER have fired.
    underlying.tracks.assert_not_awaited()


async def test_rotor_feedback_no_retry_propagates_429_to_engage_block() -> None:
    """
    Rotor session feedback (with_retry=False) must propagate 429s.

    The inner `_do` swallows ordinary NetworkErrors for fire-and-forget
    paths, but a captcha 429 must reach `_call_no_retry` so the rotor
    cooldown is engaged; otherwise feedback events keep hammering Yandex
    during an active edge ban.
    """
    client, underlying = _make_client()
    underlying._request = mock.MagicMock()
    underlying._request.post = mock.AsyncMock(side_effect=NetworkError(_CAPTCHA_HTML_SNIPPET))
    underlying.base_url = "https://api.music.yandex.net"

    # Pre-condition: rotor kind not blocked.
    assert client._block_until["rotor"] == 0.0

    result = await client.rotor_session_feedback(
        "session-xyz",
        "trackStarted",
        track_id="42",
    )

    # Feedback is fire-and-forget — caller gets False, no raise.
    assert result is False
    # But the rotor cooldown MUST have been engaged (first-strike: 60s).
    assert client._block_until["rotor"] > time.monotonic() + 10
    # Other kinds untouched.
    assert client._block_until["default"] == 0.0
    assert client._block_until["file_info"] == 0.0


async def test_retry_path_classifies_captcha_after_reconnect() -> None:
    """
    A captcha 429 on the reconnect-retry attempt must engage the block.

    Without classification on the retry, the raw NetworkError propagates
    with the full HTML body and the kind cooldown is never set.
    """
    client, underlying = _make_client()
    # First attempt: connection error → triggers reconnect.
    # Retry attempt: captcha 429 → must be classified.
    underlying.tracks = mock.AsyncMock(
        side_effect=[
            NetworkError("Server disconnected"),
            NetworkError(_CAPTCHA_HTML_SNIPPET),
        ]
    )

    with pytest.raises(ResourceTemporarilyUnavailable) as exc_info:
        await client.get_tracks(["42"])

    # Should have backed off for the first-strike captcha cooldown.
    assert exc_info.value.backoff_time == 15
    # Block engaged on default kind.
    assert client._block_until["default"] > time.monotonic() + 10
    # Both attempts ran (connection error + retry).
    assert underlying.tracks.await_count == 2
    # The HTML body must be truncated in the chain, not propagated raw.
    cause = exc_info.value.__cause__
    assert cause is not None
    assert str(cause).endswith("...[truncated]")


async def test_retry_path_re_checks_block_before_retry() -> None:
    """
    A retry after reconnect must re-check the per-kind block.

    Another concurrent task may engage the cooldown while the reconnect is
    in flight; without a re-check, the retry would still hit Yandex during
    the cooldown and prolong the edge ban.
    """
    client, underlying = _make_client()

    async def _fake_reconnect() -> None:
        # Simulate that while this task is reconnecting, another concurrent
        # task hits captcha on the same kind and engages the cooldown.
        client._block_until["default"] = time.monotonic() + 600

    client._reconnect = _fake_reconnect  # type: ignore[method-assign]
    underlying.tracks = mock.AsyncMock(side_effect=NetworkError("Server disconnected"))

    with pytest.raises(ResourceTemporarilyUnavailable) as exc_info:
        await client.get_tracks(["42"])

    # The retry must have bailed BEFORE making a second network call.
    assert underlying.tracks.await_count == 1
    # And the surfaced error must reflect the cooldown, not the connection error.
    assert "cooldown" in str(exc_info.value).lower()


async def test_file_info_cache_hit_blocked_during_cooldown() -> None:
    """
    A populated cache must not be served while the file_info kind is blocked.

    Otherwise the streaming layer would happily replay a pre-cooldown URL
    while Yandex is actively rate-limiting our IP/account, defeating the
    fail-fast guarantee.
    """
    client, underlying = _make_client()
    underlying._request = mock.MagicMock()
    underlying._request.get = mock.AsyncMock(return_value=_make_file_info_response())
    underlying.base_url = "https://api.music.yandex.net"

    # Populate cache.
    await client.get_track_file_info("42")
    assert ("42", "lossless", GET_FILE_INFO_CODECS, "raw") in client._file_info_cache
    assert underlying._request.get.await_count == 1

    # Engage the file_info cooldown.
    client._block_until["file_info"] = time.monotonic() + 600

    # Subsequent call must NOT serve the cached URL.
    result = await client.get_track_file_info("42")
    assert result is None
    # And no extra network call (block fast-fails before the cache lookup).
    assert underlying._request.get.await_count == 1


async def test_bypass_refresh_replaces_cached_entry() -> None:
    """
    BYPASS_THROTTLER refresh must overwrite the existing cache entry.

    Otherwise the next non-bypass caller keeps receiving the old URL until
    the TTL expires, even though refresh has just proven that entry stale.
    """
    client, underlying = _make_client()
    underlying._request = mock.MagicMock()
    underlying.base_url = "https://api.music.yandex.net"

    # First call: populate cache with the OLD URL.
    underlying._request.get = mock.AsyncMock(
        return_value=_make_file_info_response(url="https://example.com/old")
    )
    first = await client.get_track_file_info("42")
    assert first is not None
    assert first["url"] == "https://example.com/old"

    # Refresh under BYPASS_THROTTLER with a fresh URL.
    underlying._request.get = mock.AsyncMock(
        return_value=_make_file_info_response(url="https://example.com/new")
    )
    token = BYPASS_THROTTLER.set(True)
    try:
        refreshed = await client.get_track_file_info("42")
    finally:
        BYPASS_THROTTLER.reset(token)
    assert refreshed is not None
    assert refreshed["url"] == "https://example.com/new"

    # Now a non-bypass caller must get the REFRESHED URL from cache, not the old one.
    underlying._request.get = mock.AsyncMock(
        side_effect=AssertionError("should hit cache, not network")
    )
    cached = await client.get_track_file_info("42")
    assert cached is not None
    assert cached["url"] == "https://example.com/new"


async def test_file_info_cache_invalidated_on_unauthorized() -> None:
    """
    UnauthorizedError on a refresh must clear the cached entry for the track.

    Otherwise post-re-auth callers could be served a URL tied to the
    expired session.
    """
    client, underlying = _make_client()
    underlying._request = mock.MagicMock()
    underlying.base_url = "https://api.music.yandex.net"
    cache_key = ("42", "lossless", GET_FILE_INFO_CODECS, "raw")

    # Populate cache.
    underlying._request.get = mock.AsyncMock(return_value=_make_file_info_response())
    await client.get_track_file_info("42")
    assert cache_key in client._file_info_cache

    # UnauthorizedError on a bypass refresh — must invalidate.
    underlying._request.get = mock.AsyncMock(side_effect=UnauthorizedError("token expired"))
    token = BYPASS_THROTTLER.set(True)
    try:
        result = await client.get_track_file_info("42")
    finally:
        BYPASS_THROTTLER.reset(token)
    assert result is None
    assert cache_key not in client._file_info_cache


# -- captcha cooldown ladder + decay (#146) -----------------------------------


async def test_captcha_first_strike_uses_short_cooldown() -> None:
    """First captcha strike picks the short rung — empirical Yandex recovery ~15s."""
    client, underlying = _make_client()
    underlying.tracks = mock.AsyncMock(side_effect=NetworkError(_CAPTCHA_HTML_SNIPPET))

    with pytest.raises(ResourceTemporarilyUnavailable) as exc_info:
        await client.get_tracks(["42"])

    assert exc_info.value.backoff_time == 15
    assert len(client._captcha_strikes["default"]) == 1


async def test_captcha_second_strike_uses_medium_cooldown() -> None:
    """Second strike in the retention window escalates to 60s."""
    client, underlying = _make_client()
    underlying.tracks = mock.AsyncMock(side_effect=NetworkError(_CAPTCHA_HTML_SNIPPET))

    # First strike
    with pytest.raises(ResourceTemporarilyUnavailable):
        await client.get_tracks(["42"])
    # Clear the block so the second call is allowed to reach the API and trip again.
    client._block_until["default"] = 0.0

    with pytest.raises(ResourceTemporarilyUnavailable) as exc_info:
        await client.get_tracks(["42"])

    assert exc_info.value.backoff_time == 60
    assert len(client._captcha_strikes["default"]) == 2


async def test_captcha_third_strike_uses_max_cooldown() -> None:
    """Third and later strikes cap at 120s."""
    client, underlying = _make_client()
    underlying.tracks = mock.AsyncMock(side_effect=NetworkError(_CAPTCHA_HTML_SNIPPET))

    for _ in range(2):
        with pytest.raises(ResourceTemporarilyUnavailable):
            await client.get_tracks(["42"])
        client._block_until["default"] = 0.0

    with pytest.raises(ResourceTemporarilyUnavailable) as exc_info:
        await client.get_tracks(["42"])

    assert exc_info.value.backoff_time == 120
    assert len(client._captcha_strikes["default"]) == 3


async def test_captcha_fourth_strike_stays_at_max_cooldown() -> None:
    """Strikes beyond the ladder length stay capped at the last rung (120s)."""
    client, underlying = _make_client()
    underlying.tracks = mock.AsyncMock(side_effect=NetworkError(_CAPTCHA_HTML_SNIPPET))

    for _ in range(3):
        with pytest.raises(ResourceTemporarilyUnavailable):
            await client.get_tracks(["42"])
        client._block_until["default"] = 0.0

    with pytest.raises(ResourceTemporarilyUnavailable) as exc_info:
        await client.get_tracks(["42"])

    assert exc_info.value.backoff_time == 120


async def test_captcha_strikes_decay_after_retention_window() -> None:
    """Strikes outside CAPTCHA_STRIKE_RETENTION_S are forgotten — ladder resets."""
    client, underlying = _make_client()
    underlying.tracks = mock.AsyncMock(side_effect=NetworkError(_CAPTCHA_HTML_SNIPPET))

    # Two strikes in quick succession.
    with pytest.raises(ResourceTemporarilyUnavailable):
        await client.get_tracks(["42"])
    client._block_until["default"] = 0.0
    with pytest.raises(ResourceTemporarilyUnavailable):
        await client.get_tracks(["42"])
    client._block_until["default"] = 0.0
    assert len(client._captcha_strikes["default"]) == 2

    # Age both strikes past the retention window.
    aged = time.monotonic() - 3700.0  # > CAPTCHA_STRIKE_RETENTION_S (3600s)
    client._captcha_strikes["default"].clear()
    client._captcha_strikes["default"].extend([aged, aged])

    with pytest.raises(ResourceTemporarilyUnavailable) as exc_info:
        await client.get_tracks(["42"])

    # Aged strikes were trimmed; this is a "fresh" first strike again.
    assert exc_info.value.backoff_time == 15
    assert len(client._captcha_strikes["default"]) == 1


async def test_captcha_strikes_per_kind_isolated() -> None:
    """A captcha on file_info must not bump the default strike counter."""
    client, underlying = _make_client()
    underlying._request = mock.MagicMock()
    underlying._request.get = mock.AsyncMock(side_effect=NetworkError(_CAPTCHA_HTML_SNIPPET))
    underlying.base_url = "https://api.music.yandex.net"

    # Trip captcha on file_info via the BYPASS_THROTTLER + get_track_file_info path
    # (which swallows the exception and returns None).
    token = BYPASS_THROTTLER.set(True)
    try:
        result = await client.get_track_file_info("42")
    finally:
        BYPASS_THROTTLER.reset(token)
    assert result is None

    assert len(client._captcha_strikes["file_info"]) == 1
    assert len(client._captcha_strikes["default"]) == 0


# -- metadata throttler kind (#146) -------------------------------------------


def test_metadata_kind_uses_separate_throttler() -> None:
    """`metadata` resolves to a different Throttler than `default`."""
    client = YandexMusicClient(token=SecretStr("fake_token"))
    assert client._get_throttler("metadata") is not client._get_throttler("default")
    assert client._get_throttler("metadata") is not client._get_throttler("file_info")
    assert client._get_throttler("metadata") is not client._get_throttler("rotor")


async def test_metadata_captcha_does_not_block_default() -> None:
    """A captcha-driven `metadata` block must not stop `default` calls."""
    client, underlying = _make_client()
    client._block_until["metadata"] = time.monotonic() + 600

    underlying.tracks = mock.AsyncMock(return_value=[])
    await client.get_tracks(["1"])
    underlying.tracks.assert_awaited()


async def test_default_captcha_does_not_block_metadata() -> None:
    """A captcha-driven `default` block must not stop `metadata` calls."""
    client, underlying = _make_client()
    client._block_until["default"] = time.monotonic() + 600

    underlying.artists = mock.AsyncMock(return_value=[mock.MagicMock()])
    result = await client.get_artist("42")
    assert result is not None
    underlying.artists.assert_awaited()


@pytest.mark.parametrize(
    ("method_name", "underlying_attr", "underlying_return", "call_args"),
    [
        ("get_album", "albums", [mock.MagicMock()], ("42",)),
        (
            "get_album_with_tracks",
            "albums_with_tracks",
            mock.MagicMock(),
            ("42",),
        ),
        ("get_artist", "artists", [mock.MagicMock()], ("42",)),
        (
            "get_artist_albums",
            "artists_direct_albums",
            mock.MagicMock(albums=[mock.MagicMock()]),
            ("42",),
        ),
        ("get_artist_about", "artists_about", mock.MagicMock(), ("42",)),
        (
            "get_artist_tracks",
            "artists_tracks",
            mock.MagicMock(tracks=[mock.MagicMock()]),
            ("42",),
        ),
    ],
)
async def test_metadata_methods_use_metadata_throttler(
    method_name: str,
    underlying_attr: str,
    underlying_return: Any,
    call_args: tuple[str, ...],
) -> None:
    """Each metadata-refresh method must acquire the metadata throttler."""
    client, underlying = _make_client()
    setattr(underlying, underlying_attr, mock.AsyncMock(return_value=underlying_return))

    method = getattr(client, method_name)
    await method(*call_args)

    metadata_throttler = cast("mock.AsyncMock", client._throttlers["metadata"])
    default_throttler = cast("mock.AsyncMock", client._throttlers["default"])
    metadata_throttler.acquire.assert_awaited()
    default_throttler.acquire.assert_not_awaited()


# -- initial-sync jitter window (#146) ----------------------------------------


async def test_jitter_applied_for_default_within_initial_sync_window() -> None:
    """`default` calls within INITIAL_SYNC_WINDOW_S get a positive jitter delay."""
    client, underlying = _make_client()
    client._connected_at = time.monotonic()  # window is currently active
    underlying.tracks = mock.AsyncMock(return_value=[])

    with (
        mock.patch(
            "music_assistant.providers.yandex_music.api_client.random.uniform",
            return_value=0.25,
        ),
        mock.patch(
            "music_assistant.providers.yandex_music.api_client.asyncio.sleep",
            new_callable=mock.AsyncMock,
        ) as sleep_mock,
    ):
        await client.get_tracks(["1"])

    sleep_mock.assert_awaited()
    assert sleep_mock.await_args is not None
    delay = sleep_mock.await_args.args[0]
    assert 0.0 <= delay <= 0.5  # INITIAL_SYNC_JITTER_S = 0.5


async def test_jitter_applied_for_metadata_within_initial_sync_window() -> None:
    """`metadata` calls within INITIAL_SYNC_WINDOW_S get a positive jitter delay."""
    client, underlying = _make_client()
    client._connected_at = time.monotonic()
    underlying.artists = mock.AsyncMock(return_value=[mock.MagicMock()])

    with (
        mock.patch(
            "music_assistant.providers.yandex_music.api_client.random.uniform",
            return_value=0.25,
        ),
        mock.patch(
            "music_assistant.providers.yandex_music.api_client.asyncio.sleep",
            new_callable=mock.AsyncMock,
        ) as sleep_mock,
    ):
        await client.get_artist("1")

    sleep_mock.assert_awaited()


async def test_jitter_skipped_after_initial_sync_window() -> None:
    """Outside INITIAL_SYNC_WINDOW_S the helper is a no-op."""
    client, underlying = _make_client()
    # Connected 120s ago — well past the 60s window.
    client._connected_at = time.monotonic() - 120.0
    underlying.tracks = mock.AsyncMock(return_value=[])

    with mock.patch(
        "music_assistant.providers.yandex_music.api_client.asyncio.sleep",
        new_callable=mock.AsyncMock,
    ) as sleep_mock:
        await client.get_tracks(["1"])

    sleep_mock.assert_not_awaited()


async def test_jitter_skipped_when_never_connected() -> None:
    """If _connected_at is None (no successful connect yet), jitter is skipped."""
    client, underlying = _make_client()
    client._connected_at = None
    underlying.tracks = mock.AsyncMock(return_value=[])

    with mock.patch(
        "music_assistant.providers.yandex_music.api_client.asyncio.sleep",
        new_callable=mock.AsyncMock,
    ) as sleep_mock:
        await client.get_tracks(["1"])

    sleep_mock.assert_not_awaited()


async def test_jitter_skipped_for_file_info_kind() -> None:
    """file_info is on the streaming hot path — jitter must never apply."""
    client, underlying = _make_client()
    client._connected_at = time.monotonic()  # window active
    raw_response = {
        "downloadInfo": {
            "url": "https://example.com/x",
            "codec": "flac-mp4",
        }
    }
    underlying._request = mock.MagicMock()
    underlying._request.get = mock.AsyncMock(return_value=raw_response)
    underlying.base_url = "https://api.music.yandex.net"

    with mock.patch(
        "music_assistant.providers.yandex_music.api_client.asyncio.sleep",
        new_callable=mock.AsyncMock,
    ) as sleep_mock:
        await client.get_track_file_info("42")

    sleep_mock.assert_not_awaited()


async def test_jitter_skipped_for_rotor_kind() -> None:
    """Rotor has its own bucket — jitter must never apply."""
    client, underlying = _make_client()
    client._connected_at = time.monotonic()
    dashboard = mock.MagicMock(spec=Dashboard)
    dashboard.stations = []
    underlying.rotor_stations_dashboard = mock.AsyncMock(return_value=dashboard)

    with mock.patch(
        "music_assistant.providers.yandex_music.api_client.asyncio.sleep",
        new_callable=mock.AsyncMock,
    ) as sleep_mock:
        await client.get_dashboard_stations()

    sleep_mock.assert_not_awaited()
    assert len(client._captcha_strikes["metadata"]) == 0


# -- regression pins (#146) ---------------------------------------------------


def test_throttle_default_rps_is_5() -> None:
    """Pin the default RPS — empirical probing showed Yandex tolerates ≥10."""
    assert THROTTLE_DEFAULT_RPS == 5


def test_throttle_metadata_rps_is_3() -> None:
    """Pin the metadata RPS."""
    assert THROTTLE_METADATA_RPS == 3


def test_captcha_cooldown_ladder_is_15_60_120() -> None:
    """Pin the shortened ladder — empirical recovery time was ~15s, not 60s."""
    assert CAPTCHA_COOLDOWN_LADDER_S == (15.0, 60.0, 120.0)


def test_initial_sync_window_constants() -> None:
    """Pin the jitter window defaults."""
    assert INITIAL_SYNC_JITTER_S == 0.5
    assert INITIAL_SYNC_WINDOW_S == 60.0


def test_classify_429_behavior_unchanged_smart_captcha() -> None:
    """Existing captcha classification still detects smart-captcha markers."""
    client, _ = _make_client()
    err = NetworkError(_CAPTCHA_HTML_SNIPPET)
    assert client._classify_429(err) == "captcha"


def test_classify_429_behavior_unchanged_plain_429() -> None:
    """Existing classification still returns 'rate_limit' for bare 429."""
    client, _ = _make_client()
    err = NetworkError("Bad Request (429): Too Many Requests")
    assert client._classify_429(err) == "rate_limit"


def test_classify_429_behavior_unchanged_non_network() -> None:
    """Existing classification still returns 'other' for non-NetworkError."""
    client, _ = _make_client()
    err = ValueError("HTTP 429 from some other source")
    assert client._classify_429(err) == "other"


# -- RTU propagation regression (#146): metadata methods must NOT swallow ----
# the captcha cooldown. ResourceTemporarilyUnavailable is a sibling of
# ProviderUnavailableError under MusicAssistantError, not a descendant, so
# the (BadRequestError, NetworkError, ProviderUnavailableError) catch tuple
# correctly lets RTU propagate. These tests pin that contract — a future
# refactor widening the catch to MusicAssistantError would silently defeat
# the entire #146 cooldown mechanism.


async def test_get_album_propagates_captcha_rtu() -> None:
    """A captcha trip in get_album must raise RTU, not return None."""
    client, underlying = _make_client()
    underlying.albums = mock.AsyncMock(side_effect=NetworkError(_CAPTCHA_HTML_SNIPPET))
    with pytest.raises(ResourceTemporarilyUnavailable) as exc_info:
        await client.get_album("42")
    assert exc_info.value.backoff_time == 15


async def test_get_album_with_tracks_propagates_captcha_rtu() -> None:
    """A captcha trip in get_album_with_tracks must raise RTU, not return None."""
    client, underlying = _make_client()
    underlying.albums_with_tracks = mock.AsyncMock(side_effect=NetworkError(_CAPTCHA_HTML_SNIPPET))
    with pytest.raises(ResourceTemporarilyUnavailable) as exc_info:
        await client.get_album_with_tracks("42")
    assert exc_info.value.backoff_time == 15


async def test_get_artist_propagates_captcha_rtu() -> None:
    """A captcha trip in get_artist must raise RTU, not return None."""
    client, underlying = _make_client()
    underlying.artists = mock.AsyncMock(side_effect=NetworkError(_CAPTCHA_HTML_SNIPPET))
    with pytest.raises(ResourceTemporarilyUnavailable) as exc_info:
        await client.get_artist("42")
    assert exc_info.value.backoff_time == 15


async def test_get_artist_albums_propagates_captcha_rtu() -> None:
    """A captcha trip in get_artist_albums must raise RTU, not return []."""
    client, underlying = _make_client()
    underlying.artists_direct_albums = mock.AsyncMock(
        side_effect=NetworkError(_CAPTCHA_HTML_SNIPPET)
    )
    with pytest.raises(ResourceTemporarilyUnavailable) as exc_info:
        await client.get_artist_albums("42")
    assert exc_info.value.backoff_time == 15


async def test_get_artist_about_propagates_captcha_rtu() -> None:
    """A captcha trip in get_artist_about must raise RTU, not return None."""
    client, underlying = _make_client()
    underlying.artists_about = mock.AsyncMock(side_effect=NetworkError(_CAPTCHA_HTML_SNIPPET))
    with pytest.raises(ResourceTemporarilyUnavailable) as exc_info:
        await client.get_artist_about("42")
    assert exc_info.value.backoff_time == 15


async def test_get_artist_tracks_propagates_captcha_rtu() -> None:
    """A captcha trip in get_artist_tracks must raise RTU, not return []."""
    client, underlying = _make_client()
    underlying.artists_tracks = mock.AsyncMock(side_effect=NetworkError(_CAPTCHA_HTML_SNIPPET))
    with pytest.raises(ResourceTemporarilyUnavailable) as exc_info:
        await client.get_artist_tracks("42")
    assert exc_info.value.backoff_time == 15


# -- jitter respects BYPASS_THROTTLER (#146) ---------------------------------


async def test_jitter_skipped_under_bypass_throttler() -> None:
    """
    Stream URL refresh paths run under BYPASS_THROTTLER — jitter must not fire.

    The helper sits inside the ``if not BYPASS_THROTTLER.get():`` block in
    both _call_with_retry and _call_no_retry. If a future refactor lifts
    the jitter call out of that block, stream URL refresh would eat up to
    INITIAL_SYNC_JITTER_S of avoidable latency during the first
    INITIAL_SYNC_WINDOW_S after every connect — exactly when reconnect
    storms make latency hurt the most.
    """
    client, underlying = _make_client()
    client._connected_at = time.monotonic()  # window is active

    raw_response = {
        "downloadInfo": {
            "url": "https://example.com/x",
            "codec": "flac-mp4",
        }
    }
    underlying._request = mock.MagicMock()
    underlying._request.get = mock.AsyncMock(return_value=raw_response)
    underlying.base_url = "https://api.music.yandex.net"

    with mock.patch(
        "music_assistant.providers.yandex_music.api_client.asyncio.sleep",
        new_callable=mock.AsyncMock,
    ) as sleep_mock:
        token = BYPASS_THROTTLER.set(True)
        try:
            await client.get_track_file_info("42")
        finally:
            BYPASS_THROTTLER.reset(token)

    sleep_mock.assert_not_awaited()


# -- M5: BadRequestError handling (4xx is terminal, not retryable) -----------


async def test_search_swallows_bad_request_as_empty_result() -> None:
    """
    A 4xx from Yandex search is terminal — return None, do not signal retry.

    Wrapping ``BadRequestError`` as ``ResourceTemporarilyUnavailable`` tells
    Music Assistant the request can be retried, which reproduces the same
    failure in a loop. The right answer is "no result".
    """
    client, underlying = _make_client()
    underlying.search = mock.AsyncMock(side_effect=BadRequestError("malformed query"))

    result = await client.search("any query")

    assert result is None
    underlying.search.assert_awaited_once()


async def test_get_liked_tracks_swallows_bad_request_as_empty_list() -> None:
    """Terminal 4xx for liked tracks returns ``[]`` — not a retryable failure."""
    client, underlying = _make_client()
    underlying.users_likes_tracks = mock.AsyncMock(side_effect=BadRequestError("not allowed"))

    result = await client.get_liked_tracks()

    assert result == []


async def test_get_liked_albums_swallows_bad_request_as_empty_list() -> None:
    """Terminal 4xx for liked albums returns ``[]`` — not a retryable failure."""
    client, underlying = _make_client()
    underlying.users_likes_albums = mock.AsyncMock(side_effect=BadRequestError("not allowed"))

    result = await client.get_liked_albums()

    assert result == []


# -- M8: get_liked_tracks tolerates naive timestamps from yandex-music --------


async def test_get_liked_tracks_sort_survives_naive_timestamp() -> None:
    """
    Sorting must not crash when ``TrackShort.timestamp`` is timezone-naive.

    The upstream ``yandex-music`` library is inconsistent about tz on
    ``TrackShort.timestamp``. Comparing a naive ``datetime`` against the
    previous ``datetime.min.replace(tzinfo=UTC)`` sentinel raises
    ``TypeError: can't compare offset-naive and offset-aware datetimes``
    and the whole liked-tracks collection fails to load.
    """
    client, underlying = _make_client()

    naive_ts = datetime(2024, 1, 1, 12, 0, 0)  # noqa: DTZ001 — naive on purpose
    aware_ts = datetime(2024, 6, 1, 12, 0, 0, tzinfo=UTC)
    track_naive = type("T", (), {"id": 1, "timestamp": naive_ts})()
    track_aware = type("T", (), {"id": 2, "timestamp": aware_ts})()
    track_missing = type("T", (), {"id": 3})()  # no .timestamp at all

    result_obj = type("R", (), {"tracks": [track_naive, track_aware, track_missing]})()
    underlying.users_likes_tracks = mock.AsyncMock(return_value=result_obj)

    result = await client.get_liked_tracks()

    assert {t.id for t in result} == {1, 2, 3}


# -- M9: _call_with_retry re-acquires the throttler on reconnect retry ---------


async def test_call_with_retry_reacquires_throttler_on_reconnect() -> None:
    """
    The reconnect-retry path must consume a throttler token too.

    Skipping ``throttler.acquire()`` on the second attempt doubles the
    effective request rate during connection flap — exactly the conditions
    that already increase the risk of Yandex's smart-captcha tripping.
    """
    client, underlying = _make_client()

    # Make .tracks fail once with a connection error, then succeed.
    track = type("T", (), {"id": 42})()
    underlying.tracks = mock.AsyncMock(side_effect=[NetworkError("ECONNRESET"), [track]])

    result = await client.get_tracks(["42"])

    assert result == [track]
    # The throttler used by get_tracks falls under the "default" kind.
    default_throttler = client._throttlers["default"]
    assert default_throttler.acquire.await_count == 2, (  # type: ignore[attr-defined]
        "throttler must be re-acquired on the reconnect-retry attempt"
    )


async def test_jitter_skipped_when_kind_already_blocked() -> None:
    """
    A blocked kind must fast-fail BEFORE the jitter sleep.

    Order contract in _call_with_retry: _check_block -> jitter -> acquire ->
    _check_block. The pre-check raises RTU immediately when the kind is
    quarantined, so the jitter sleep never runs. A refactor that reorders
    these calls would turn a fast-fail circuit breaker into a slow-fail
    one during the first INITIAL_SYNC_WINDOW_S after connect — exactly
    when MA's library walker is hammering the provider hardest.
    """
    client, underlying = _make_client()
    client._connected_at = time.monotonic()  # window is active
    client._block_until["default"] = time.monotonic() + 600  # kind quarantined
    underlying.tracks = mock.AsyncMock(return_value=[])

    with (
        mock.patch(
            "music_assistant.providers.yandex_music.api_client.asyncio.sleep",
            new_callable=mock.AsyncMock,
        ) as sleep_mock,
        pytest.raises(ResourceTemporarilyUnavailable),
    ):
        await client.get_tracks(["1"])

    sleep_mock.assert_not_awaited()
    # Fast-fail: underlying API was never called.
    underlying.tracks.assert_not_awaited()


# -- Per-endpoint concurrency lock (defense-in-depth vs Yandex captcha) -------


async def test_parallel_same_endpoint_calls_serialize() -> None:
    """
    Parallel calls to the same endpoint must run one-at-a-time.

    Yandex's edge treats concurrent requests to the same URL family as a
    scraper signature and trips captcha within ~460 ms. The per-endpoint
    lock in ``_call_with_retry`` is the defense-in-depth that prevents a
    future ``asyncio.gather`` from re-introducing the same burst pattern.
    """
    client, underlying = _make_client()

    concurrent_peak = 0
    in_flight = 0
    lock = asyncio.Lock()

    async def _slow_tracks(_track_ids: list[str]) -> list[Any]:
        nonlocal concurrent_peak, in_flight
        async with lock:
            in_flight += 1
            concurrent_peak = max(concurrent_peak, in_flight)
        try:
            await asyncio.sleep(0.05)
            return []
        finally:
            async with lock:
                in_flight -= 1

    underlying.tracks = _slow_tracks

    # Fire 5 parallel calls to the SAME method; per-endpoint lock should
    # serialise them despite ``asyncio.gather`` queueing them simultaneously.
    await asyncio.gather(*(client.get_tracks([str(i)]) for i in range(5)))

    assert concurrent_peak == 1, (
        f"per-endpoint lock failed to serialise; saw {concurrent_peak} concurrent calls"
    )


async def test_restrictive_mode_caps_global_concurrency() -> None:
    """
    Restrictive mode caps total in-flight requests to ``RESTRICTIVE_GLOBAL_CONCURRENCY``.

    Yandex's edge enforces a per-token concurrency limit on datacenter /
    VPN IPs (empirically ~6 simultaneous before captcha). The
    restrictive_rate_limits toggle adds a token-wide semaphore so the
    provider stays under that ceiling regardless of how the call sites
    fan out.
    """
    client = YandexMusicClient(token=SecretStr("fake"), restrictive_rate_limits=True)
    mock_underlying = mock.AsyncMock()
    client._client = mock_underlying
    client._user_id = 12345
    for kind in client._throttlers:
        client._throttlers[kind] = mock.AsyncMock()

    async def _fake_connect() -> bool:
        client._client = mock_underlying
        return True

    client.connect = _fake_connect  # type: ignore[method-assign]

    concurrent_peak = 0
    in_flight = 0
    state_lock = asyncio.Lock()

    # Stub direct on YandexMusicClient methods (each one different) so the
    # per-endpoint lock cannot also bound this — only the global semaphore
    # should. We hijack ``_call_with_retry`` itself: it's the place every
    # method funnels through, and instrumenting it lets us count true
    # in-flight invocations without re-shaping every yandex_music response.
    real_invoke = client._invoke_under_endpoint_lock

    async def _instrumented(_func: Any, _real_client: Any, _endpoint: Any) -> Any:
        nonlocal concurrent_peak, in_flight
        async with state_lock:
            in_flight += 1
            concurrent_peak = max(concurrent_peak, in_flight)
        try:
            await asyncio.sleep(0.05)
        finally:
            async with state_lock:
                in_flight -= 1
        # Bypass the actual HTTP call after measuring — we only care about
        # how many entered the gate at once, not what they return.
        return mock.MagicMock()

    client._invoke_under_endpoint_lock = _instrumented  # type: ignore[method-assign,assignment]

    async def _call(i: int) -> Any:
        # Each iteration uses a different ``__qualname__`` so per-endpoint
        # locks don't interfere with the measurement.
        async def _fake(_c: Any) -> Any:
            return None

        _fake.__qualname__ = f"YandexMusicClient.synthetic_{i}.<locals>.<lambda>"
        return await client._call_with_retry(_fake, kind="default")

    # Fire 8 parallel calls. Without the global semaphore, peak concurrency
    # would be 8. With it, peak ≤ RESTRICTIVE_GLOBAL_CONCURRENCY.
    await asyncio.gather(*(_call(i) for i in range(8)))

    # restore
    client._invoke_under_endpoint_lock = real_invoke  # type: ignore[method-assign]

    assert concurrent_peak <= RESTRICTIVE_GLOBAL_CONCURRENCY, (
        f"restrictive mode failed to cap global concurrency; "
        f"peak={concurrent_peak} > {RESTRICTIVE_GLOBAL_CONCURRENCY}"
    )


async def test_parallel_different_endpoints_run_concurrently() -> None:
    """
    Calls to different endpoint methods must NOT block each other.

    The per-endpoint lock is keyed on the calling method's qualname, so
    parallel calls to distinct YandexMusicClient methods proceed in
    parallel (subject to throttler/RPS).
    """
    client, underlying = _make_client()

    concurrent_peak = 0
    in_flight = 0
    lock = asyncio.Lock()

    async def _slow(*_args: Any, **_kwargs: Any) -> Any:
        nonlocal concurrent_peak, in_flight
        async with lock:
            in_flight += 1
            concurrent_peak = max(concurrent_peak, in_flight)
        try:
            await asyncio.sleep(0.05)
            return []
        finally:
            async with lock:
                in_flight -= 1

    underlying.tracks = _slow
    underlying.users_likes_albums = _slow

    await asyncio.gather(
        client.get_tracks(["1"]),
        client.get_liked_albums(),
    )

    assert concurrent_peak == 2, (
        f"different endpoints should run in parallel; saw peak={concurrent_peak}"
    )
