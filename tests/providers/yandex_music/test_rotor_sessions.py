"""Rotor-session adapter checks using the real library and a mocked transport."""

from __future__ import annotations

import time
from collections.abc import Awaitable, Iterator
from typing import cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from music_assistant_models.errors import LoginFailed
from ya_passport_auth import SecretStr
from yandex_music import ClientAsync, Track
from yandex_music.exceptions import BadRequestError, NetworkError, UnauthorizedError

from music_assistant.providers.yandex_music.api_client import YandexMusicClient


@pytest.fixture
def rotor_client() -> Iterator[tuple[YandexMusicClient, ClientAsync, AsyncMock]]:
    """Use the library's actual models and serialization with no network I/O."""
    underlying = ClientAsync("fake_token")
    post = AsyncMock()
    underlying._request = MagicMock(post=post)
    client = YandexMusicClient(SecretStr("fake_token"))
    client._client = underlying
    client._user_id = 12345
    for kind in client._throttlers:
        client._throttlers[kind] = AsyncMock()
    with patch.object(underlying, "tracks", AsyncMock(return_value=[Track(id=101), Track(id=100)])):
        yield client, underlying, post


@pytest.mark.parametrize("method", ["new", "tracks", "feedback"])
async def test_rotor_uses_public_library_methods(
    rotor_client: tuple[YandexMusicClient, ClientAsync, AsyncMock], method: str
) -> None:
    """The adapter delegates transport and model parsing to the library."""
    client, underlying, post = rotor_client
    post.return_value = {"radioSessionId": "session", "batchId": "batch", "sequence": []}
    name = f"rotor_session_{method}"
    with patch.object(underlying, name, wraps=getattr(underlying, name)) as call:
        if method == "new":
            assert await client.rotor_session_new("user:onyourwave") == ("session", [], "batch")
        elif method == "tracks":
            assert await client.rotor_session_tracks("session", current_track_id="100") == (
                [],
                "batch",
            )
        else:
            assert await client.rotor_session_feedback("session", "trackStarted", track_id="100")
        call.assert_awaited_once()


async def test_rotor_creation_preserves_flags_settings_and_track_order(
    rotor_client: tuple[YandexMusicClient, ClientAsync, AsyncMock],
) -> None:
    """Hydrated tracks follow the session sequence, including omitted adverts."""
    client, underlying, post = rotor_client
    post.return_value = {
        "radioSessionId": "session",
        "batchId": "batch",
        "sequence": [
            {"type": "track", "track": {"id": 100}, "liked": False},
            {"type": "ad", "track": None, "liked": False},
            {"type": "track", "track": {"id": 101}, "liked": True},
        ],
    }
    session, tracks, batch = await client.rotor_session_new(
        "user:onyourwave",
        settings={"diversity": "discover", "moodEnergy": "calm", "language": "russian"},
        queue=["99"],
    )
    assert (session, batch) == ("session", "batch")
    assert [track.id for track in tracks] == [100, 101]
    cast("AsyncMock", underlying.tracks).assert_awaited_once_with(["100", "101"])
    post.assert_awaited_once_with(
        "https://api.music.yandex.net/rotor/session/new",
        json={
            "seeds": [
                "user:onyourwave",
                "settingDiversity:discover",
                "settingMoodEnergy:calm",
                "settingLanguage:russian",
            ],
            "queue": ["99"],
            "includeTracksInResponse": True,
            "includeWaveModel": True,
            "interactive": True,
        },
    )


async def test_rotor_hydration_skips_tracks_without_ids(
    rotor_client: tuple[YandexMusicClient, ClientAsync, AsyncMock],
) -> None:
    """An unavailable sequence item must not poison hydration of valid tracks."""
    client, underlying, post = rotor_client
    post.return_value = {
        "radioSessionId": "session",
        "batchId": "batch",
        "sequence": [
            {"type": "track", "track": {"id": 100}, "liked": False},
            {"type": "track", "track": {"id": None}, "liked": False},
            {"type": "track", "track": {"id": 101}, "liked": False},
        ],
    }
    _, tracks, _ = await client.rotor_session_new("user:onyourwave")
    cast("AsyncMock", underlying.tracks).assert_awaited_once_with(["100", "101"])
    assert [track.id for track in tracks] == [100, 101]


