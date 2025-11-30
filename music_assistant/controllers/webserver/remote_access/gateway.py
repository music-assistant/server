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
import secrets
import string
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import aiohttp
from aiortc import (
    RTCConfiguration,
    RTCIceCandidate,
    RTCIceServer,
    RTCPeerConnection,
    RTCSessionDescription,
)

from music_assistant.constants import MASS_LOGGER_NAME

if TYPE_CHECKING:
    from collections.abc import Callable

LOGGER = logging.getLogger(f"{MASS_LOGGER_NAME}.remote_access")

# Reduce verbose logging from aiortc/aioice
logging.getLogger("aioice").setLevel(logging.WARNING)
logging.getLogger("aiortc").setLevel(logging.WARNING)


def generate_remote_id() -> str:
    """Generate a unique Remote ID in the format MA-XXXX-XXXX."""
    chars = string.ascii_uppercase + string.digits
    part1 = "".join(secrets.choice(chars) for _ in range(4))
    part2 = "".join(secrets.choice(chars) for _ in range(4))
    return f"MA-{part1}-{part2}"


@dataclass
class WebRTCSession:
    """Represents an active WebRTC session with a remote client."""

    session_id: str
    peer_connection: RTCPeerConnection
    data_channel: Any = None
    local_ws: Any = None
    message_queue: asyncio.Queue[str] = field(default_factory=asyncio.Queue)


