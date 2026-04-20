"""Unit tests for the KION Music API client."""

from __future__ import annotations

import base64
import hashlib
import hmac
from unittest import mock

import pytest
from yandex_music.exceptions import NetworkError
from yandex_music.utils.sign_request import DEFAULT_SIGN_KEY

from music_assistant.providers.kion_music.api_client import KionMusicClient
from music_assistant.providers.kion_music.constants import DEFAULT_BASE_URL


@pytest.fixture
def client() -> KionMusicClient:
    """Return a KionMusicClient with a fake token and explicit base URL."""
    return KionMusicClient("fake_token", base_url=DEFAULT_BASE_URL)


async def test_connect_sets_base_url(client: KionMusicClient) -> None:
    """Verify connect() passes DEFAULT_BASE_URL to ClientAsync."""
    with mock.patch("music_assistant.providers.kion_music.api_client.ClientAsync") as mock_cls:
        mock_instance = mock.AsyncMock()
        mock_instance.me = type("Me", (), {"account": type("Account", (), {"uid": 42})()})()
        mock_instance.init = mock.AsyncMock(return_value=mock_instance)
        mock_cls.return_value = mock_instance

        result = await client.connect()

        assert result is True
        mock_cls.assert_called_once_with("fake_token", base_url=DEFAULT_BASE_URL)


async def test_get_liked_albums_batching(client: KionMusicClient) -> None:
    """Test that liked albums are fetched in batches of 50."""
    mock_client = mock.AsyncMock()
    client._client = mock_client
    client._user_id = 1

    # Create 60 likes so we get 2 batches
    likes = []
    for i in range(60):
        like = type("Like", (), {"album": type("Album", (), {"id": i + 1})()})()
        likes.append(like)

    mock_client.users_likes_albums = mock.AsyncMock(return_value=likes)

    batch1 = [type("Album", (), {"id": i + 1})() for i in range(50)]
    batch2 = [type("Album", (), {"id": i + 51})() for i in range(10)]
    mock_client.albums = mock.AsyncMock(side_effect=[batch1, batch2])

    result = await client.get_liked_albums()

    assert len(result) == 60
    assert mock_client.albums.call_count == 2


async def test_get_liked_albums_batch_fallback_on_network_error(
    client: KionMusicClient,
) -> None:
    """Test fallback to minimal data when batch fetch fails."""
    mock_client = mock.AsyncMock()
    client._client = mock_client
    client._user_id = 1

    album_obj = type("Album", (), {"id": 1})()
    likes = [type("Like", (), {"album": album_obj})()]

    mock_client.users_likes_albums = mock.AsyncMock(return_value=likes)
    mock_client.albums = mock.AsyncMock(side_effect=NetworkError("timeout"))

    result = await client.get_liked_albums()

    assert len(result) == 1
    assert result[0].id == 1


# ─── send_rotor_station_feedback: dispatch to typed v3 helpers ────────────────


async def test_rotor_feedback_radio_started_calls_typed_helper(
    client: KionMusicClient,
) -> None:
    """RadioStarted dispatches to rotor_station_feedback_radio_started."""
    mock_client = mock.AsyncMock()
    mock_client.rotor_station_feedback_radio_started = mock.AsyncMock(return_value=True)
    mock_client.rotor_station_feedback = mock.AsyncMock(return_value=True)
    client._client = mock_client

    ok = await client.send_rotor_station_feedback("user:onyourwave", "radioStarted")

    assert ok is True
    mock_client.rotor_station_feedback_radio_started.assert_awaited_once()
    mock_client.rotor_station_feedback.assert_not_called()


async def test_rotor_feedback_track_started_requires_track_id(
    client: KionMusicClient,
) -> None:
    """TrackStarted without track_id returns False and skips the API call."""
    mock_client = mock.AsyncMock()
    mock_client.rotor_station_feedback_track_started = mock.AsyncMock(return_value=True)
    client._client = mock_client

    ok = await client.send_rotor_station_feedback("user:onyourwave", "trackStarted", track_id=None)

    assert ok is False
    mock_client.rotor_station_feedback_track_started.assert_not_called()


async def test_rotor_feedback_track_started_calls_typed_helper(
    client: KionMusicClient,
) -> None:
    """TrackStarted with track_id dispatches to rotor_station_feedback_track_started."""
    mock_client = mock.AsyncMock()
    mock_client.rotor_station_feedback_track_started = mock.AsyncMock(return_value=True)
    client._client = mock_client

    ok = await client.send_rotor_station_feedback(
        "user:onyourwave", "trackStarted", track_id="42", batch_id="batch-1"
    )

    assert ok is True
    mock_client.rotor_station_feedback_track_started.assert_awaited_once()
    args, kwargs = mock_client.rotor_station_feedback_track_started.call_args
    assert args[0] == "user:onyourwave"
    assert kwargs["track_id"] == "42"
    assert kwargs["batch_id"] == "batch-1"


