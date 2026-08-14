"""
Music Assistant WebRTC Gateway.

This module provides WebRTC-based remote access to Music Assistant instances.
It connects to a signaling server and handles incoming WebRTC connections,
bridging them to the local WebSocket API.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass, field
from functools import partial
from typing import TYPE_CHECKING, Any, ClassVar, NamedTuple
from urllib.parse import urlparse

import aiohttp
from aiolibdatachannel import (
    ConnectionClosedError,
    IceServer,
    LogLevel,
    PeerConnection,
    RTCConfiguration,
    RTCError,
    RTCState,
    StateChangeEvent,
    install_python_logger,
)

from music_assistant.constants import MASS_LOGGER_NAME, SENDSPIN_SERVER_PORT, VERBOSE_LOG_LEVEL
from music_assistant.controllers.streams.live_announcements import LIVE_ANNOUNCEMENT_ROUTE

if TYPE_CHECKING:
    from aiolibdatachannel import DataChannel

LOGGER = logging.getLogger(f"{MASS_LOGGER_NAME}.remote_access")

# Max concurrent proxied (image) fetches, so a burst of album-art requests stays bounded
# instead of piling up local requests and the response bodies they buffer (see #4889).
HTTP_PROXY_CONCURRENCY = 6

# Preferred piece size when chunking an oversized message; each piece becomes a base64 frame
# roughly a third larger, so the channel's negotiated limit can size it down further.
DATA_CHANNEL_CHUNK_SIZE = 64 * 1024

# Room a chunk frame's JSON envelope takes around its base64 payload: 60 bytes of fixed keys
# plus the group, sequence and count numbers.
DATA_CHANNEL_CHUNK_OVERHEAD = 128

# Preferred body chunk size on the dedicated http proxy channel. Raw binary needs no escaping,
# so this sits close to libdatachannel's 256 KiB cap; the channel's negotiated limit still wins.
HTTP_PROXY_BODY_CHUNK_SIZE = 192 * 1024

DEFAULT_SENDSPIN_URL = f"ws://localhost:{SENDSPIN_SERVER_PORT}/sendspin"

# Labels of the data channels the gateway routes, next to the client's own API channel
CHANNEL_SENDSPIN = "sendspin"
CHANNEL_LIVE_ANNOUNCEMENT = "live_announcement"
CHANNEL_HTTP_PROXY = "http_proxy"


class _BridgeTarget(NamedTuple):
    """The local WebSocket a data channel label is bridged to."""

    url: str
    on_first_message: Callable[[WebRTCSession, str], None] | None = None


@dataclass
class _ServedChannel:
    """A data channel the gateway serves, plus the local WebSocket it is bridged to (if any)."""

    label: str
    channel: DataChannel | None
    local_ws: aiohttp.ClientWebSocketResponse | None = None


@dataclass
class WebRTCSession:
    """Represents an active WebRTC session with a remote client."""

    session_id: str
    pc: PeerConnection
    # Main API channel (ma-api) - bridges to local MA WebSocket API
    data_channel: DataChannel | None = None
    local_ws: aiohttp.ClientWebSocketResponse | None = None
    # Channels served by label (sendspin, live announcements, http proxy)
    channels: dict[str, _ServedChannel] = field(default_factory=dict)
    sendspin_player_id: str | None = None  # Extracted from first sendspin auth message


# Serves one incoming data channel for a session until that channel goes away.
type _ChannelHandler = Callable[
    [WebRTCSession, _ServedChannel, DataChannel], Coroutine[Any, Any, None]
]


class WebRTCGateway:
    """
    WebRTC Gateway for Music Assistant Remote Access.

    This gateway:
    1. Connects to a signaling server
    2. Registers with a unique Remote ID
    3. Handles incoming WebRTC connections from remote PWA clients
    4. Bridges WebRTC DataChannel messages to the local WebSocket API
    """

    # Close code 4000 means this connection was replaced by a new one from the same server
    # In that case, we should not reconnect as another connection is now active
    CLOSE_CODE_REPLACED = 4000

    # Default ICE servers (public STUN only - used as fallback)
    DEFAULT_ICE_SERVERS: ClassVar[list[dict[str, Any]]] = [
        {"urls": "stun:stun.home-assistant.io:3478"},
        {"urls": "stun:stun.l.google.com:19302"},
        {"urls": "stun:stun1.l.google.com:19302"},
        {"urls": "stun:stun.cloudflare.com:3478"},
    ]

    def __init__(
        self,
        http_session: aiohttp.ClientSession,
        remote_id: str,
        cert_pem: str,
        key_pem: str,
        signaling_url: str = "wss://signaling.music-assistant.io/ws",
        local_ws_url: str = "ws://localhost:8095/ws",
        sendspin_url: str = DEFAULT_SENDSPIN_URL,
        ice_servers: list[dict[str, Any]] | None = None,
        ice_servers_callback: Callable[[], Awaitable[list[dict[str, Any]]]] | None = None,
        set_sendspin_player_callback: Callable[[str, str], None] | None = None,
    ) -> None:
        """
        Initialize the WebRTC Gateway.

        :param http_session: Shared aiohttp ClientSession for HTTP/WebSocket connections.
        :param remote_id: Remote ID for this server instance.
        :param cert_pem: Persistent DTLS certificate (PEM), enabling client-side pinning.
        :param key_pem: Private key (PEM) matching the DTLS certificate.
        :param signaling_url: WebSocket URL of the signaling server.
        :param local_ws_url: Same-host WebSocket URL of the Music Assistant API to bridge to.
        :param sendspin_url: Internal Sendspin WebSocket URL to bridge to.
        :param ice_servers: List of ICE server configurations (used at registration time).
        :param ice_servers_callback: Optional callback to fetch fresh ICE servers for each session.
        :param set_sendspin_player_callback: Callback to set sendspin player for a session.
        """
        self.http_session = http_session
        self.signaling_url = signaling_url
        self.local_ws_url = local_ws_url
        self.sendspin_url = sendspin_url
        self._remote_id = remote_id
        self._cert_pem = cert_pem
        self._key_pem = key_pem
        self.logger = LOGGER
        self._ice_servers_callback = ice_servers_callback
        self._set_sendspin_player_callback = set_sendspin_player_callback

        # Data channel label -> the handler that serves it. The bridged labels each name a
        # local WebSocket; the live announcement route sits on the same webserver as the
        # ma-api WebSocket. The http proxy is served in-process instead of bridged.
        self._channel_handlers: dict[str, _ChannelHandler] = {
            CHANNEL_SENDSPIN: partial(
                self._bridge_websocket,
                target=_BridgeTarget(self.sendspin_url, self._try_extract_sendspin_client_id),
            ),
            CHANNEL_LIVE_ANNOUNCEMENT: partial(
                self._bridge_websocket,
                target=_BridgeTarget(_ws_url_for_path(self.local_ws_url, LIVE_ANNOUNCEMENT_ROUTE)),
            ),
            CHANNEL_HTTP_PROXY: self._serve_http_proxy,
        }

        # Static ICE servers used at registration time (relayed to clients via signaling server)
        self.ice_servers = ice_servers or self.DEFAULT_ICE_SERVERS

        self.sessions: dict[str, WebRTCSession] = {}
        self._background_tasks: set[asyncio.Task[None]] = set()
        self._signaling_ws: aiohttp.ClientWebSocketResponse | None = None
        self._running = False
        self._reconnect_delay = 10  # Wait 10 seconds before reconnecting
        self._max_reconnect_delay = 300  # Max 5 minutes between reconnects
        self._current_reconnect_delay = 10
        self._run_task: asyncio.Task[None] | None = None
        self._is_connected = False
        self._connecting = False
        # gateway-wide by design: normally a single remote client is connected
        self._http_proxy_semaphore = asyncio.Semaphore(HTTP_PROXY_CONCURRENCY)
        self._chunk_group_seq = 0

    @property
    def is_running(self) -> bool:
        """Return whether the gateway is running."""
        return self._running

    @property
    def is_connected(self) -> bool:
        """Return whether the gateway is connected to the signaling server."""
        return self._is_connected

    async def start(self) -> None:
        """Start the WebRTC Gateway."""
        if self._running:
            self.logger.warning("WebRTC Gateway already running, skipping start")
            return
        # Failing candidates and permissions is how ICE converges: full chatter only at VERBOSE
        if self.logger.isEnabledFor(VERBOSE_LOG_LEVEL):
            rtc_log_level = LogLevel.VERBOSE
        elif self.logger.isEnabledFor(logging.DEBUG):
            rtc_log_level = LogLevel.WARNING
            self.logger.addFilter(_BENIGN_NATIVE_NOISE_FILTER)
        else:
            rtc_log_level = LogLevel.ERROR
        install_python_logger(self.logger, level=rtc_log_level)
        self.logger.info("Starting WebRTC Gateway")
        self.logger.debug("Signaling URL: %s", self.signaling_url)
        self.logger.debug("Local WS URL: %s", self.local_ws_url)
        self._running = True
        self._run_task = asyncio.create_task(self._run())
        self.logger.debug("WebRTC Gateway start task created")

    async def stop(self) -> None:
        """Stop the WebRTC Gateway."""
        self.logger.info("Stopping WebRTC Gateway")
        self.logger.removeFilter(_BENIGN_NATIVE_NOISE_FILTER)
        self._running = False

        # Close all sessions
        for session_id in list(self.sessions.keys()):
            await self._close_session(session_id)

        # Close signaling connection gracefully
        if self._signaling_ws and not self._signaling_ws.closed:
            try:
                await self._signaling_ws.close()
            except Exception:
                self.logger.debug("Error closing signaling WebSocket", exc_info=True)

        # Cancel run task and wait for it to finish
        if self._run_task and not self._run_task.done():
            self._run_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._run_task

        # Wait briefly for any in-progress connection to notice _running=False
        if self._connecting:
            await asyncio.sleep(0.1)

        self._signaling_ws = None
        self._connecting = False

    async def _get_fresh_ice_servers(self) -> list[dict[str, Any]]:
        """Get fresh ICE servers for a new WebRTC session."""
        if self._ice_servers_callback:
            try:
                fresh_servers = await self._ice_servers_callback()
                if fresh_servers:
                    return fresh_servers
            except Exception:
                self.logger.exception("Failed to fetch fresh ICE servers, using cached servers")
        return self.ice_servers

    async def _run(self) -> None:
        """Run the main loop with reconnection logic."""
        self.logger.debug("WebRTC Gateway _run() loop starting")
        while self._running:
            should_reconnect = True
            try:
                should_reconnect = await self._connect_to_signaling()
                # Connection closed gracefully or with error
                self._is_connected = False
                if self._running and should_reconnect:
                    self.logger.warning(
                        "Signaling server connection lost. Reconnecting in %ss...",
                        self._current_reconnect_delay,
                    )
            except Exception:
                self._is_connected = False
                self.logger.exception("Signaling connection error")
                if self._running:
                    self.logger.info(
                        "Reconnecting to signaling server in %ss",
                        self._current_reconnect_delay,
                    )

            if self._running and should_reconnect:
                await asyncio.sleep(self._current_reconnect_delay)
                # Exponential backoff with max limit
                self._current_reconnect_delay = min(
                    self._current_reconnect_delay * 2, self._max_reconnect_delay
                )
            elif not should_reconnect:
                # Connection was replaced by another instance, stop the run loop
                self.logger.info("Connection replaced, stopping reconnection attempts")
                self._running = False
                break

    async def _connect_to_signaling(self) -> bool:
        """Connect to the signaling server."""
        if self._connecting:
            self.logger.warning("Already connecting to signaling server, skipping")
            return False  # Don't trigger another reconnect cycle
        self._connecting = True
        close_code: int | None = None
        self.logger.info("Connecting to signaling server: %s", self.signaling_url)
        try:
            self._signaling_ws = await self.http_session.ws_connect(
                self.signaling_url,
                heartbeat=35.0,  # Send ping every 35s (slightly above server's 30s interval)
            )
            # Check if we were stopped while connecting
            if not self._running:
                self.logger.debug("Gateway stopped during connection, closing WebSocket")
                await self._signaling_ws.close()
                self._signaling_ws = None
                self._connecting = False
                return False
            self.logger.debug("WebSocket connection established, id=%s", id(self._signaling_ws))
            self.logger.debug("Sending registration")
            await self._register()
            self._current_reconnect_delay = self._reconnect_delay
            self.logger.debug("Registration sent, waiting for confirmation...")

            # Run message loop and get close code
            close_code = await self._signaling_message_loop(self._signaling_ws)

            # Get close code from WebSocket if not already set from CLOSE message
            if close_code is None:
                close_code = self._signaling_ws.close_code
            ws_exception = self._signaling_ws.exception()
            self.logger.debug(
                "Message loop exited - WebSocket closed: %s, close_code: %s, exception: %s",
                self._signaling_ws.closed,
                close_code,
                ws_exception,
            )
        except TimeoutError:
            self.logger.error("Timeout connecting to signaling server")
        except aiohttp.ClientError as err:
            self.logger.error("Failed to connect to signaling server: %s", err)
        except Exception:
            self.logger.exception("Unexpected error in signaling connection")
        finally:
            self._is_connected = False
            self._connecting = False
            self._signaling_ws = None

        # Check if this connection was replaced by another one
        if close_code == self.CLOSE_CODE_REPLACED:
            self.logger.info("Connection was replaced by another instance - not reconnecting")
            return False

        return True

    async def _signaling_message_loop(self, ws: aiohttp.ClientWebSocketResponse) -> int | None:
        """Process messages from the signaling WebSocket."""
        close_code: int | None = None
        self.logger.debug("Entering message loop")
        async for msg in ws:
            if msg.type == aiohttp.WSMsgType.TEXT:
                try:
                    await self._handle_signaling_message(json.loads(msg.data))
                except Exception:
                    self.logger.exception("Error handling signaling message")
            elif msg.type == aiohttp.WSMsgType.PING:
                self.logger.log(VERBOSE_LOG_LEVEL, "Received WebSocket PING")
            elif msg.type == aiohttp.WSMsgType.PONG:
                self.logger.log(VERBOSE_LOG_LEVEL, "Received WebSocket PONG")
            elif msg.type == aiohttp.WSMsgType.CLOSE:
                close_code = msg.data
                self.logger.warning(
                    "Signaling server sent close frame: code=%s, reason=%s",
                    msg.data,
                    msg.extra,
                )
                break
            elif msg.type == aiohttp.WSMsgType.CLOSED:
                self.logger.warning("Signaling server closed connection")
                break
            elif msg.type == aiohttp.WSMsgType.ERROR:
                self.logger.error("WebSocket error: %s", ws.exception())
                break
            else:
                self.logger.warning("Unexpected WebSocket message type: %s", msg.type)
        return close_code

    async def _register(self) -> None:
        """Register with the signaling server."""
        if self._signaling_ws:
            await self._signaling_ws.send_json(
                {
                    "type": "register-server",
                    "remoteId": self._remote_id,
                    "iceServers": self.ice_servers,
                }
            )

    async def _handle_signaling_message(self, message: dict[str, Any]) -> None:
        """Handle incoming signaling messages."""
        msg_type = message.get("type")

        if msg_type in ("ping", "pong"):
            # Ignore JSON-level ping/pong messages - we use WebSocket protocol-level heartbeat
            # The signaling server still sends these for backward compatibility with older clients
            pass
        elif msg_type == "registered":
            self._is_connected = True
            self.logger.info("Registered with signaling server")
        elif msg_type == "error":
            error_msg = message.get("error") or message.get("message", "Unknown error")
            self.logger.error("Signaling server error: %s", error_msg)
        elif msg_type == "client-connected":
            session_id = message.get("sessionId")
            if session_id:
                await self._create_session(session_id)
                # Send session-ready with fresh ICE servers for the client
                fresh_ice_servers = await self._get_fresh_ice_servers()
                if self._signaling_ws:
                    await self._signaling_ws.send_json(
                        {
                            "type": "session-ready",
                            "sessionId": session_id,
                            "iceServers": fresh_ice_servers,
                        }
                    )
        elif msg_type == "client-disconnected":
            session_id = message.get("sessionId")
            if session_id:
                await self._close_session(session_id)
        elif msg_type == "offer":
            session_id = message.get("sessionId")
            offer_data = message.get("data")
            if session_id and offer_data:
                await self._handle_offer(session_id, offer_data)
        elif msg_type == "ice-candidate":
            session_id = message.get("sessionId")
            candidate_data = message.get("data")
            if session_id and candidate_data:
                await self._handle_ice_candidate(session_id, candidate_data)

    async def _create_session(self, session_id: str) -> None:
        """Create a new WebRTC session."""
        session_ice_servers = await self._get_fresh_ice_servers()
        config = RTCConfiguration(
            ice_servers=self._build_ice_servers(session_ice_servers),
            certificate_pem=self._cert_pem,
            key_pem=self._key_pem,
        )
        pc = PeerConnection(config)
        session = WebRTCSession(session_id=session_id, pc=pc)
        self.sessions[session_id] = session

        # PC-owned tasks are cancelled and awaited by pc.aclose() during teardown
        pc.spawn_task(self._monitor_state(session))
        pc.spawn_task(self._accept_channels(session))
        pc.spawn_task(self._forward_local_candidates(session))

    async def _handle_offer(self, session_id: str, offer: dict[str, Any]) -> None:
        """Handle incoming WebRTC offer."""
        session = self.sessions.get(session_id)
        if not session:
            return
        pc = session.pc

        if pc.closed:
            return

        sdp = offer.get("sdp")
        sdp_type = offer.get("type")
        if not sdp or not sdp_type:
            self.logger.error("Invalid offer data: missing sdp or type")
            return

        try:
            await pc.set_remote_description(str(sdp), "offer")

            if session_id not in self.sessions or pc.closed:
                return

            # Trickle ICE: set_local_description returns as soon as the SDP is ready
            # (candidate-less), so we answer immediately instead of blocking on
            # create_answer() until ICE gathering completes (~20s+ with slow STUN/TURN).
            # Local candidates are streamed separately by _forward_local_candidates.
            answer = await pc.set_local_description("answer")

            if session_id not in self.sessions or pc.closed:
                return

            if self._signaling_ws:
                await self._signaling_ws.send_json(
                    {
                        "type": "answer",
                        "sessionId": session_id,
                        "data": {
                            "sdp": answer.sdp,
                            "type": answer.type,
                        },
                    }
                )
        except Exception:
            self.logger.exception("Error handling offer for session %s", session_id)
            # Clean up the session on error
            await self._close_session(session_id)

    async def _handle_ice_candidate(self, session_id: str, candidate: dict[str, Any]) -> None:
        """Handle incoming ICE candidate."""
        session = self.sessions.get(session_id)
        if not session or not candidate:
            return

        pc = session.pc
        if pc.closed:
            return

        candidate_str = candidate.get("candidate")
        sdp_mid = candidate.get("sdpMid")

        if not candidate_str:
            return

        # libdatachannel accepts both the browser's "candidate:..."-prefixed form and the
        # bare form, so the string is forwarded as-is. add_remote_candidate buffers the
        # candidate internally until the remote description is set.
        mid = str(sdp_mid) if sdp_mid else ""
        try:
            await pc.add_remote_candidate(candidate_str, mid)
        except Exception:
            self.logger.exception("Failed to add ICE candidate for session %s", session_id)

    async def _handle_http_proxy_request(
        self,
        channel: DataChannel | None,
        request_data: dict[str, Any],
        send_lock: asyncio.Lock | None = None,
    ) -> None:
        """
        Handle an HTTP proxy request from a remote client.

        :param channel: Data channel the request arrived on, and the response is sent back on.
        :param request_data: The decoded ``http-proxy-request`` message.
        :param send_lock: Send lock of the dedicated http proxy channel, which answers with a
            JSON header plus a raw binary body. Omitted for clients that proxy over the API
            channel, which get the whole response hex-encoded in one JSON message instead.
        """
        request_id = request_data.get("id")
        method = request_data.get("method", "GET")
        path = request_data.get("path", "/")
        headers = request_data.get("headers", {})

        # Build local HTTP URL from the WebSocket URL.
        # Handle both ws:// and wss:// schemes.
        parsed = urlparse(self.local_ws_url)
        http_scheme = "https" if parsed.scheme == "wss" else "http"
        # Keep `path` as a URI path so an `@`/`//` prefix can't repoint the host (SSRF).
        local_http_url = f"{http_scheme}://{parsed.netloc}/{path.lstrip('/')}"

        self.logger.debug("HTTP proxy request: %s %s", method, local_http_url)

        async with self._http_proxy_semaphore:
            try:
                # Use shared HTTP session for this request
                # this dial never leaves the host: TLS verification would fail on the bind
                # address, and an unfollowed redirect cannot take the unverified dial off-host
                async with self.http_session.request(
                    method, local_http_url, headers=headers, ssl=False, allow_redirects=False
                ) as response:
                    body = await response.read()
                    await self._send_http_proxy_response(
                        channel,
                        request_id,
                        response.status,
                        dict(response.headers),
                        body,
                        send_lock,
                    )
            except Exception as err:
                self.logger.exception("Error handling HTTP proxy request")
                await self._send_http_proxy_response(
                    channel,
                    request_id,
                    500,
                    {"Content-Type": "text/plain"},
                    str(err).encode(),
                    send_lock,
                )

    async def _send_http_proxy_response(
        self,
        channel: DataChannel | None,
        request_id: str | None,
        status: int,
        headers: dict[str, str],
        body: bytes,
        send_lock: asyncio.Lock | None = None,
    ) -> None:
        """
        Send an HTTP-proxy response back on the channel its request arrived on.

        :param send_lock: Send lock of the dedicated http proxy channel, whose responses are a
            JSON header followed by the body as raw binary frames. Without it the response goes
            out hex-encoded inside one JSON message, as clients on the API channel expect.
        """
        if send_lock is None:
            await self._send_chunked(
                channel,
                json.dumps(
                    {
                        "type": "http-proxy-response",
                        "id": request_id,
                        "status": status,
                        "headers": headers,
                        "body": body.hex(),
                    }
                ),
            )
            return

        header = json.dumps(
            {
                "type": "http-proxy-response",
                "id": request_id,
                "status": status,
                "headers": headers,
                "size": len(body),
            }
        )
        # The body frames carry no request id, so an interleaved response would be
        # indistinguishable from this one's body: hold the channel for header plus body.
        # Responses therefore queue whole rather than frame by frame, which costs nothing on
        # a channel that already sends one message at a time.
        async with send_lock:
            await self._send_on_channel(channel, header)
            if channel is None or not channel.is_open:
                return
            # a peer that advertises no limit in its SDP is assumed to accept only 64 KiB
            chunk_size = min(HTTP_PROXY_BODY_CHUNK_SIZE, channel.max_message_size)
            for offset in range(0, len(body), chunk_size):
                await self._send_on_channel(channel, body[offset : offset + chunk_size])

    async def _send_chunked(self, channel: DataChannel | None, text: str) -> None:
        """Send a text message on a data channel, chunking it if it exceeds the size limit."""
        if channel is None or not channel.is_open:
            return
        # a peer that advertises no limit in its SDP is assumed to accept only 64 KiB
        limit = channel.max_message_size
        data = text.encode()
        if len(data) <= min(DATA_CHANNEL_CHUNK_SIZE, limit):
            await self._send_on_channel(channel, text)
            return

        # Oversized messages are split into base64 frames the client reassembles by group id
        # (base64 keeps each frame's size predictable regardless of JSON escaping / unicode).
        # A piece is sized so its frame still fits the limit: base64 turns every 3 bytes into 4,
        # on top of the JSON envelope.
        piece_size = min(DATA_CHANNEL_CHUNK_SIZE, (limit - DATA_CHANNEL_CHUNK_OVERHEAD) // 4 * 3)
        self._chunk_group_seq += 1
        group_id = self._chunk_group_seq
        count = (len(data) + piece_size - 1) // piece_size
        for seq in range(count):
            # a send onto a closed channel returns without suspending, so without this the
            # loop would encode and discard every remaining piece without yielding
            if channel.is_closed:
                return
            piece = data[seq * piece_size : (seq + 1) * piece_size]
            await self._send_on_channel(
                channel,
                json.dumps(
                    {
                        "type": "__chunk__",
                        "id": group_id,
                        "seq": seq,
                        "count": count,
                        "b64": base64.b64encode(piece).decode(),
                    }
                ),
            )

    async def _close_session(self, session_id: str) -> None:
        """Close a WebRTC session."""
        session = self.sessions.pop(session_id, None)
        if not session:
            return

        # Close the local bridges first so no more data is fed through the channels
        bridged = [served.local_ws for served in session.channels.values()]
        for local_ws in (session.local_ws, *bridged):
            if local_ws is not None and not local_ws.closed:
                with contextlib.suppress(Exception):
                    await local_ws.close()
        session.local_ws = None
        session.channels.clear()

        # aclose tears down all PC-owned pumps and every data channel; safe here because
        # _close_session is never invoked from within a PC-owned task
        await session.pc.aclose()

    # ---- Session pumps (PC-owned tasks) --------------------------------------

    async def _monitor_state(self, session: WebRTCSession) -> None:
        """Close the session when its PeerConnection reports a failed state."""
        async for event in session.pc.events():
            if isinstance(event, StateChangeEvent) and event.state == RTCState.FAILED:
                self._schedule_close(session.session_id)
                return

    async def _forward_local_candidates(self, session: WebRTCSession) -> None:
        """Stream locally-gathered ICE candidates to the remote client (trickle ICE)."""
        # The iterator ends on gathering-complete or when the PC closes.
        async for candidate in session.pc.ice_candidates():
            if session.session_id not in self.sessions or not self._signaling_ws:
                return
            await self._signaling_ws.send_json(
                {
                    "type": "ice-candidate",
                    "sessionId": session.session_id,
                    "data": {
                        "candidate": candidate.candidate,
                        "sdpMid": candidate.mid,
                    },
                }
            )

    async def _accept_channels(self, session: WebRTCSession) -> None:
        """Accept incoming data channels and start their handlers."""
        async for channel in session.pc.incoming_data_channels():
            if (handler := self._channel_handlers.get(channel.label)) is not None:
                if channel.label in session.channels:
                    # replacing the entry would leave the running handler untracked, and
                    # tearing either one down would then orphan the other's resources
                    self.logger.warning(
                        "Refusing a second '%s' data channel for session %s",
                        channel.label,
                        session.session_id,
                    )
                    channel.close()
                    continue
                served = _ServedChannel(label=channel.label, channel=channel)
                session.channels[channel.label] = served
                session.pc.spawn_task(handler(session, served, channel))
            elif session.data_channel is None:
                # the browser opens its API channel first, whatever label it gives it
                session.data_channel = channel
                session.pc.spawn_task(self._bridge_ma_api(session, channel))
            else:
                # a label this server does not know must not be taken for a second API
                # channel: that would replace the live bridge and break the session
                self.logger.warning(
                    "Refusing data channel with unknown label '%s' for session %s",
                    channel.label,
                    session.session_id,
                )
                channel.close()

    async def _bridge_ma_api(self, session: WebRTCSession, channel: DataChannel) -> None:
        """Bridge the ma-api data channel to the local WebSocket API."""
        # wait for the channel to open first, so the local WS's initial server_info (sent
        # immediately on connect) isn't dropped by _send_on_channel while it's still opening
        try:
            await channel.wait_open()
        except RTCError:
            self._schedule_close(session.session_id)
            return
        try:
            # Include session_id in URL so server can track WebRTC sessions
            ws_url = f"{self.local_ws_url}?webrtc_session_id={session.session_id}"
            # TLS verification would fail on the bind address and adds nothing to a dial
            # that never leaves this host
            session.local_ws = await self.http_session.ws_connect(ws_url, ssl=False)
        except Exception:
            self.logger.exception("Failed to connect to local WebSocket %s", self.local_ws_url)
            channel.close()
            self._schedule_close(session.session_id)
            return

        # from_local runs as its own PC-owned pump; this task drives channel -> local
        session.pc.spawn_task(self._ma_api_from_local(session, channel))
        await self._ma_api_to_local(session, channel)
        # channel -> local loop ended (remote channel closed): tear down the session
        self._schedule_close(session.session_id)

    async def _ma_api_to_local(self, session: WebRTCSession, channel: DataChannel) -> None:
        """Forward messages from the ma-api data channel to the local WebSocket."""
        try:
            async for message in channel:
                if isinstance(message, str):
                    # Check if this is an HTTP proxy request
                    try:
                        msg_data = json.loads(message)
                        if (
                            isinstance(msg_data, dict)
                            and msg_data.get("type") == "http-proxy-request"
                        ):
                            # clients without a dedicated http proxy channel proxy over
                            # this one; handle off the receive loop so a slow fetch never
                            # blocks API messages or the next image (see #4889)
                            session.pc.spawn_task(
                                self._handle_http_proxy_request(channel, msg_data)
                            )
                            continue
                    except json.JSONDecodeError, ValueError:
                        pass

                if session.local_ws and not session.local_ws.closed:
                    if isinstance(message, bytes):
                        await session.local_ws.send_bytes(message)
                    else:
                        await session.local_ws.send_str(message)
        except ConnectionClosedError:
            self.logger.debug("ma-api channel closed for session %s", session.session_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            self.logger.exception("Error forwarding to local WebSocket")

    async def _ma_api_from_local(self, session: WebRTCSession, channel: DataChannel) -> None:
        """Forward messages from the local WebSocket to the ma-api data channel."""
        local_ws = session.local_ws
        if local_ws is None:
            return
        try:
            async for msg in local_ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    await self._send_chunked(channel, msg.data)
                elif msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSED):
                    break
        except asyncio.CancelledError:
            raise
        except Exception:
            self.logger.exception("Error forwarding from local WebSocket")
        # the local WS closed: the ma-api session is unusable, so tear it down instead of
        # leaving the client an open channel that silently drops messages
        self._schedule_close(session.session_id)

    async def _bridge_websocket(
        self,
        session: WebRTCSession,
        served: _ServedChannel,
        channel: DataChannel,
        *,
        target: _BridgeTarget,
    ) -> None:
        """
        Bridge a data channel to the local WebSocket its label is routed to.

        :param target: The local WebSocket this channel's label is routed to.
        """
        try:
            # TLS verification would fail on the bind address and adds nothing to a dial
            # that never leaves this host (a no-op for the plain ws:// targets)
            served.local_ws = await self.http_session.ws_connect(target.url, ssl=False)
            self.logger.debug(
                "%s channel connected for session %s", served.label, session.session_id
            )
        except Exception:
            self.logger.exception(
                "Failed to connect %s channel to %s for session %s",
                served.label,
                target.url,
                session.session_id,
            )
            await self._close_channel(session, served)
            return

        # from_local runs as its own PC-owned pump; this task drives channel -> local
        session.pc.spawn_task(self._ws_bridge_from_local(session, served, channel))
        await self._ws_bridge_to_local(session, served, channel, target)
        # channel -> local loop ended (remote channel closed): tear down only this bridge
        await self._close_channel(session, served)

    async def _ws_bridge_to_local(
        self,
        session: WebRTCSession,
        served: _ServedChannel,
        channel: DataChannel,
        target: _BridgeTarget,
    ) -> None:
        """Forward messages from a bridged data channel to its local WebSocket."""
        first_message = True
        try:
            async for message in channel:
                if first_message:
                    first_message = False
                    if target.on_first_message and isinstance(message, str):
                        target.on_first_message(session, message)

                local_ws = served.local_ws
                if local_ws and not local_ws.closed:
                    if isinstance(message, bytes):
                        await local_ws.send_bytes(message)
                    else:
                        await local_ws.send_str(message)
        except ConnectionClosedError:
            self.logger.debug("%s channel closed for session %s", served.label, session.session_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            self.logger.exception("Error forwarding %s to local", served.label)

    async def _ws_bridge_from_local(
        self, session: WebRTCSession, served: _ServedChannel, channel: DataChannel
    ) -> None:
        """Forward messages from a bridged local WebSocket to its data channel."""
        local_ws = served.local_ws
        if local_ws is None:
            return
        try:
            async for msg in local_ws:
                if msg.type in {aiohttp.WSMsgType.TEXT, aiohttp.WSMsgType.BINARY}:
                    await self._send_on_channel(channel, msg.data)
                elif msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSED):
                    break
        except asyncio.CancelledError:
            raise
        except Exception:
            self.logger.exception("Error forwarding %s from local", served.label)
        # the local WS closed: close only this bridge, leaving the ma-api session up
        await self._close_channel(session, served)

    async def _serve_http_proxy(
        self, session: WebRTCSession, served: _ServedChannel, channel: DataChannel
    ) -> None:
        """Serve proxied HTTP requests arriving on their own data channel."""
        send_lock = asyncio.Lock()
        try:
            async for message in channel:
                if not isinstance(message, str):
                    continue
                try:
                    request = json.loads(message)
                except json.JSONDecodeError, ValueError:
                    continue
                if isinstance(request, dict) and request.get("type") == "http-proxy-request":
                    # handle off the receive loop so a slow fetch never holds up the next
                    # request (bounded by the gateway-wide semaphore; see #4889)
                    session.pc.spawn_task(
                        self._handle_http_proxy_request(channel, request, send_lock)
                    )
        except ConnectionClosedError:
            self.logger.debug("%s channel closed for session %s", served.label, session.session_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            self.logger.exception("Error serving %s channel", served.label)
        # the remote channel closed: tear down only this channel, leaving the session up
        await self._close_channel(session, served)

    async def _close_channel(self, session: WebRTCSession, served: _ServedChannel) -> None:
        """Close one served data channel and the local WebSocket it is bridged to."""
        # only drop the entry while it still points at this channel, so a teardown can
        # never untrack a channel that replaced it
        if session.channels.get(served.label) is served:
            del session.channels[served.label]
        local_ws = served.local_ws
        served.local_ws = None
        if local_ws is not None and not local_ws.closed:
            with contextlib.suppress(Exception):
                await local_ws.close()
        channel = served.channel
        served.channel = None
        if channel is not None and not channel.closed:
            with contextlib.suppress(Exception):
                await channel.aclose()

    # ---- Helpers -------------------------------------------------------------

    def _build_ice_servers(self, servers: list[dict[str, Any]]) -> list[IceServer]:
        """Build IceServer entries (one per url) for our own peer connection."""
        ice_servers: list[IceServer] = []
        skipped: list[str] = []
        for server in servers:
            urls = server.get("urls")
            username = server.get("username")
            credential = server.get("credential")
            url_list = [urls] if isinstance(urls, str) else (urls or [])
            for url in url_list:
                if not _is_usable_ice_url(url):
                    skipped.append(url)
                    continue
                ice_servers.append(IceServer(url=url, username=username, credential=credential))
        if skipped:
            self.logger.debug("Skipping ICE server urls unusable by libjuice: %s", skipped)
        return ice_servers

    def _schedule_close(self, session_id: str) -> None:
        """Schedule session teardown on a gateway-owned task."""
        # Run outside the PC-owned pump that triggered it so pc.aclose() (which the pump
        # is awaited by) does not deadlock on itself
        if session_id not in self.sessions:
            return
        task = asyncio.create_task(self._close_session(session_id))
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    async def _send_on_channel(self, channel: DataChannel | None, data: str | bytes) -> None:
        """Send data on a data channel if it is currently open."""
        if channel is None or not channel.is_open:
            return
        try:
            await channel.send(data)
        except ConnectionClosedError:
            pass
        except RTCError as err:
            # a single failed send (e.g. an over-limit message) must not tear down the pump
            self.logger.warning("Dropping %d-byte data channel message: %s", len(data), err)

    def _try_extract_sendspin_client_id(self, session: WebRTCSession, message: str) -> None:
        """Try to extract client_id from sendspin auth message and set on websocket client."""
        try:
            data = json.loads(message)
            if data.get("type") != "auth":
                return  # Not an auth message

            # This is an auth message - extract client_id if present
            if client_id := data.get("client_id"):
                session.sendspin_player_id = client_id
                self.logger.debug(
                    "Extracted sendspin player %s for session %s",
                    client_id,
                    session.session_id,
                )
                # Use callback to set sendspin player on the websocket client
                if self._set_sendspin_player_callback:
                    self._set_sendspin_player_callback(session.session_id, client_id)
        except json.JSONDecodeError, TypeError:
            pass  # Not valid JSON, ignore


class _BenignNativeNoiseFilter(logging.Filter):
    """Drops known-benign native libdatachannel log lines."""

    def filter(self, record: logging.LogRecord) -> bool:
        """Return whether this log record is worth keeping."""
        # Cloudflare omits the ERROR-CODE attribute, so libjuice warns on a benign refusal
        return "TURN CreatePermission error response, code=0" not in record.getMessage()


_BENIGN_NATIVE_NOISE_FILTER = _BenignNativeNoiseFilter()


def _ws_url_for_path(ws_url: str, path: str) -> str:
    """
    Return the url of another WebSocket route on the same host.

    :param ws_url: WebSocket url to take the scheme and host from.
    :param path: Route to reach on that same host.
    """
    parsed = urlparse(ws_url)
    return f"{parsed.scheme}://{parsed.netloc}{path}"


def _is_usable_ice_url(url: str) -> bool:
    """
    Return whether libdatachannel's ICE backend (libjuice) can use this ICE server url.

    :param url: ICE server url, e.g. ``turn:turn.example.com:3478?transport=tcp``.
    """
    scheme, _, remainder = url.partition(":")
    scheme = scheme.lower()
    if scheme == "stun":
        return True
    if scheme not in ("turn", "turns"):
        return False
    # like rtc::IceServer url parsing, the transport parameter wins over the scheme
    query = remainder.partition("?")[2].lower()
    if "transport=udp" in query:
        return True
    return scheme == "turn" and not ("transport=tcp" in query or "transport=tls" in query)
