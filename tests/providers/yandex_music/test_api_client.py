"""Unit tests for YandexMusicClient (api_client.py)."""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
from collections.abc import Mapping
from typing import Any
from unittest import mock

import pytest
from music_assistant_models.errors import LoginFailed, ResourceTemporarilyUnavailable
from ya_passport_auth import SecretStr
from yandex_music.exceptions import NetworkError, UnauthorizedError
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
    client = YandexMusicClient(token=SecretStr("fake_token"))
    mock_underlying = mock.AsyncMock()
    client._client = mock_underlying
    client._user_id = 12345
    client._throttler = mock.AsyncMock()  # disable throttling in unit tests

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
    """Return (args, kwargs) from the most recent await on ``m``.

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
    """Expired/invalid token during /rotor/session/* surfaces as LoginFailed.

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
    """get_track_file_info parses the v3-style camelCase ``downloadInfo`` key.

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