async def test_rotor_feedback_track_finished_calls_typed_helper(
    client: KionMusicClient,
) -> None:
    """TrackFinished dispatches to rotor_station_feedback_track_finished with seconds."""
    mock_client = mock.AsyncMock()
    mock_client.rotor_station_feedback_track_finished = mock.AsyncMock(return_value=True)
    client._client = mock_client

    ok = await client.send_rotor_station_feedback(
        "user:onyourwave",
        "trackFinished",
        track_id="42",
        total_played_seconds=123,
    )

    assert ok is True
    mock_client.rotor_station_feedback_track_finished.assert_awaited_once()
    _, kwargs = mock_client.rotor_station_feedback_track_finished.call_args
    assert kwargs["track_id"] == "42"
    assert kwargs["total_played_seconds"] == 123.0


async def test_rotor_feedback_skip_calls_typed_helper(
    client: KionMusicClient,
) -> None:
    """Skip dispatches to rotor_station_feedback_skip with played seconds."""
    mock_client = mock.AsyncMock()
    mock_client.rotor_station_feedback_skip = mock.AsyncMock(return_value=True)
    client._client = mock_client

    ok = await client.send_rotor_station_feedback(
        "user:onyourwave", "skip", track_id="42", total_played_seconds=10
    )

    assert ok is True
    mock_client.rotor_station_feedback_skip.assert_awaited_once()
    _, kwargs = mock_client.rotor_station_feedback_skip.call_args
    assert kwargs["track_id"] == "42"
    assert kwargs["total_played_seconds"] == 10.0


async def test_rotor_feedback_unknown_type_falls_back(
    client: KionMusicClient,
) -> None:
    """Unknown feedback types fall back to the generic rotor_station_feedback helper."""
    mock_client = mock.AsyncMock()
    mock_client.rotor_station_feedback = mock.AsyncMock(return_value=True)
    # Typed helpers must NOT be called for an unknown feedback type.
    mock_client.rotor_station_feedback_radio_started = mock.AsyncMock(return_value=True)
    mock_client.rotor_station_feedback_track_started = mock.AsyncMock(return_value=True)
    mock_client.rotor_station_feedback_track_finished = mock.AsyncMock(return_value=True)
    mock_client.rotor_station_feedback_skip = mock.AsyncMock(return_value=True)
    client._client = mock_client

    ok = await client.send_rotor_station_feedback("user:onyourwave", "like", track_id="42")

    assert ok is True
    mock_client.rotor_station_feedback.assert_awaited_once()
    mock_client.rotor_station_feedback_radio_started.assert_not_called()
    mock_client.rotor_station_feedback_track_started.assert_not_called()
    mock_client.rotor_station_feedback_track_finished.assert_not_called()
    mock_client.rotor_station_feedback_skip.assert_not_called()


# ─── get_track_file_info: params + sign construction ─────────────────────────


async def test_get_track_file_info_normalizes_codec_whitespace(
    client: KionMusicClient,
) -> None:
    """Whitespace around codec tokens is stripped from both params and sign string."""
    mock_client = mock.AsyncMock()
    mock_client.base_url = DEFAULT_BASE_URL
    mock_request = mock.AsyncMock()
    mock_request.get = mock.AsyncMock(return_value={"downloadInfo": None})
    mock_client._request = mock_request
    client._client = mock_client

    await client.get_track_file_info(
        "42",
        quality="lossless",
        codecs=" flac-mp4 , flac , aac-mp4 ",
        transport="raw",
    )

    mock_request.get.assert_awaited_once()
    _, kwargs = mock_request.get.call_args
    params = kwargs["params"]
    assert params["codecs"] == "flac-mp4,flac,aac-mp4"


async def test_get_track_file_info_builds_signed_params(
    client: KionMusicClient,
) -> None:
    """Sign string is ts+trackId+quality+codecs_no_commas+transport, b64(HMAC-SHA256)[:-1]."""
    mock_client = mock.AsyncMock()
    mock_client.base_url = DEFAULT_BASE_URL
    mock_request = mock.AsyncMock()
    mock_request.get = mock.AsyncMock(return_value={"downloadInfo": None})
    mock_client._request = mock_request
    client._client = mock_client

    await client.get_track_file_info(
        "42",
        quality="lossless",
        codecs="flac-mp4,flac",
        transport="encraw",
    )

    mock_request.get.assert_awaited_once()
    args, kwargs = mock_request.get.call_args
    assert args[0] == f"{DEFAULT_BASE_URL}/get-file-info"
    params = kwargs["params"]
    assert params["trackId"] == "42"
    assert params["quality"] == "lossless"
    assert params["codecs"] == "flac-mp4,flac"
    assert params["transports"] == "encraw"
    # Recompute expected sign from the emitted ts to verify the formula.
    ts = params["ts"]
    expected_sign_input = f"{ts}42losslessflac-mp4flacencraw".encode()
    expected_sign = base64.b64encode(
        hmac.new(DEFAULT_SIGN_KEY.encode(), expected_sign_input, hashlib.sha256).digest()
    ).decode()[:-1]
    assert params["sign"] == expected_sign
    # Kion API expects 43 chars (one trailing '=' stripped from b64 of SHA-256).
    assert len(params["sign"]) == 43
