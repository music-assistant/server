"""Music Assistant WebRTC Gateway.

This module provides WebRTC-based remote access to Music Assistant instances.
It connects to a signaling server and handles incoming WebRTC connections,
bridging them to the local WebSocket API.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import aiohttp
from aiortc import RTCConfiguration, RTCIceServer, RTCPeerConnection, RTCSessionDescription
from aiortc.sdp import candidate_from_sdp

from music_assistant.constants import MASS_LOGGER_NAME, VERBOSE_LOG_LEVEL
from music_assistant.helpers.webrtc_certificate import create_peer_connection_with_certificate

if TYPE_CHECKING:
    from aiortc.rtcdtlstransport import RTCCertificate

LOGGER = logging.getLogger(f"{MASS_LOGGER_NAME}.remote_access")

# Reduce verbose logging from aiortc/aioice
logging.getLogger("aioice").setLevel(logging.WARNING)
logging.getLogger("aiortc").setLevel(logging.WARNING)


@dataclass
class WebRTCSession:
    """Represents an active WebRTC session with a remote client."""

    session_id: str
    peer_connection: RTCPeerConnection
    # Main API channel (ma-api) - bridges to local MA WebSocket API
    data_channel: Any = None
    local_ws: Any = None
    message_queue: asyncio.Queue[str] = field(default_factory=asyncio.Queue)
    forward_to_local_task: asyncio.Task[None] | None = None
    forward_from_local_task: asyncio.Task[None] | None = None
    # Sendspin channel - bridges to internal sendspin server
    sendspin_channel: Any = None
    sendspin_ws: Any = None
    sendspin_queue: asyncio.Queue[str | bytes] = field(default_factory=asyncio.Queue)
    sendspin_to_local_task: asyncio.Task[None] | None = None
    sendspin_from_local_task: asyncio.Task[None] | None = None


class WebRTCGateway:
    """WebRTC Gateway for Music Assistant Remote Access.

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
    DEFAULT_ICE_SERVERS: list[dict[str, Any]] = [
        {"urls": "stun:stun.home-assistant.io:3478"},
        {"urls": "stun:stun.l.google.com:19302"},
        {"urls": "stun:stun1.l.google.com:19302"},
        {"urls": "stun:stun.cloudflare.com:3478"},
    ]

    def __init__(
        self,
        http_session: aiohttp.ClientSession,
        remote_id: str,
        certificate: RTCCertificate,
        signaling_url: str = "wss://signaling.music-assistant.io/ws",
        local_ws_url: str = "ws://localhost:8095/ws",
        ice_servers: list[dict[str, Any]] | None = None,
        ice_servers_callback: Callable[[], Awaitable[list[dict[str, Any]]]] | None = None,
    ) -> None:
        """
        Initialize the WebRTC Gateway.

        :param http_session: Shared aiohttp ClientSession for HTTP/WebSocket connections.
        :param remote_id: Remote ID for this server instance.
        :param certificate: Persistent RTCCertificate for DTLS, enabling client-side pinning.
        :param signaling_url: WebSocket URL of the signaling server.
        :param local_ws_url: Local WebSocket URL to bridge to.
        :param ice_servers: List of ICE server configurations (used at registration time).
        :param ice_servers_callback: Optional callback to fetch fresh ICE servers for each session.
        """
        self.http_session = http_session
        self.signaling_url = signaling_url
        self.local_ws_url = local_ws_url
        self._remote_id = remote_id
        self._certificate = certificate
        self.logger = LOGGER
        self._ice_servers_callback = ice_servers_callback

        # Static ICE servers used at registration time (relayed to clients via signaling server)
        self.ice_servers = ice_servers or self.DEFAULT_ICE_SERVERS

        self.sessions: dict[str, WebRTCSession] = {}
        self._signaling_ws: aiohttp.ClientWebSocketResponse | None = None
        self._running = False
        self._reconnect_delay = 10  # Wait 10 seconds before reconnecting
        self._max_reconnect_delay = 300  # Max 5 minutes between reconnects
        self._current_reconnect_delay = 10
        self._run_task: asyncio.Task[None] | None = None
        self._is_connected = False
        self._connecting = False

    @property
    def is_running(self) -> bool:
        """Return whether the gateway is running."""
        return self._running

    @property
    def is_connected(self) -> bool:
        """Return whether the gateway is connected to the signaling server."""
        return self._is_connected

    async def _get_fresh_ice_servers(self) -> list[dict[str, Any]]:
        """Get fresh ICE servers for a new WebRTC session.

        If an ice_servers_callback was provided, it will be called to get fresh
        TURN credentials. Otherwise, returns the static ice_servers.

        :return: List of ICE server configurations with fresh credentials.
        """
        if self._ice_servers_callback:
            try:
                fresh_servers = await self._ice_servers_callback()
                if fresh_servers:
                    return fresh_servers
            except Exception:
                self.logger.exception("Failed to fetch fresh ICE servers, using cached servers")
        return self.ice_servers

    async def start(self) -> None:
        """Start the WebRTC Gateway."""
        if self._running:
            self.logger.warning("WebRTC Gateway already running, skipping start")
            return
        self.logger.info("Starting WebRTC Gateway")
        self.logger.debug("Signaling URL: %s", self.signaling_url)
        self.logger.debug("Local WS URL: %s", self.local_ws_url)
        self._running = True
        self._run_task = asyncio.create_task(self._run())
        self.logger.debug("WebRTC Gateway start task created")

    async def stop(self) -> None:
        """Stop the WebRTC Gateway."""
        self.logger.info("Stopping WebRTC Gateway")
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
        """Connect to the signaling server.

        :return: True if reconnection should be attempted, False if connection was replaced.
        """
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
        """Process messages from the signaling WebSocket.

        :param ws: The WebSocket connection to process messages from.
        :return: Close code if connection was closed with a code, None otherwise.
        """
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
        """Handle incoming signaling messages.

        :param message: The signaling message.
        """
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
        """Create a new WebRTC session.

        :param session_id: The session ID.
        """
        session_ice_servers = await self._get_fresh_ice_servers()
        config = RTCConfiguration(
            iceServers=[RTCIceServer(**server) for server in session_ice_servers]
        )
        pc = create_peer_connection_with_certificate(self._certificate, configuration=config)
        session = WebRTCSession(session_id=session_id, peer_connection=pc)
        self.sessions[session_id] = session

        @pc.on("datachannel")
        def on_datachannel(channel: Any) -> None:
            if channel.label == "sendspin":
                session.sendspin_channel = channel
                asyncio.create_task(self._setup_sendspin_channel(session))
            else:
                session.data_channel = channel
                asyncio.create_task(self._setup_data_channel(session))

        @pc.on("icecandidate")
        async def on_icecandidate(candidate: Any) -> None:
            if candidate and self._signaling_ws:
                await self._signaling_ws.send_json(
                    {
                        "type": "ice-candidate",
                        "sessionId": session_id,
                        "data": {
                            "candidate": candidate.candidate,
                            "sdpMid": candidate.sdpMid,
                            "sdpMLineIndex": candidate.sdpMLineIndex,
                        },
                    }
                )

        @pc.on("connectionstatechange")
        async def on_connectionstatechange() -> None:
            if pc.connectionState == "failed":
                await self._close_session(session_id)

    async def _handle_offer(self, session_id: str, offer: dict[str, Any]) -> None:
        """Handle incoming WebRTC offer.

        :param session_id: The session ID.
        :param offer: The offer data.
        """
        session = self.sessions.get(session_id)
        if not session:
            return
        pc = session.peer_connection

        if pc.connectionState in ("closed", "failed"):
            return

        sdp = offer.get("sdp")
        sdp_type = offer.get("type")
        if not sdp or not sdp_type:
            self.logger.error("Invalid offer data: missing sdp or type")
            return

        try:
            await pc.setRemoteDescription(
                RTCSessionDescription(
                    sdp=str(sdp),
                    type=str(sdp_type),
                )
            )

            if session_id not in self.sessions or pc.connectionState in ("closed", "failed"):
                return

            answer = await pc.createAnswer()

            if session_id not in self.sessions or pc.connectionState in ("closed", "failed"):
                return

            await pc.setLocalDescription(answer)

            # Wait for ICE gathering to complete before sending the answer
            # aiortc doesn't support trickle ICE, candidates are embedded in SDP after gathering
            gather_timeout = 30
            gather_start = asyncio.get_event_loop().time()
            while pc.iceGatheringState != "complete":
                if session_id not in self.sessions or pc.connectionState in ("closed", "failed"):
                    return
                if asyncio.get_event_loop().time() - gather_start > gather_timeout:
                    self.logger.warning("Session %s ICE gathering timeout", session_id)
                    break
                await asyncio.sleep(0.1)

            if session_id not in self.sessions or pc.connectionState in ("closed", "failed"):
                return

            if self._signaling_ws:
                await self._signaling_ws.send_json(
                    {
                        "type": "answer",
                        "sessionId": session_id,
                        "data": {
                            "sdp": pc.localDescription.sdp,
                            "type": pc.localDescription.type,
                        },
                    }
                )
        except Exception:
            self.logger.exception("Error handling offer for session %s", session_id)
            # Clean up the session on error
            await self._close_session(session_id)

    async def _handle_ice_candidate(self, session_id: str, candidate: dict[str, Any]) -> None:
        """Handle incoming ICE candidate.

        :param session_id: The session ID.
        :param candidate: The ICE candidate data.
        """
        session = self.sessions.get(session_id)
        if not session or not candidate:
            return

        pc = session.peer_connection
        if pc.connectionState in ("closed", "failed"):
            return

        candidate_str = candidate.get("candidate")
        sdp_mid = candidate.get("sdpMid")
        sdp_mline_index = candidate.get("sdpMLineIndex")

        if not candidate_str:
            return

        try:
            # Parse ICE candidate - browser sends "candidate:..." format
            if candidate_str.startswith("candidate:"):
                sdp_candidate_str = candidate_str[len("candidate:") :]
            else:
                sdp_candidate_str = candidate_str

            ice_candidate = candidate_from_sdp(sdp_candidate_str)
            ice_candidate.sdpMid = str(sdp_mid) if sdp_mid else None
            ice_candidate.sdpMLineIndex = (
                int(sdp_mline_index) if sdp_mline_index is not None else None
            )

            if session_id not in self.sessions or pc.connectionState in ("closed", "failed"):
                return

            await session.peer_connection.addIceCandidate(ice_candidate)
        except Exception:
            self.logger.exception("Failed to add ICE candidate for session %s", session_id)

    async def _setup_data_channel(self, session: WebRTCSession) -> None:
        """Set up data channel and bridge to local WebSocket.

        :param session: The WebRTC session.
        """
        channel = session.data_channel
        if not channel:
            return
        try:
            session.local_ws = await self.http_session.ws_connect(self.local_ws_url)
            loop = asyncio.get_event_loop()

            # Store task references for proper cleanup
            session.forward_to_local_task = asyncio.create_task(self._forward_to_local(session))
            session.forward_from_local_task = asyncio.create_task(self._forward_from_local(session))

            @channel.on("message")  # type: ignore[untyped-decorator]
            def on_message(message: str) -> None:
                # Called from aiortc thread, use call_soon_threadsafe
                # Only queue message if session is still active
                if session.forward_to_local_task and not session.forward_to_local_task.done():
                    loop.call_soon_threadsafe(session.message_queue.put_nowait, message)

            @channel.on("close")  # type: ignore[untyped-decorator]
            def on_close() -> None:
                # Called from aiortc thread, use call_soon_threadsafe to schedule task
                asyncio.run_coroutine_threadsafe(self._close_session(session.session_id), loop)

        except Exception:
            self.logger.exception("Failed to connect to local WebSocket")

    async def _forward_to_local(self, session: WebRTCSession) -> None:
        """Forward messages from WebRTC DataChannel to local WebSocket.

        :param session: The WebRTC session.
        """
        try:
            while session.local_ws and not session.local_ws.closed:
                message = await session.message_queue.get()

                # Check if this is an HTTP proxy request
                try:
                    msg_data = json.loads(message)
                    if isinstance(msg_data, dict) and msg_data.get("type") == "http-proxy-request":
                        # Handle HTTP proxy request
                        await self._handle_http_proxy_request(session, msg_data)
                        continue
                except (json.JSONDecodeError, ValueError):
                    pass

                # Regular WebSocket message
                if session.local_ws and not session.local_ws.closed:
                    await session.local_ws.send_str(message)
        except asyncio.CancelledError:
            # Task was cancelled during cleanup, this is expected
            self.logger.debug("Forward to local task cancelled for session %s", session.session_id)
            raise
        except Exception:
            self.logger.exception("Error forwarding to local WebSocket")

    async def _forward_from_local(self, session: WebRTCSession) -> None:
        """Forward messages from local WebSocket to WebRTC DataChannel.

        :param session: The WebRTC session.
        """
        try:
            async for msg in session.local_ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    if session.data_channel and session.data_channel.readyState == "open":
                        session.data_channel.send(msg.data)
                elif msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSED):
                    break
        except asyncio.CancelledError:
            # Task was cancelled during cleanup, this is expected
            self.logger.debug(
                "Forward from local task cancelled for session %s", session.session_id
            )
            raise
        except Exception:
            self.logger.exception("Error forwarding from local WebSocket")

    async def _handle_http_proxy_request(
        self, session: WebRTCSession, request_data: dict[str, Any]
    ) -> None:
        """Handle HTTP proxy request from remote client.

        :param session: The WebRTC session.
        :param request_data: The HTTP proxy request data.
        """
        request_id = request_data.get("id")
        method = request_data.get("method", "GET")
        path = request_data.get("path", "/")
        headers = request_data.get("headers", {})

        # Build local HTTP URL
        # Extract host and port from local_ws_url (ws://localhost:8095/ws)
        ws_url_parts = self.local_ws_url.replace("ws://", "").split("/")
        host_port = ws_url_parts[0]  # localhost:8095
        local_http_url = f"http://{host_port}{path}"

        self.logger.debug("HTTP proxy request: %s %s", method, local_http_url)

        try:
            # Use shared HTTP session for this request
            async with self.http_session.request(
                method, local_http_url, headers=headers
            ) as response:
                # Read response body
                body = await response.read()

                # Prepare response data
                response_data = {
                    "type": "http-proxy-response",
                    "id": request_id,
                    "status": response.status,
                    "headers": dict(response.headers),
                    "body": body.hex(),  # Send as hex string to avoid encoding issues
                }

                # Send response back through data channel
                if session.data_channel and session.data_channel.readyState == "open":
                    session.data_channel.send(json.dumps(response_data))

        except Exception as err:
            self.logger.exception("Error handling HTTP proxy request")
            # Send error response
            error_response = {
                "type": "http-proxy-response",
                "id": request_id,
                "status": 500,
                "headers": {"Content-Type": "text/plain"},
                "body": str(err).encode().hex(),
            }
            if session.data_channel and session.data_channel.readyState == "open":
                session.data_channel.send(json.dumps(error_response))

    async def _close_session(self, session_id: str) -> None:
        """Close a WebRTC session.

        :param session_id: The session ID.
        """
        session = self.sessions.pop(session_id, None)
        if not session:
            return

        # Cancel forwarding tasks first to prevent race conditions
        if session.forward_to_local_task and not session.forward_to_local_task.done():
            session.forward_to_local_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await session.forward_to_local_task

        if session.forward_from_local_task and not session.forward_from_local_task.done():
            session.forward_from_local_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await session.forward_from_local_task

        # Cancel sendspin forwarding tasks
        if session.sendspin_to_local_task and not session.sendspin_to_local_task.done():
            session.sendspin_to_local_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await session.sendspin_to_local_task

        if session.sendspin_from_local_task and not session.sendspin_from_local_task.done():
            session.sendspin_from_local_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await session.sendspin_from_local_task

        # Close connections
        if session.local_ws and not session.local_ws.closed:
            await session.local_ws.close()
        if session.sendspin_ws and not session.sendspin_ws.closed:
            await session.sendspin_ws.close()
        if session.data_channel:
            session.data_channel.close()
        if session.sendspin_channel:
            session.sendspin_channel.close()
        await session.peer_connection.close()

    async def _setup_sendspin_channel(self, session: WebRTCSession) -> None:
        """Set up sendspin data channel and bridge to internal sendspin server.

        :param session: The WebRTC session.
        """
        channel = session.sendspin_channel
        if not channel:
            return

        try:
            session.sendspin_ws = await self.http_session.ws_connect("ws://127.0.0.1:8927/sendspin")
            self.logger.debug("Sendspin channel connected for session %s", session.session_id)

            loop = asyncio.get_event_loop()

            session.sendspin_to_local_task = asyncio.create_task(
                self._forward_sendspin_to_local(session)
            )
            session.sendspin_from_local_task = asyncio.create_task(
                self._forward_sendspin_from_local(session)
            )

            @channel.on("message")  # type: ignore[untyped-decorator]
            def on_message(message: str | bytes) -> None:
                if session.sendspin_to_local_task and not session.sendspin_to_local_task.done():
                    loop.call_soon_threadsafe(session.sendspin_queue.put_nowait, message)

            @channel.on("close")  # type: ignore[untyped-decorator]
            def on_close() -> None:
                if session.sendspin_ws and not session.sendspin_ws.closed:
                    asyncio.run_coroutine_threadsafe(session.sendspin_ws.close(), loop)

        except Exception:
            self.logger.exception(
                "Failed to connect sendspin channel to internal server for session %s",
                session.session_id,
            )
            # Clean up partial state on failure
            if session.sendspin_to_local_task:
                session.sendspin_to_local_task.cancel()
            if session.sendspin_from_local_task:
                session.sendspin_from_local_task.cancel()
            if session.sendspin_ws and not session.sendspin_ws.closed:
                await session.sendspin_ws.close()

    async def _forward_sendspin_to_local(self, session: WebRTCSession) -> None:
        """Forward messages from sendspin DataChannel to internal sendspin server.

        :param session: The WebRTC session.
        """
        try:
            while session.sendspin_ws and not session.sendspin_ws.closed:
                message = await session.sendspin_queue.get()
                if session.sendspin_ws and not session.sendspin_ws.closed:
                    if isinstance(message, bytes):
                        await session.sendspin_ws.send_bytes(message)
                    else:
                        await session.sendspin_ws.send_str(message)
        except asyncio.CancelledError:
            self.logger.debug(
                "Sendspin forward to local task cancelled for session %s",
                session.session_id,
            )
            raise
        except Exception:
            self.logger.exception("Error forwarding sendspin to local")

    async def _forward_sendspin_from_local(self, session: WebRTCSession) -> None:
        """Forward messages from internal sendspin server to sendspin DataChannel.

        :param session: The WebRTC session.
        """
        if not session.sendspin_ws or session.sendspin_ws.closed:
            return

        try:
            async for msg in session.sendspin_ws:
                if msg.type in {aiohttp.WSMsgType.TEXT, aiohttp.WSMsgType.BINARY}:
                    if session.sendspin_channel and session.sendspin_channel.readyState == "open":
                        session.sendspin_channel.send(msg.data)
                elif msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSED):
                    break
        except asyncio.CancelledError:
            self.logger.debug(
                "Sendspin forward from local task cancelled for session %s",
                session.session_id,
            )
            raise
        except Exception:
            self.logger.exception("Error forwarding sendspin from local")
