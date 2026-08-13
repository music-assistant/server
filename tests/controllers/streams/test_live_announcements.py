"""Tests for the live announcements a client speaks into the webserver."""

from __future__ import annotations

import asyncio
import logging
import re
from contextlib import aclosing
from typing import TYPE_CHECKING, Any, NamedTuple
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import WSMsgType, web
from aiohttp.test_utils import TestClient, TestServer, make_mocked_request
from music_assistant_models.auth import User, UserRole
from music_assistant_models.enums import ContentType
from music_assistant_models.media_items import AudioFormat

from music_assistant.controllers.streams import live_announcements
from music_assistant.controllers.streams.live_announcements import (
    LIVE_ANNOUNCEMENT_ROUTE,
    LiveAnnouncementManager,
    LiveAnnouncementSession,
)
from music_assistant.helpers.audio import create_streaming_wave_header
from tests.common import use_real_create_task

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from aiohttp import ClientWebSocketResponse

PLAYER_ID = "player1"
BASE_URL = "http://ma.local:8097"
VALID_TOKEN = "valid-token"
SAMPLE_RATE = 16000
LIVE_FORMAT = AudioFormat(
    content_type=ContentType.PCM_S16LE,
    sample_rate=SAMPLE_RATE,
    bit_depth=16,
    channels=1,
)
# the close code a client that may not (or cannot) announce is disconnected with
REJECTED = 4001
# upper bound on anything the server is expected to answer right away
REPLY_TIMEOUT = 5


class Harness(NamedTuple):
    """A live announcement manager, its MusicAssistant stub and a client for its route."""

    manager: LiveAnnouncementManager
    mass: MagicMock
    client: TestClient[web.Request, web.Application]


@pytest.fixture(name="harness")
async def harness_fixture() -> AsyncGenerator[Harness]:
    """Yield a live announcement manager served on a real websocket route."""
    mass = MagicMock()
    mass.streams.base_url = BASE_URL
    mass.webserver.auth.authenticate_with_token = AsyncMock(
        return_value=User(user_id="user_1", username="listener", role=UserRole.USER)
    )
    mass.players.get_player = MagicMock(
        side_effect=lambda player_id: MagicMock() if player_id == PLAYER_ID else None
    )
    mass.players.play_announcement = AsyncMock()
    # the announcement is dispatched as a task, which must actually run
    use_real_create_task(mass)
    manager = LiveAnnouncementManager(mass, logging.getLogger("test.streams.live_announcements"))
    app = web.Application()
    app.router.add_get(LIVE_ANNOUNCEMENT_ROUTE, manager.handle_ws)
    client: TestClient[web.Request, web.Application] = TestClient(TestServer(app))
    await client.start_server()
    try:
        yield Harness(manager, mass, client)
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_read_replays_the_clip_and_waits_for_the_rest() -> None:
    """
    A reader attaching mid-sentence gets the clip from the start and then follows along.

    The renderer only starts pulling once its ffmpeg is up, by which time the client is
    already speaking, so the session is a buffer and not a queue.
    """
    session = _session()
    await session.write(b"first")

    stream = session.read()
    async with aclosing(stream):
        assert await anext(stream) == b"first"
        reader = asyncio.ensure_future(anext(stream))
        await asyncio.sleep(0)
        assert not reader.done()
        await session.write(b"second")
        assert await asyncio.wait_for(reader, timeout=REPLY_TIMEOUT) == b"second"

        # the clip only ends when the client says it is done
        end = asyncio.ensure_future(anext(stream))
        await asyncio.sleep(0)
        assert not end.done()
        await session.finish()
        with pytest.raises(StopAsyncIteration):
            await asyncio.wait_for(end, timeout=REPLY_TIMEOUT)

    # audio that arrives after the clip ended is not part of it
    await session.write(b"late")
    assert [chunk async for chunk in session.read()] == [b"first", b"second"]