class WebRTCGateway:
    """WebRTC Gateway for Music Assistant Remote Access.

    This gateway:
    1. Connects to a signaling server
    2. Registers with a unique Remote ID
    3. Handles incoming WebRTC connections from remote PWA clients
    4. Bridges WebRTC DataChannel messages to the local WebSocket API
    """

    def __init__(
        self,
        signaling_url: str = "wss://signaling.music-assistant.io/ws",
        local_ws_url: str = "ws://localhost:8095/ws",
        ice_servers: list[dict[str, Any]] | None = None,
        remote_id: str | None = None,
        on_remote_id_ready: Callable[[str], None] | None = None,
    ) -> None:
        """Initialize the WebRTC Gateway.

        :param signaling_url: WebSocket URL of the signaling server.
        :param local_ws_url: Local WebSocket URL to bridge to.
        :param ice_servers: List of ICE server configurations.
        :param remote_id: Optional Remote ID to use (generated if not provided).
        :param on_remote_id_ready: Callback when Remote ID is registered.
        """
        self.signaling_url = signaling_url
        self.local_ws_url = local_ws_url
        self.remote_id = remote_id or generate_remote_id()
        self.on_remote_id_ready = on_remote_id_ready
        self.logger = LOGGER

        self.ice_servers = ice_servers or [
            {"urls": "stun:stun.l.google.com:19302"},
            {"urls": "stun:stun1.l.google.com:19302"},
            {"urls": "stun:stun.cloudflare.com:3478"},
        ]

        self.sessions: dict[str, WebRTCSession] = {}
        self._signaling_ws: aiohttp.ClientWebSocketResponse | None = None
        self._signaling_session: aiohttp.ClientSession | None = None
        self._running = False
        self._reconnect_delay = 5
        self._max_reconnect_delay = 60
        self._current_reconnect_delay = 5
        self._run_task: asyncio.Task[None] | None = None
        self._is_connected = False

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
        self.logger.info("Starting WebRTC Gateway with Remote ID: %s", self.remote_id)
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

        # Close signaling connection
        if self._signaling_ws:
            await self._signaling_ws.close()
        if self._signaling_session:
            await self._signaling_session.close()

        # Cancel run task
        if self._run_task and not self._run_task.done():
            self._run_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._run_task

    async def _run(self) -> None:
        """Run the main loop with reconnection logic."""
        self.logger.debug("WebRTC Gateway _run() loop starting")
        while self._running:
            try:
                await self._connect_to_signaling()
                # Connection closed gracefully or with error
                self._is_connected = False
                if self._running:
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

            if self._running:
                await asyncio.sleep(self._current_reconnect_delay)
                # Exponential backoff with max limit
                self._current_reconnect_delay = min(
                    self._current_reconnect_delay * 2, self._max_reconnect_delay
                )

    async def _connect_to_signaling(self) -> None:
        """Connect to the signaling server."""
        self.logger.info("Connecting to signaling server: %s", self.signaling_url)
        # Create session with increased timeout for WebSocket connection
        timeout = aiohttp.ClientTimeout(
            total=None,  # No total timeout for WebSocket connections
            connect=30,  # 30 seconds to establish connection
            sock_connect=30,  # 30 seconds for socket connection
            sock_read=None,  # No timeout for reading (we handle ping/pong)
        )
        self._signaling_session = aiohttp.ClientSession(timeout=timeout)
        try:
            self._signaling_ws = await self._signaling_session.ws_connect(
                self.signaling_url,
                heartbeat=45,  # Send WebSocket ping every 45 seconds
                autoping=True,  # Automatically respond to pings
            )
            await self._register()
            self._is_connected = True
            # Reset reconnect delay on successful connection
            self._current_reconnect_delay = self._reconnect_delay
            self.logger.info("Connected to signaling server")

            # Message loop
            async for msg in self._signaling_ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    try:
                        await self._handle_signaling_message(json.loads(msg.data))
                    except Exception:
                        self.logger.exception("Error handling signaling message")
                elif msg.type == aiohttp.WSMsgType.CLOSED:
                    self.logger.warning("Signaling server closed connection: %s", msg.extra)
                    break
                elif msg.type == aiohttp.WSMsgType.ERROR:
                    self.logger.error("WebSocket error: %s", msg.extra)
                    break

            self.logger.info("Message loop exited normally")
        except TimeoutError:
            self.logger.error("Timeout connecting to signaling server")
        except aiohttp.ClientError as err:
            self.logger.error("Failed to connect to signaling server: %s", err)
        finally:
            self._is_connected = False
            if self._signaling_session:
                await self._signaling_session.close()
                self._signaling_session = None
            self._signaling_ws = None

    async def _register(self) -> None:
        """Register with the signaling server."""
        if self._signaling_ws:
            registration_msg = {
                "type": "register-server",
                "remoteId": self.remote_id,
            }
            self.logger.info(
                "Sending registration to signaling server with Remote ID: %s",
                self.remote_id,
            )
            self.logger.debug("Registration message: %s", registration_msg)
            await self._signaling_ws.send_json(registration_msg)
            self.logger.debug("Registration message sent successfully")
        else:
            self.logger.warning("Cannot register: signaling websocket is not connected")

    async def _handle_signaling_message(self, message: dict[str, Any]) -> None:
        """Handle incoming signaling messages.

        :param message: The signaling message.
        """
        msg_type = message.get("type")
        self.logger.debug("Received signaling message: %s - Full message: %s", msg_type, message)

        if msg_type == "ping":
            # Respond to ping with pong
            if self._signaling_ws:
                await self._signaling_ws.send_json({"type": "pong"})
        elif msg_type == "pong":
            # Server responded to our ping, connection is alive
            pass
        elif msg_type == "registered":
            self.logger.info("Registered with signaling server as: %s", message.get("remoteId"))
            if self.on_remote_id_ready:
                self.on_remote_id_ready(self.remote_id)
        elif msg_type == "error":
            self.logger.error(
                "Signaling server error: %s",
                message.get("message", "Unknown error"),
            )
        elif msg_type == "client-connected":
            session_id = message.get("sessionId")
            if session_id:
                await self._create_session(session_id)
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
        config = RTCConfiguration(
            iceServers=[RTCIceServer(**server) for server in self.ice_servers]
        )
        pc = RTCPeerConnection(configuration=config)
        session = WebRTCSession(session_id=session_id, peer_connection=pc)
        self.sessions[session_id] = session

        @pc.on("datachannel")
        def on_datachannel(channel: Any) -> None:
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
        sdp = offer.get("sdp")
        sdp_type = offer.get("type")
        if not sdp or not sdp_type:
            self.logger.error("Invalid offer data: missing sdp or type")
            return
        await pc.setRemoteDescription(
            RTCSessionDescription(
                sdp=str(sdp),
                type=str(sdp_type),
            )
        )
        answer = await pc.createAnswer()
        await pc.setLocalDescription(answer)
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

    async def _handle_ice_candidate(self, session_id: str, candidate: dict[str, Any]) -> None:
        """Handle incoming ICE candidate.

        :param session_id: The session ID.
        :param candidate: The ICE candidate data.
        """
        session = self.sessions.get(session_id)
        if not session or not candidate:
            return

        candidate_str = candidate.get("candidate")
        sdp_mid = candidate.get("sdpMid")
        sdp_mline_index = candidate.get("sdpMLineIndex")

        if not candidate_str:
            return

        # Create RTCIceCandidate from the SDP string
        try:
            ice_candidate = RTCIceCandidate(
                component=1,
                foundation="",
                ip="",
                port=0,
                priority=0,
                protocol="udp",
                type="host",
                sdpMid=str(sdp_mid) if sdp_mid else None,
                sdpMLineIndex=int(sdp_mline_index) if sdp_mline_index is not None else None,
            )
            # Parse the candidate string to populate the fields
            ice_candidate.candidate = str(candidate_str)  # type: ignore[attr-defined]
            await session.peer_connection.addIceCandidate(ice_candidate)
        except Exception:
            self.logger.exception("Failed to add ICE candidate: %s", candidate)

    async def _setup_data_channel(self, session: WebRTCSession) -> None:
        """Set up data channel and bridge to local WebSocket.

        :param session: The WebRTC session.
        """
        channel = session.data_channel
        if not channel:
            return
        local_session = aiohttp.ClientSession()
        try:
            session.local_ws = await local_session.ws_connect(self.local_ws_url)
            loop = asyncio.get_event_loop()
            asyncio.create_task(self._forward_to_local(session))
            asyncio.create_task(self._forward_from_local(session, local_session))

            @channel.on("message")  # type: ignore[misc]
            def on_message(message: str) -> None:
                # Called from aiortc thread, use call_soon_threadsafe
                loop.call_soon_threadsafe(session.message_queue.put_nowait, message)

            @channel.on("close")  # type: ignore[misc]
            def on_close() -> None:
                # Called from aiortc thread, use call_soon_threadsafe to schedule task
                asyncio.run_coroutine_threadsafe(self._close_session(session.session_id), loop)

        except Exception:
            self.logger.exception("Failed to connect to local WebSocket")
            await local_session.close()

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
        except Exception:
            self.logger.exception("Error forwarding to local WebSocket")

    async def _forward_from_local(
        self, session: WebRTCSession, local_session: aiohttp.ClientSession
    ) -> None:
        """Forward messages from local WebSocket to WebRTC DataChannel.

        :param session: The WebRTC session.
        :param local_session: The aiohttp client session.
        """
        try:
            async for msg in session.local_ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    if session.data_channel and session.data_channel.readyState == "open":
                        session.data_channel.send(msg.data)
                elif msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSED):
                    break
        except Exception:
            self.logger.exception("Error forwarding from local WebSocket")
        finally:
            await local_session.close()

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
            # Create a new HTTP client session for this request
            async with (
                aiohttp.ClientSession() as http_session,
                http_session.request(method, local_http_url, headers=headers) as response,
            ):
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
        if session.local_ws and not session.local_ws.closed:
            await session.local_ws.close()
        if session.data_channel:
            session.data_channel.close()
        await session.peer_connection.close()
