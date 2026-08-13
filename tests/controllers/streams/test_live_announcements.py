"""Tests for the live announcements a client speaks into the webserver."""

from __future__ import annotations

import asyncio
import logging
import re
from contextlib import aclosing
from types import MethodType
from typing import TYPE_CHECKING, Any, NamedTuple
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import WSMsgType, web
from aiohttp.test_utils import TestClient, TestServer, make_mocked_request
from music_assistant_models.auth import User, UserRole
from music_assistant_models.enums import ContentType
from music_assistant_models.media_items import AudioFormat

from music_assistant.constants import HOMEASSISTANT_SYSTEM_USER
from music_assistant.controllers.players.controller import PlayerController
from music_assistant.controllers.players.helpers import handle_player_command
from music_assistant.controllers.streams import live_announcements
from music_assistant.controllers.streams.live_announcements import (
    LIVE_ANNOUNCEMENT_ROUTE,
    MAX_CLOSE_REASON_BYTES,
    LiveAnnouncementManager,
    LiveAnnouncementSession,
)
from music_assistant.controllers.webserver.helpers.auth_middleware import get_current_user
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
    player: MagicMock


class BlockedAnnouncement(NamedTuple):
    """An announcement that reports its progress and plays for as long as the test wants."""

    started: asyncio.Event
    release: asyncio.Event
    done: asyncio.Event


