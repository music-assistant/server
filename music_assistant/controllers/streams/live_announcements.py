"""
Live announcements: speech captured by a client and played on a player once it is spoken.

A client holds an authenticated WebSocket on the MA webserver and pushes raw PCM frames
for as long as the user speaks. Those frames land in a LiveAnnouncementSession, which
serves them as a WAV url on the stream server. Once the clip is complete it is announced
like any other: the announcement renderer pulls that url the same way it pulls a TTS
clip, so every player plays it exactly as it plays the announcements it already knows.

Wire format on the inbound WebSocket:
- text {"type":"auth","token":...} - first message, omitted for Ingress connections
- text {"type":"start","player_id":...,"sample_rate":...,"channels":...,
        "pre_announce":...,"volume_level":...}
- text {"type":"started"} back, after which audio is accepted
- binary frames of raw little-endian signed 16-bit PCM in the announced format
- text (any) to end the clip, or simply close the connection
- text {"type":"finished"} or {"type":"error","message":...} back once it has played

A rejected client is closed with code 4001 and the reason in the close frame.
"""

from __future__ import annotations

import asyncio
import secrets
from contextlib import aclosing, suppress
from typing import TYPE_CHECKING, NamedTuple

from aiohttp import WSMsgType, web
from music_assistant_models.auth import Scope
from music_assistant_models.enums import ContentType
from music_assistant_models.errors import MusicAssistantError, PlayerCommandFailed
from music_assistant_models.media_items import AudioFormat
from orjson import dumps

from music_assistant.constants import HOMEASSISTANT_SYSTEM_USER
from music_assistant.controllers.webserver.helpers.auth_middleware import (
    get_authenticated_user,
    has_scope,
    is_request_from_ingress,
    set_current_user,
)
from music_assistant.helpers.audio import create_streaming_wave_header
from music_assistant.helpers.json import JSON_DECODE_EXCEPTIONS, json_loads

from .announcements import MAX_ANNOUNCEMENT_SECONDS, MAX_CLIP_SECONDS

if TYPE_CHECKING:
    import logging
    from collections.abc import AsyncGenerator, Callable

    from music_assistant.mass import MusicAssistant

# The client connects here to push its audio; the announcement itself is served from
# the stream server, whose route only ever carries the session id.
LIVE_ANNOUNCEMENT_ROUTE = "/live_announcement"
LIVE_ANNOUNCEMENT_STREAM_PATH = "/live_announcement/{session_id}.wav"

# Live audio is always uncompressed 16-bit PCM; only the rate and channel count vary,
# and the client reports both in its start message. The clip is held in memory while it
# is spoken, so the accepted range stops at what a capture device plausibly delivers -
# anything beyond it is only a bigger buffer, since announcements render to 44.1kHz.
LIVE_ANNOUNCEMENT_BIT_DEPTH = 16
MIN_SAMPLE_RATE = 8000
MAX_SAMPLE_RATE = 96000
MAX_CHANNELS = 2

# A client that stops sending without saying so holds the player's playback lock for as
# long as it stays connected, so silence ends the clip on its own. The idle timeout only
# measures the gap between frames, so a clip is bounded in wall clock time as well -
# otherwise a trickle of frames could hold the player forever.
IDLE_TIMEOUT = 10
MAX_SESSION_SECONDS = MAX_ANNOUNCEMENT_SECONDS
# Maximum time to wait for the handshake messages that precede the audio.
HANDSHAKE_TIMEOUT = 10
# Live announcements are spoken one at a time by a person, so a handful of sessions is
# already generous; the cap bounds the audio that can be buffered at once.
MAX_CONCURRENT_SESSIONS = 4
# Room for the whole clip to play out and the player to be restored. A player provider
# that never returns would otherwise hold one of the session slots until a restart.
ANNOUNCEMENT_TIMEOUT = MAX_CLIP_SECONDS + 60
# A close frame carries 125 bytes, two of which are taken by the status code.
MAX_CLOSE_REASON_BYTES = 123