@pytest.mark.parametrize(
    "event_type", ["radioStarted", "trackStarted", "trackFinished", "skip", "like", "dislike"]
)
async def test_rotor_feedback_wire_fields(
    rotor_client: tuple[YandexMusicClient, ClientAsync, AsyncMock], event_type: str
) -> None:
    """Feedback uses the library's event schema and batch anchor."""
    client, _, post = rotor_client
    post.return_value = {"result": "ok"}
    assert await client.rotor_session_feedback(
        "session", event_type, track_id="100", total_played_seconds=12, batch_id="batch"
    )
    assert post.await_args is not None
    assert post.await_args.args == ("https://api.music.yandex.net/rotor/session/session/feedback",)
    body = post.await_args.kwargs["json"]
    event = body["event"]
    assert body["batchId"] == "batch"
    assert event["type"] == event_type
    assert event["timestamp"].endswith("Z")
    if event_type == "radioStarted":
        assert body["from"] == "100"
        assert event.get("trackId") is None
        assert "from" not in event
    else:
        assert event["trackId"] == "100"
        assert body.get("from") is None
    if event_type in ("trackFinished", "skip"):
        assert event["totalPlayedSeconds"] == 12
    else:
        assert event.get("totalPlayedSeconds") is None


@pytest.mark.parametrize("method", ["new", "tracks", "feedback"])
async def test_rotor_unauthorized_prompts_reauthentication(
    rotor_client: tuple[YandexMusicClient, ClientAsync, AsyncMock], method: str
) -> None:
    """All session operations map rejected credentials to MA's login error."""
    client, _, post = rotor_client
    post.side_effect = UnauthorizedError("stale token")
    operation: Awaitable[object]
    if method == "new":
        operation = client.rotor_session_new("user:onyourwave")
    elif method == "tracks":
        operation = client.rotor_session_tracks("session", current_track_id="100")
    else:
        operation = client.rotor_session_feedback("session", "trackStarted", track_id="100")
    with pytest.raises(LoginFailed):
        await operation
    assert post.await_count == 1


@pytest.mark.parametrize("error", [BadRequestError("rejected"), NetworkError("temporary")])
async def test_rotor_feedback_drops_errors_without_retry(
    rotor_client: tuple[YandexMusicClient, ClientAsync, AsyncMock], error: Exception
) -> None:
    """Fire-and-forget feedback never reconnects and repeats a failed event."""
    client, _, post = rotor_client
    post.side_effect = error
    with patch.object(client, "_reconnect", new_callable=AsyncMock) as reconnect:
        assert not await client.rotor_session_feedback("session", "trackStarted", track_id="100")
        reconnect.assert_not_awaited()
    assert post.await_count == 1


async def test_rotor_feedback_captcha_quarantines_only_rotor(
    rotor_client: tuple[YandexMusicClient, ClientAsync, AsyncMock],
) -> None:
    """The new library path retains captcha handling in the provider's throttle."""
    client, _, post = rotor_client
    post.side_effect = NetworkError("<html>smart-captcha about-429.html</html>")
    assert not await client.rotor_session_feedback("session", "trackStarted", track_id="100")
    assert client._block_until["rotor"] > time.monotonic()
    assert client._block_until["default"] == 0
    assert client._block_until["file_info"] == 0
    assert post.await_count == 1


@pytest.mark.parametrize("method", ["new", "tracks"])
async def test_rotor_fetch_retries_connection_drop_with_another_throttle_slot(
    rotor_client: tuple[YandexMusicClient, ClientAsync, AsyncMock], method: str
) -> None:
    """Library-backed fetches preserve the reconnect and rate-limited retry path."""
    client, underlying, post = rotor_client
    post.side_effect = [
        NetworkError("Connection reset by peer"),
        {"radioSessionId": "session", "batchId": "batch", "sequence": []},
    ]

    async def reconnect() -> None:
        client._client = underlying

    with patch.object(client, "_reconnect", side_effect=reconnect) as reconnect_call:
        if method == "new":
            assert await client.rotor_session_new("user:onyourwave") == ("session", [], "batch")
        else:
            assert await client.rotor_session_tracks("session", current_track_id="100") == (
                [],
                "batch",
            )
        reconnect_call.assert_awaited_once()
    assert post.await_count == 2
    assert cast("AsyncMock", client._throttlers["rotor"].acquire).await_count == 2