@pytest.fixture(name="harness")
async def harness_fixture() -> AsyncGenerator[Harness]:
    """Yield a live announcement manager served on a real websocket route."""
    mass = MagicMock()
    mass.streams.base_url = BASE_URL
    mass.webserver.auth.authenticate_with_token = AsyncMock(
        return_value=User(user_id="user_1", username="listener", role=UserRole.USER)
    )
    player = _player()
    mass.players.get_player = MagicMock(
        side_effect=lambda player_id: player if player_id == PLAYER_ID else None
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
        yield Harness(manager, mass, client, player)
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_read_replays_the_clip_from_the_start() -> None:
    """
    A reader gets the whole clip from the start and follows the audio still coming in.

    The clip is replayed rather than consumed, so it can be served again whenever the
    renderer comes back for it.
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

    # the whole clip is served again, without the audio that arrived after it ended
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
    """A text frame completes the clip, which is then announced on the chosen player."""
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
async def test_the_renderer_is_served_the_finished_clip(harness: Harness) -> None:
    """The announcement url delivers a wave header followed by the clip that was spoken."""
    served = _serve_the_clip_when_announced(harness)

    ws = await _start_speaking(harness.client)
    assert await _reply(ws) == "started"
    await ws.send_bytes(b"\x01\x02")
    await ws.send_bytes(b"\x03\x04")
    await ws.send_str('{"type": "stop"}')
    assert await _reply(ws) == "finished"

    assert served == [[create_streaming_wave_header(LIVE_FORMAT), b"\x01\x02", b"\x03\x04"]]


@pytest.mark.asyncio
async def test_the_session_lives_as_long_as_the_announcement(harness: Harness) -> None:
    """The audio stays available for as long as the player is playing it."""
    blocked = _block_announcement(harness)

    ws = await _start_speaking(harness.client)
    assert await _reply(ws) == "started"
    await ws.send_bytes(b"\x01\x02")
    await ws.send_str('{"type": "stop"}')
    await _wait(blocked.started)

    # the player is on the clip, so it must stay available to be (re)fetched
    session_id = _session_id(harness.mass.players.play_announcement.call_args.kwargs["url"])
    assert harness.manager.active_sessions == 1
    request, writer = _stream_request(session_id)
    await harness.manager.serve_stream(request)
    assert _written(writer) == [create_streaming_wave_header(LIVE_FORMAT), b"\x01\x02"]

    blocked.release.set()
    assert await _reply(ws) == "finished"

    assert harness.manager.active_sessions == 0
    late_request, _ = _stream_request(session_id)
    with pytest.raises(web.HTTPNotFound):
        await harness.manager.serve_stream(late_request)


@pytest.mark.asyncio
async def test_the_announcement_outlives_a_client_that_drops(harness: Harness) -> None:
    """A client that disconnects mid-sentence still gets what it spoke played out."""
    blocked = _block_announcement(harness)

    ws = await _start_speaking(harness.client)
    assert await _reply(ws) == "started"
    await ws.send_bytes(b"\x01\x02")
    await ws.close()

    # the clip is announced although there is no longer anyone to report back to
    await _wait(blocked.started)
    assert harness.manager.active_sessions == 1
    blocked.release.set()
    await _wait(blocked.done)


@pytest.mark.asyncio
async def test_a_player_that_never_finishes_is_not_waited_on_forever(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A player that never reports back ends as an error instead of holding the session."""
    monkeypatch.setattr(live_announcements, "ANNOUNCEMENT_TIMEOUT", 0.1)
    # never released: this stands in for a provider that hands off and never returns
    _block_announcement(harness)

    ws = await _start_speaking(harness.client)
    assert await _reply(ws) == "started"
    await ws.send_bytes(b"\x01\x02")
    await ws.send_str('{"type": "stop"}')

    assert await _reply(ws) == "error"
    assert harness.manager.active_sessions == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("player_filter", [[], [PLAYER_ID]], ids=["no filter", "player allowed"])
async def test_the_announcement_runs_as_the_user_that_spoke_it(
    harness: Harness, player_filter: list[str]
) -> None:
    """
    The user of the connection reaches the task the announcement is dispatched on.

    Player commands apply the user's restrictions from that context, so without it
    every live announcement would run unrestricted.
    """
    user = User(
        user_id="user_4", username="speaker", role=UserRole.USER, player_filter=player_filter
    )
    harness.mass.webserver.auth.authenticate_with_token = AsyncMock(return_value=user)
    announced = _announce_through_player_commands(harness)

    ws = await _start_speaking(harness.client)
    assert await _reply(ws) == "started"
    await ws.send_bytes(b"\x01\x02")
    await ws.send_str('{"type": "stop"}')
    assert await _reply(ws) == "finished"

    assert announced == [(PLAYER_ID, user)]


@pytest.mark.asyncio
async def test_a_user_without_access_to_the_player_is_refused(harness: Harness) -> None:
    """A player outside the user's filter is refused, as it is for any other command."""
    harness.mass.webserver.auth.authenticate_with_token = AsyncMock(
        return_value=User(
            user_id="user_5", username="guest", role=UserRole.USER, player_filter=["other_player"]
        )
    )
    announced = _announce_through_player_commands(harness)

    ws = await _start_speaking(harness.client)
    assert await _reply(ws) == "started"
    await ws.send_bytes(b"\x01\x02")
    await ws.send_str('{"type": "stop"}')
    assert await _reply(ws) == "error"

    assert announced == []


@pytest.mark.asyncio
async def test_a_client_that_goes_silent_ends_its_own_clip(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Silence ends the clip on its own, so a client that stops sending is not left open."""
    monkeypatch.setattr(live_announcements, "IDLE_TIMEOUT", 0.05)

    ws = await _start_speaking(harness.client)
    assert await _reply(ws) == "started"
    await ws.send_bytes(b"\x01\x02")

    assert await _reply(ws) == "finished"
    harness.mass.players.play_announcement.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_clip_that_keeps_going_is_cut_off(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A client that keeps sending cannot keep its session open indefinitely."""
    # with the idle timeout out of reach, only the wall clock bound can end this clip
    monkeypatch.setattr(live_announcements, "MAX_SESSION_SECONDS", 0.1)
    monkeypatch.setattr(live_announcements, "IDLE_TIMEOUT", 30)

    ws = await _start_speaking(harness.client)
    assert await _reply(ws) == "started"
    await ws.send_bytes(b"\x01\x02")

    assert await _reply(ws) == "finished"
    harness.mass.players.play_announcement.assert_awaited_once()


@pytest.mark.asyncio
async def test_only_so_many_clips_can_be_in_progress_at_once(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The audio buffered at the same time is bounded by a cap on the sessions."""
    monkeypatch.setattr(live_announcements, "MAX_CONCURRENT_SESSIONS", 1)

    speaking = await _start_speaking(harness.client)
    assert await _reply(speaking) == "started"
    await speaking.send_bytes(b"\x01\x02")

    rejected = await _start_speaking(harness.client)
    assert await _close_code(rejected) == REJECTED

    await speaking.send_str('{"type": "stop"}')
    assert await _reply(speaking) == "finished"
    assert harness.mass.players.play_announcement.await_count == 1


@pytest.mark.asyncio
async def test_empty_frames_are_not_part_of_the_clip(harness: Harness) -> None:
    """A frame without audio must not grow the clip that is held in memory."""
    served = _serve_the_clip_when_announced(harness)

    ws = await _start_speaking(harness.client)
    assert await _reply(ws) == "started"
    await ws.send_bytes(b"")
    await ws.send_bytes(b"\x01\x02")
    await ws.send_bytes(b"")
    await ws.send_str('{"type": "stop"}')
    assert await _reply(ws) == "finished"

    assert served == [[create_streaming_wave_header(LIVE_FORMAT), b"\x01\x02"]]


@pytest.mark.asyncio
@pytest.mark.parametrize("frames", [[], [b"", b""]], ids=["no audio at all", "only empty frames"])
async def test_a_clip_without_audio_is_not_announced(harness: Harness, frames: list[bytes]) -> None:
    """
    A clip nobody spoke into is dropped instead of played.

    Announcing silence would interrupt whatever the player is doing for nothing.
    """
    ws = await _start_speaking(harness.client)
    assert await _reply(ws) == "started"
    for frame in frames:
        await ws.send_bytes(frame)
    await ws.send_str('{"type": "stop"}')

    assert await _reply(ws) == "finished"
    harness.mass.players.play_announcement.assert_not_called()
    assert harness.manager.active_sessions == 0


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
        await ws.send_bytes(b"\x01\x02")
        await ws.send_str('{"type": "stop"}')
        assert await _reply(ws) == "finished"

    harness.mass.webserver.auth.authenticate_with_token.assert_not_called()
    harness.mass.players.play_announcement.assert_awaited_once()


@pytest.mark.asyncio
async def test_the_home_assistant_system_user_announces_over_ingress(harness: Harness) -> None:
    """Home Assistant announces as its own system user, which ingress is the home of."""
    user = User(user_id="user_6", username=HOMEASSISTANT_SYSTEM_USER, role=UserRole.SERVICE)
    with (
        patch.object(live_announcements, "is_request_from_ingress", return_value=True),
        patch.object(live_announcements, "get_authenticated_user", AsyncMock(return_value=user)),
    ):
        ws = await harness.client.ws_connect(LIVE_ANNOUNCEMENT_ROUTE)
        await ws.send_json({"type": "start", "player_id": PLAYER_ID, "sample_rate": SAMPLE_RATE})
        assert await _reply(ws) == "started"
        await ws.send_bytes(b"\x01\x02")
        await ws.send_str('{"type": "stop"}')
        assert await _reply(ws) == "finished"

    harness.mass.players.play_announcement.assert_awaited_once()


@pytest.mark.asyncio
async def test_the_home_assistant_system_user_is_rejected_off_ingress(harness: Harness) -> None:
    """The Home Assistant system token is not accepted on the regular webserver."""
    harness.mass.webserver.auth.authenticate_with_token = AsyncMock(
        return_value=User(
            user_id="user_7", username=HOMEASSISTANT_SYSTEM_USER, role=UserRole.SERVICE
        )
    )
    ws = await harness.client.ws_connect(LIVE_ANNOUNCEMENT_ROUTE)
    await ws.send_json({"type": "auth", "token": VALID_TOKEN})

    assert await _close_code(ws) == REJECTED
    harness.mass.players.play_announcement.assert_not_called()


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
async def test_an_unavailable_player_is_rejected(harness: Harness) -> None:
    """
    A player that is offline is refused up front.

    The command for such a player is silently dropped downstream, which would leave
    the client believing its announcement played.
    """
    harness.player.available = False

    ws = await _start_speaking(harness.client)

    assert await _close_code(ws) == REJECTED
    harness.mass.players.play_announcement.assert_not_called()


@pytest.mark.asyncio
async def test_a_long_rejection_reason_still_reaches_the_client(harness: Harness) -> None:
    """The reason quotes what the client sent, which must still fit in a close frame."""
    ws = await harness.client.ws_connect(LIVE_ANNOUNCEMENT_ROUTE)
    await ws.send_json({"type": "auth", "token": VALID_TOKEN})
    await ws.send_json({"type": "start", "player_id": "x" * 500, "sample_rate": SAMPLE_RATE})

    msg = await asyncio.wait_for(ws.receive(), timeout=REPLY_TIMEOUT)
    assert msg.type is WSMsgType.CLOSE
    assert msg.data == REJECTED
    assert msg.extra is not None
    assert msg.extra.startswith("Unknown or unavailable player")
    assert len(msg.extra.encode()) <= MAX_CLOSE_REASON_BYTES


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


def _player() -> MagicMock:
    """Return the player the tests announce on."""
    player = MagicMock()
    player.player_id = PLAYER_ID
    player.display_name = "Kitchen"
    player.available = True
    player.protocol_parent_id = None
    return player


def _announce_through_player_commands(harness: Harness) -> list[tuple[str, User | None]]:
    """
    Announce through the guards every player command passes, instead of a bare mock.

    :param harness: The harness whose announcements should run through the guards.
    :return: The player and the user of every announcement that got through.
    """
    controller = PlayerController.__new__(PlayerController)
    controller._players = {PLAYER_ID: harness.player}
    controller.logger = logging.getLogger("test.streams.live_announcements.players")
    announced: list[tuple[str, User | None]] = []

    async def _play(_self: PlayerController, player_id: str, **_kwargs: Any) -> None:
        announced.append((player_id, get_current_user()))

    harness.mass.players.play_announcement = MethodType(handle_player_command(_play), controller)
    return announced


def _block_announcement(harness: Harness) -> BlockedAnnouncement:
    """
    Keep an announcement playing until the test releases it.

    :param harness: The harness whose announcements should block.
    :return: The events reporting the announcement and releasing it again.
    """
    blocked = BlockedAnnouncement(asyncio.Event(), asyncio.Event(), asyncio.Event())

    async def _play(*_args: Any, **_kwargs: Any) -> None:
        blocked.started.set()
        await blocked.release.wait()
        blocked.done.set()

    harness.mass.players.play_announcement = AsyncMock(side_effect=_play)
    return blocked


def _serve_the_clip_when_announced(harness: Harness) -> list[list[bytes]]:
    """
    Fetch the announcement url the way the renderer does, whenever one is announced.

    :param harness: The harness whose announcements should be fetched.
    :return: What was written to the stream for each of them.
    """
    served: list[list[bytes]] = []

    async def _pull(_player_id: str, url: str, **_kwargs: Any) -> None:
        request, writer = _stream_request(_session_id(url))
        await harness.manager.serve_stream(request)
        served.append(_written(writer))

    harness.mass.players.play_announcement = AsyncMock(side_effect=_pull)
    return served


async def _wait(event: asyncio.Event) -> None:
    """Wait for something the server is expected to do right away."""
    await asyncio.wait_for(event.wait(), timeout=REPLY_TIMEOUT)


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


def _written(writer: MagicMock) -> list[bytes]:
    """Return the chunks that were written to a served stream, header first."""
    return [call.args[0] for call in writer.write.call_args_list]


def _session_id(url: str) -> str:
    """Return the session id in a live announcement url."""
    return url.rsplit("/", maxsplit=1)[-1].removesuffix(".wav")