@pytest.mark.asyncio
async def test_duration_follows_the_audio_that_arrived() -> None:
    """The duration is what has been spoken so far, in seconds of PCM."""
    session = _session()
    assert session.duration == 0

    await session.write(b"\x00" * LIVE_FORMAT.pcm_sample_size)
    assert session.duration == 1.0

    await session.write(b"\x00" * (LIVE_FORMAT.pcm_sample_size // 2))
    assert session.duration == 1.5


@pytest.mark.asyncio
async def test_an_unknown_session_is_not_served() -> None:
    """A session id that is not (or no longer) live has nothing to serve."""
    manager = LiveAnnouncementManager(MagicMock(), logging.getLogger("test.streams.live"))
    request, _ = _stream_request("ghost")

    with pytest.raises(web.HTTPNotFound):
        await manager.serve_stream(request)


@pytest.mark.asyncio
async def test_a_spoken_clip_is_announced_on_the_player(harness: Harness) -> None:
    """The start message starts the announcement and a text frame ends the clip."""
    ws = await _start_speaking(harness.client, pre_announce=True, volume_level=42)
    assert await _reply(ws) == "started"

    await ws.send_bytes(b"\x01\x02")
    await ws.send_str('{"type": "stop"}')
    assert await _reply(ws) == "finished"

    harness.mass.players.play_announcement.assert_awaited_once()
    call = harness.mass.players.play_announcement.call_args
    assert call.args == (PLAYER_ID,)
    assert call.kwargs["pre_announce"] is True
    assert call.kwargs["volume_level"] == 42
    assert re.fullmatch(rf"{re.escape(BASE_URL)}/live_announcement/[\w-]+\.wav", call.kwargs["url"])


@pytest.mark.asyncio
async def test_the_renderer_is_served_what_is_being_spoken(harness: Harness) -> None:
    """The announcement url delivers an open-ended wave header and then the frames."""
    served: list[bytes] = []

    async def _pull(_player_id: str, url: str, **_kwargs: Any) -> None:
        request, writer = _stream_request(_session_id(url))
        await harness.manager.serve_stream(request)
        served.append(_written(writer))

    harness.mass.players.play_announcement = AsyncMock(side_effect=_pull)

    ws = await _start_speaking(harness.client)
    assert await _reply(ws) == "started"
    await ws.send_bytes(b"\x01\x02")
    await ws.send_bytes(b"\x03\x04")
    await ws.send_str('{"type": "stop"}')
    assert await _reply(ws) == "finished"

    assert served == [create_streaming_wave_header(LIVE_FORMAT) + b"\x01\x02\x03\x04"]


@pytest.mark.asyncio
async def test_the_session_lives_as_long_as_the_announcement(harness: Harness) -> None:
    """The audio stays available for as long as the player is playing it."""
    playing = asyncio.Event()

    async def _play(*_args: Any, **_kwargs: Any) -> None:
        await playing.wait()

    harness.mass.players.play_announcement = AsyncMock(side_effect=_play)

    ws = await _start_speaking(harness.client)
    assert await _reply(ws) == "started"
    await ws.send_bytes(b"\x01\x02")
    await ws.send_str('{"type": "stop"}')

    # the player is still on the clip, so a renderer starting late is still served
    session_id = _session_id(harness.mass.players.play_announcement.call_args.kwargs["url"])
    assert harness.manager.active_sessions == 1
    request, writer = _stream_request(session_id)
    await harness.manager.serve_stream(request)
    assert _written(writer).endswith(b"\x01\x02")

    playing.set()
    assert await _reply(ws) == "finished"

    assert harness.manager.active_sessions == 0
    late_request, _ = _stream_request(session_id)
    with pytest.raises(web.HTTPNotFound):
        await harness.manager.serve_stream(late_request)


@pytest.mark.asyncio
async def test_the_announcement_outlives_a_client_that_drops(harness: Harness) -> None:
    """A client that disconnects mid-sentence still gets what it spoke played out."""
    playing = asyncio.Event()
    played = asyncio.Event()

    async def _play(*_args: Any, **_kwargs: Any) -> None:
        await playing.wait()
        played.set()

    harness.mass.players.play_announcement = AsyncMock(side_effect=_play)

    ws = await _start_speaking(harness.client)
    assert await _reply(ws) == "started"
    await ws.send_bytes(b"\x01\x02")
    await ws.close()

    assert not played.is_set()
    assert harness.manager.active_sessions == 1

    playing.set()
    await asyncio.wait_for(played.wait(), timeout=REPLY_TIMEOUT)


@pytest.mark.asyncio
async def test_a_client_that_goes_silent_ends_its_own_clip(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Silence ends the clip instead of holding the player until the client disconnects."""
    monkeypatch.setattr(live_announcements, "IDLE_TIMEOUT", 0.05)

    ws = await _start_speaking(harness.client)
    assert await _reply(ws) == "started"
    await ws.send_bytes(b"\x01\x02")

    assert await _reply(ws) == "finished"


@pytest.mark.asyncio
async def test_an_ingress_client_announces_without_an_auth_message(harness: Harness) -> None:
    """Home Assistant authenticates its own users, so ingress skips the auth message."""
    user = User(user_id="user_2", username="ha_user", role=UserRole.USER)
    with (
        patch.object(live_announcements, "is_request_from_ingress", return_value=True),
        patch.object(live_announcements, "get_authenticated_user", AsyncMock(return_value=user)),
    ):
        ws = await harness.client.ws_connect(LIVE_ANNOUNCEMENT_ROUTE)
        await ws.send_json({"type": "start", "player_id": PLAYER_ID, "sample_rate": SAMPLE_RATE})
        assert await _reply(ws) == "started"
        await ws.send_str('{"type": "stop"}')
        assert await _reply(ws) == "finished"

    harness.mass.webserver.auth.authenticate_with_token.assert_not_called()
    harness.mass.players.play_announcement.assert_awaited_once()


@pytest.mark.asyncio
async def test_an_auth_message_without_a_token_is_rejected(harness: Harness) -> None:
    """A client that presents no token never gets to announce."""
    ws = await harness.client.ws_connect(LIVE_ANNOUNCEMENT_ROUTE)
    await ws.send_json({"type": "auth"})

    assert await _close_code(ws) == REJECTED
    harness.mass.players.play_announcement.assert_not_called()


@pytest.mark.asyncio
async def test_an_unknown_token_is_rejected(harness: Harness) -> None:
    """A token that does not resolve to a user never gets to announce."""
    harness.mass.webserver.auth.authenticate_with_token = AsyncMock(return_value=None)
    ws = await harness.client.ws_connect(LIVE_ANNOUNCEMENT_ROUTE)
    await ws.send_json({"type": "auth", "token": "nope"})

    assert await _close_code(ws) == REJECTED
    harness.mass.players.play_announcement.assert_not_called()


@pytest.mark.asyncio
async def test_a_user_that_may_not_control_players_is_rejected(harness: Harness) -> None:
    """Announcing takes the players control scope, whatever else the token is valid for."""
    # a role id outside ROLE_SCOPES grants no scopes at all
    harness.mass.webserver.auth.authenticate_with_token = AsyncMock(
        return_value=User(user_id="user_3", username="kiosk", role="kiosk")
    )
    ws = await harness.client.ws_connect(LIVE_ANNOUNCEMENT_ROUTE)
    await ws.send_json({"type": "auth", "token": VALID_TOKEN})

    assert await _close_code(ws) == REJECTED
    harness.mass.players.play_announcement.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "start_message",
    [
        {"type": "start", "player_id": "ghost", "sample_rate": SAMPLE_RATE},
        {"type": "start", "player_id": PLAYER_ID, "sample_rate": 1000},
        {"type": "start", "player_id": PLAYER_ID, "sample_rate": SAMPLE_RATE, "channels": 3},
    ],
    ids=["unknown player", "sample rate out of range", "channel count out of range"],
)
async def test_an_unusable_start_message_is_rejected(
    harness: Harness, start_message: dict[str, object]
) -> None:
    """Nothing is announced when the client cannot say what to send where."""
    ws = await harness.client.ws_connect(LIVE_ANNOUNCEMENT_ROUTE)
    await ws.send_json({"type": "auth", "token": VALID_TOKEN})
    await ws.send_json(start_message)

    assert await _close_code(ws) == REJECTED
    harness.mass.players.play_announcement.assert_not_called()


def _session(session_id: str = "session1") -> LiveAnnouncementSession:
    """Return a session for the format the tests speak in."""
    return LiveAnnouncementSession(
        session_id, f"{BASE_URL}/live_announcement/{session_id}.wav", LIVE_FORMAT
    )


async def _start_speaking(
    client: TestClient[web.Request, web.Application], **start: object
) -> ClientWebSocketResponse:
    """
    Connect, authenticate and send the start message.

    :param client: Test client for the manager's websocket route.
    :param start: Extra fields to put in the start message.
    """
    ws = await client.ws_connect(LIVE_ANNOUNCEMENT_ROUTE)
    await ws.send_json({"type": "auth", "token": VALID_TOKEN})
    await ws.send_json(
        {"type": "start", "player_id": PLAYER_ID, "sample_rate": SAMPLE_RATE, **start}
    )
    return ws


async def _reply(ws: ClientWebSocketResponse) -> str:
    """Return the type of the next message the server sends."""
    message = await asyncio.wait_for(ws.receive_json(), timeout=REPLY_TIMEOUT)
    return str(message["type"])


async def _close_code(ws: ClientWebSocketResponse) -> int:
    """Return the code the server closed the connection with."""
    msg = await asyncio.wait_for(ws.receive(), timeout=REPLY_TIMEOUT)
    assert msg.type is WSMsgType.CLOSE
    return int(msg.data)


def _stream_request(session_id: str) -> tuple[web.Request, MagicMock]:
    """Return a request for the audio of the given session, plus the writer serving it."""
    writer = MagicMock()
    writer.write = AsyncMock()
    writer.write_headers = AsyncMock()
    writer.write_eof = AsyncMock()
    writer.drain = AsyncMock()
    request = make_mocked_request(
        "GET",
        f"/live_announcement/{session_id}.wav",
        match_info={"session_id": session_id},
        writer=writer,
    )
    return request, writer


def _written(writer: MagicMock) -> bytes:
    """Return the body that was written to a served stream."""
    return b"".join(call.args[0] for call in writer.write.call_args_list)


def _session_id(url: str) -> str:
    """Return the session id in a live announcement url."""
    return url.rsplit("/", maxsplit=1)[-1].removesuffix(".wav")