class LiveAnnouncementStart(NamedTuple):
    """The validated contents of a client's start message."""

    player_id: str
    audio_format: AudioFormat
    pre_announce: bool | None
    volume_level: int | None


class LiveAnnouncementSession:
    """
    The audio of a single live announcement, buffered while it is being spoken.

    The whole clip is kept until the session is dropped, so the renderer - which only
    starts pulling once the clip is complete - can read it from the start, and can do so
    again if it is served more than once.
    """

    def __init__(self, session_id: str, url: str, audio_format: AudioFormat) -> None:
        """
        Initialize the session.

        :param session_id: The id that identifies this session in its stream url.
        :param url: The url the announcement audio is served on.
        :param audio_format: The format of the PCM frames the client sends.
        """
        self.session_id = session_id
        self.url = url
        self.audio_format = audio_format
        self._chunks: list[bytes] = []
        self._total_bytes = 0
        self._finished = False
        self._audio_added = asyncio.Condition()

    @property
    def duration(self) -> float:
        """Return the duration (in seconds) of the audio received so far."""
        return self._total_bytes / self.audio_format.pcm_sample_size

    async def write(self, chunk: bytes) -> None:
        """
        Add a chunk of PCM audio to the clip.

        :param chunk: Raw PCM audio in this session's format.
        """
        async with self._audio_added:
            if self._finished:
                return
            self._chunks.append(chunk)
            self._total_bytes += len(chunk)
            self._audio_added.notify_all()

    async def finish(self) -> None:
        """End the clip, releasing any reader waiting for more audio."""
        async with self._audio_added:
            self._finished = True
            self._audio_added.notify_all()

    async def read(self) -> AsyncGenerator[bytes]:
        """Yield the clip from the start, waiting for audio that is still being spoken."""
        index = 0
        while True:
            async with self._audio_added:
                while index >= len(self._chunks):
                    if self._finished:
                        return
                    await self._audio_added.wait()
                chunk = self._chunks[index]
            index += 1
            # yielded outside the lock, so a slow reader never blocks the client
            yield chunk


class LiveAnnouncementManager:
    """Owner of the live announcements that are currently being spoken."""

    def __init__(self, mass: MusicAssistant, logger: logging.Logger) -> None:
        """
        Initialize the manager.

        :param mass: The MusicAssistant instance.
        :param logger: Logger of the streams controller this manager belongs to.
        """
        self.mass = mass
        self.logger = logger.getChild("live_announcements")
        self._sessions: dict[str, LiveAnnouncementSession] = {}
        self._connections: set[web.WebSocketResponse] = set()
        self._unregister: Callable[[], None] | None = None
        self._closing = False

    @property
    def active_sessions(self) -> int:
        """Return the number of live announcements currently in progress."""
        return len(self._sessions)

    def setup(self) -> None:
        """Register the inbound audio route on the MA webserver."""
        # a reload closes and sets this manager up again, so the shutdown flag is
        # cleared here - it would otherwise silence every clip until a restart
        self._closing = False
        # this route rides on the webserver instead of the stream server because it is
        # the only one of the two that is authenticated (and reachable over https).
        self._unregister = self.mass.webserver.register_dynamic_route(
            LIVE_ANNOUNCEMENT_ROUTE, self.handle_ws, "GET"
        )

    async def close(self) -> None:
        """Unregister the route and disconnect any client that is still speaking."""
        # closing a connection reads as a client that stopped speaking, so without this
        # a recording in progress would announce itself on a server that is going down
        self._closing = True
        if self._unregister is not None:
            self._unregister()
            self._unregister = None
        for ws in list(self._connections):
            with suppress(Exception):
                await ws.close()

    async def serve_stream(self, request: web.Request) -> web.StreamResponse:
        """Serve the audio of a live announcement to the announcement renderer."""
        session_id = request.match_info["session_id"]
        if (session := self._sessions.get(session_id)) is None:
            raise web.HTTPNotFound(reason=f"Unknown live announcement: {session_id}")
        resp = web.StreamResponse(status=200, reason="OK")
        resp.content_type = "audio/wav"
        resp.enable_chunked_encoding()
        await resp.prepare(request)
        # the header declares an open-ended length: how long the user will speak is
        # not known when the first bytes go out
        await resp.write(create_streaming_wave_header(session.audio_format))
        # aclosing releases the reader immediately when the renderer goes away, instead
        # of leaving it parked on the session until garbage collection finalizes it
        audio = session.read()
        async with aclosing(audio):
            async for chunk in audio:
                try:
                    await resp.write(chunk)
                except ConnectionResetError, BrokenPipeError:
                    break
        return resp

    async def handle_ws(self, request: web.Request) -> web.WebSocketResponse:
        """Serve one client connection: receive the spoken audio and announce it."""
        ws = web.WebSocketResponse(heartbeat=25)
        await ws.prepare(request)
        self._connections.add(ws)
        try:
            if not await self._authenticate(request, ws):
                return ws
            if (start := await self._read_start_message(request, ws)) is None:
                return ws
            if len(self._sessions) >= MAX_CONCURRENT_SESSIONS:
                await self._reject(request, ws, "Too many live announcements in progress")
                return ws
            await self._run_session(ws, start)
        finally:
            self._connections.discard(ws)
            if not ws.closed:
                await ws.close()
        return ws

    async def _run_session(self, ws: web.WebSocketResponse, start: LiveAnnouncementStart) -> None:
        """Announce what the client speaks, from the start message up to the stop."""
        session = self._create_session(start.audio_format)
        try:
            await self._send(ws, {"type": "started"})
            await self._receive_audio(ws, session)
        finally:
            await session.finish()
        if not session.duration or self._closing:
            # nothing was spoken (a mis-tap, or a client that never sent audio): announcing
            # it would interrupt whatever the player is doing to play silence. the same
            # applies while shutting down, where the clip was cut short by us.
            self._sessions.pop(session.session_id, None)
            await self._send(ws, {"type": "finished"})
            return
        # only now the clip is complete: players that announce natively need it whole up
        # front - AirPlay renders it to a file and schedules one synchronized instant for
        # every group member from its exact duration, which a growing clip cannot give.
        # Its own task, so a client that drops still gets what it spoke played out, and
        # it owns the session, dropping it once the audio has been consumed.
        announcement = self.mass.create_task(
            self._play_live_announcement(
                session,
                start.player_id,
                pre_announce=start.pre_announce,
                volume_level=start.volume_level,
            )
        )
        if ws.closed:
            # there is no longer anyone to report the outcome to, so release the
            # connection handler and let the announcement play itself out
            return
        try:
            await announcement
        except MusicAssistantError as err:
            # a typed error carries a message meant for the person who spoke
            self.logger.warning("Live announcement to player %s failed: %s", start.player_id, err)
            await self._send(ws, {"type": "error", "message": str(err)})
            return
        except Exception:
            # anything else is a defect rather than a failed announcement, so it is logged
            # with its traceback and reported without leaking its internals
            self.logger.exception("Live announcement to player %s failed", start.player_id)
            await self._send(ws, {"type": "error", "message": "The announcement failed."})
            return
        await self._send(ws, {"type": "finished"})

    async def _receive_audio(
        self, ws: web.WebSocketResponse, session: LiveAnnouncementSession
    ) -> None:
        """Buffer the audio frames the client sends until it stops speaking."""
        try:
            async with asyncio.timeout(MAX_SESSION_SECONDS):
                await self._read_frames(ws, session)
        except TimeoutError:
            self.logger.warning(
                "Live announcement %s ended: it ran for the maximum of %s seconds",
                session.session_id,
                MAX_SESSION_SECONDS,
            )

    async def _read_frames(
        self, ws: web.WebSocketResponse, session: LiveAnnouncementSession
    ) -> None:
        """Read audio frames until the client stops speaking or falls silent."""
        while True:
            try:
                async with asyncio.timeout(IDLE_TIMEOUT):
                    msg = await ws.receive()
            except TimeoutError:
                self.logger.warning(
                    "Live announcement %s ended: no audio for %s seconds",
                    session.session_id,
                    IDLE_TIMEOUT,
                )
                return
            if msg.type != WSMsgType.BINARY:
                # any text frame is the client saying it is done; anything else is the
                # connection going away, which means the same thing
                return
            if not msg.data:
                # carries no audio, so it must not grow the clip that is held in memory
                continue
            await session.write(msg.data)
            if session.duration >= MAX_ANNOUNCEMENT_SECONDS:
                self.logger.warning(
                    "Live announcement %s reached the maximum length of %s seconds",
                    session.session_id,
                    MAX_ANNOUNCEMENT_SECONDS,
                )
                return

    async def _play_live_announcement(
        self,
        session: LiveAnnouncementSession,
        player_id: str,
        pre_announce: bool | None,
        volume_level: int | None,
    ) -> None:
        """Play the session audio as an announcement and drop the session afterwards."""
        try:
            # the player was available when the handshake accepted it, but a whole clip
            # has been spoken since; an unavailable player drops the command downstream
            # without a word, which would be reported back as a clip that played
            player = self.mass.players.get_player(player_id)
            if player is None or not player.available:
                raise PlayerCommandFailed(f"Player {player_id} is no longer available.")
            # a player that plays announcements natively hands off to its provider, which
            # has no deadline of its own - one that never returns would otherwise keep
            # this session (and its slot) alive for as long as the server runs
            async with asyncio.timeout(ANNOUNCEMENT_TIMEOUT):
                await self.mass.players.play_announcement(
                    player_id,
                    url=session.url,
                    pre_announce=pre_announce,
                    volume_level=volume_level,
                )
        except TimeoutError as err:
            # reported to the client rather than swallowed: a timeout means the player
            # never confirmed it played, so "finished" would be a guess
            raise PlayerCommandFailed("The announcement did not finish playing.") from err
        finally:
            self._sessions.pop(session.session_id, None)

    def _create_session(self, audio_format: AudioFormat) -> LiveAnnouncementSession:
        """Register a new session and return it."""
        session_id = secrets.token_urlsafe(16)
        path = LIVE_ANNOUNCEMENT_STREAM_PATH.format(session_id=session_id)
        session = LiveAnnouncementSession(
            session_id, f"{self.mass.streams.base_url}{path}", audio_format
        )
        self._sessions[session_id] = session
        return session

    async def _authenticate(self, request: web.Request, ws: web.WebSocketResponse) -> bool:
        """
        Authenticate a client connection, mirroring the visualizer relay handshake.

        Ingress requests are authenticated by Home Assistant via headers; everything
        else must send `{"type": "auth", "token": ...}` first.

        :param request: The incoming HTTP request.
        :param ws: The prepared WebSocket response.
        :return: True when the client may announce.
        """
        is_ingress = is_request_from_ingress(request)
        if is_ingress:
            user = await get_authenticated_user(request)
        elif (message := await self._read_message(request, ws, "auth")) is None:
            return False
        elif not (token := message.get("token")):
            return await self._reject(request, ws, "Auth message without a token")
        else:
            user = await self.mass.webserver.auth.authenticate_with_token(str(token))
        if user is None:
            return await self._reject(request, ws, "Authentication failed")
        if not is_ingress and user.username == HOMEASSISTANT_SYSTEM_USER:
            # the token of the Home Assistant system user is only meant to travel over
            # the ingress connection, mirroring the policy of the websocket api
            return await self._reject(
                request, ws, "Home Assistant system user not allowed on regular webserver"
            )
        if not has_scope(user, Scope.PLAYERS_CONTROL):
            return await self._reject(request, ws, "Not allowed to control players")
        # the announcement is dispatched as a task, which inherits this context, so the
        # player command it runs sees the user and applies their player restrictions
        set_current_user(user)
        return True

    async def _read_start_message(
        self, request: web.Request, ws: web.WebSocketResponse
    ) -> LiveAnnouncementStart | None:
        """Read and validate the start message, returning None when it is unusable."""
        if (message := await self._read_message(request, ws, "start")) is None:
            return None
        player_id = message.get("player_id")
        player = self.mass.players.get_player(player_id) if isinstance(player_id, str) else None
        # an unavailable player silently drops the command downstream, which would
        # otherwise be reported to the client as an announcement that played
        if not isinstance(player_id, str) or player is None or not player.available:
            await self._reject(request, ws, f"Unknown or unavailable player: {player_id}")
            return None
        sample_rate = message.get("sample_rate")
        if (
            not isinstance(sample_rate, int)
            or not MIN_SAMPLE_RATE <= sample_rate <= MAX_SAMPLE_RATE
        ):
            await self._reject(request, ws, f"Unsupported sample rate: {sample_rate}")
            return None
        channels = message.get("channels", 1)
        if not isinstance(channels, int) or not 1 <= channels <= MAX_CHANNELS:
            await self._reject(request, ws, f"Unsupported channel count: {channels}")
            return None
        pre_announce = message.get("pre_announce")
        volume_level = message.get("volume_level")
        return LiveAnnouncementStart(
            player_id=player_id,
            audio_format=AudioFormat(
                content_type=ContentType.PCM_S16LE,
                sample_rate=sample_rate,
                bit_depth=LIVE_ANNOUNCEMENT_BIT_DEPTH,
                channels=channels,
            ),
            pre_announce=pre_announce if isinstance(pre_announce, bool) else None,
            volume_level=volume_level if isinstance(volume_level, int) else None,
        )

    async def _read_message(
        self, request: web.Request, ws: web.WebSocketResponse, expected_type: str
    ) -> dict[str, object] | None:
        """Read one json message of the expected type, returning None when it is not."""
        try:
            async with asyncio.timeout(HANDSHAKE_TIMEOUT):
                msg = await ws.receive()
        except TimeoutError:
            await self._reject(request, ws, f"Timeout waiting for the {expected_type} message")
            return None
        if msg.type != WSMsgType.TEXT:
            await self._reject(request, ws, f"Expected a {expected_type} message")
            return None
        try:
            data = json_loads(msg.data)
        except JSON_DECODE_EXCEPTIONS:
            await self._reject(request, ws, f"Invalid JSON in the {expected_type} message")
            return None
        if not isinstance(data, dict) or data.get("type") != expected_type:
            await self._reject(request, ws, f"Expected a {expected_type} message")
            return None
        return data

    async def _reject(self, request: web.Request, ws: web.WebSocketResponse, reason: str) -> bool:
        """
        Close a connection that may not (or cannot) announce, and log why.

        The reason travels in the close frame, where the client reads it to tell the
        user what went wrong instead of showing a bare disconnect.
        """
        self.logger.warning("Rejected live announcement from %s: %s", request.remote, reason)
        # a close frame carries at most 125 bytes, 2 of which are the status code, and a
        # reason quoting client input can exceed that - which the browser drops entirely.
        # the decode/encode round trip drops a character the cut landed in the middle of,
        # since an invalid utf-8 reason would be rejected just the same.
        message = reason.encode()[:MAX_CLOSE_REASON_BYTES].decode(errors="ignore").encode()
        await ws.close(code=4001, message=message)
        return False

    async def _send(self, ws: web.WebSocketResponse, message: dict[str, object]) -> None:
        """Send one json message, ignoring a client that already went away."""
        with suppress(ConnectionError, RuntimeError):
            await ws.send_str(dumps(message).decode())
