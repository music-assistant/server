"""Player Provider for Sendspin."""

from __future__ import annotations

import asyncio
import contextlib
import secrets
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast

import aiohttp
from aiortc import RTCConfiguration, RTCIceServer, RTCPeerConnection, RTCSessionDescription
from aiortc.sdp import candidate_from_sdp
from aiosendspin.server import ClientAddedEvent, ClientRemovedEvent, SendspinEvent, SendspinServer
from music_assistant_models.enums import ProviderFeature

from music_assistant.controllers.webserver.helpers.auth_middleware import get_current_user
from music_assistant.mass import MusicAssistant
from music_assistant.models.player_provider import PlayerProvider
from music_assistant.providers.sendspin.player import SendspinPlayer

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.provider import ProviderManifest


@dataclass
class SendspinWebRTCSession:
    """Represents an active WebRTC session for a Sendspin client."""

    session_id: str
    peer_connection: RTCPeerConnection
    data_channel: Any = None
    sendspin_ws: aiohttp.ClientWebSocketResponse | None = None
    forward_task: asyncio.Task[None] | None = None
    message_queue: asyncio.Queue[str | bytes] = field(default_factory=asyncio.Queue)


class SendspinProvider(PlayerProvider):
    """Player Provider for Sendspin."""

    server_api: SendspinServer
    unregister_cbs: list[Callable[[], None]]
    _webrtc_sessions: dict[str, SendspinWebRTCSession]
    _pending_unregisters: dict[str, asyncio.Event]

    def __init__(
        self, mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
    ) -> None:
        """Initialize a new Sendspin player provider."""
        super().__init__(mass, manifest, config)
        self.server_api = SendspinServer(
            self.mass.loop, mass.server_id, "Music Assistant", self.mass.http_session
        )
        self._webrtc_sessions = {}
        self._pending_unregisters = {}
        self.unregister_cbs = [
            self.server_api.add_event_listener(self.event_cb),
            # WebRTC signaling commands for Sendspin connections
            # this is used to establish WebRTC DataChannels with Sendspin clients
            # for example the WebPlayer in the Music Assistant frontend or supported (mobile) apps
            self.mass.register_api_command(
                "sendspin/connection_info", self.handle_get_connection_info
            ),
            self.mass.register_api_command("sendspin/connect", self.handle_webrtc_connect),
            self.mass.register_api_command("sendspin/ice", self.handle_webrtc_ice),
            self.mass.register_api_command("sendspin/disconnect", self.handle_webrtc_disconnect),
            self.mass.register_api_command("sendspin/ice_servers", self.handle_get_ice_servers),
        ]

    async def event_cb(self, server: SendspinServer, event: SendspinEvent) -> None:
        """Event callback registered to the sendspin server."""
        self.logger.debug("Received SendspinEvent: %s", event)
        match event:
            case ClientAddedEvent(client_id):
                # Wait for any pending unregister to complete before registering
                # This prevents a race condition where a slow unregister removes
                # a newly registered player after a quick reconnect
                if pending_event := self._pending_unregisters.get(client_id):
                    self.logger.debug(
                        "Waiting for pending unregister of %s before registering", client_id
                    )
                    await pending_event.wait()
                player = SendspinPlayer(self, client_id)
                self.logger.debug("Client %s connected", client_id)
                await self.mass.players.register(player)
            case ClientRemovedEvent(client_id):
                self.logger.debug("Client %s disconnected", client_id)
                unregister_event = asyncio.Event()
                self._pending_unregisters[client_id] = unregister_event
                try:
                    await self.mass.players.unregister(client_id)
                finally:
                    self._pending_unregisters.pop(client_id, None)
                    unregister_event.set()
            case _:
                self.logger.error("Unknown sendspin event: %s", event)

    @property
    def supported_features(self) -> set[ProviderFeature]:
        """Return the features supported by this Provider."""
        return {
            ProviderFeature.SYNC_PLAYERS,
        }

    async def loaded_in_mass(self) -> None:
        """Call after the provider has been loaded."""
        await super().loaded_in_mass()
        # Start server for handling incoming Sendspin connections from clients
        # and mDNS discovery of new clients
        await self.server_api.start_server(
            port=8927,
            host=self.mass.streams.bind_ip,
            advertise_host=cast("str", self.mass.streams.publish_ip),
        )

    async def unload(self, is_removed: bool = False) -> None:
        """
        Handle unload/close of the provider.

        Called when provider is deregistered (e.g. MA exiting or config reloading).
        is_removed will be set to True when the provider is removed from the configuration.
        """
        # Close all WebRTC sessions
        for session_id in list(self._webrtc_sessions.keys()):
            await self._close_webrtc_session(session_id)

        # Stop the Sendspin server
        await self.server_api.close()

        for cb in self.unregister_cbs:
            cb()
        self.unregister_cbs = []
        for player in self.players:
            self.logger.debug("Unloading player %s", player.name)
            await self.mass.players.unregister(player.player_id)

    async def handle_webrtc_connect(self, offer: dict[str, str]) -> dict[str, Any]:
        """
        Handle WebRTC connection request for Sendspin.

        This command is called via an authenticated API connection.
        The client sends a WebRTC offer and receives an answer.

        :param offer: WebRTC offer with 'sdp' and 'type' fields.
        :return: Dictionary with session_id and WebRTC answer.
        """
        session_id = secrets.token_urlsafe(16)
        self.logger.debug("Creating Sendspin WebRTC session %s", session_id)

        # Get ICE servers (may include HA Cloud TURN servers)
        ice_servers = await self.mass.webserver.remote_access.get_ice_servers()
        self.logger.debug(
            "Creating Sendspin WebRTC session with %d ICE servers",
            len(ice_servers),
        )

        # Create peer connection with ICE servers
        config = RTCConfiguration(iceServers=[RTCIceServer(**server) for server in ice_servers])
        pc = RTCPeerConnection(configuration=config)

        session = SendspinWebRTCSession(
            session_id=session_id,
            peer_connection=pc,
        )
        self._webrtc_sessions[session_id] = session

        # Track ICE candidates to send back
        ice_candidates: list[dict[str, Any]] = []

        @pc.on("icecandidate")
        def on_ice_candidate(candidate: Any) -> None:
            if candidate:
                ice_candidates.append(
                    {
                        "candidate": candidate.candidate,
                        "sdpMid": candidate.sdpMid,
                        "sdpMLineIndex": candidate.sdpMLineIndex,
                    }
                )

        @pc.on("datachannel")
        def on_datachannel(channel: Any) -> None:
            self.logger.debug("Sendspin DataChannel opened for session %s", session_id)
            session.data_channel = channel
            asyncio.create_task(self._setup_sendspin_bridge(session))

        @pc.on("connectionstatechange")
        async def on_connection_state_change() -> None:
            self.logger.debug(
                "Sendspin WebRTC connection state: %s for session %s",
                pc.connectionState,
                session_id,
            )
            if pc.connectionState in ("failed", "closed", "disconnected"):
                await self._close_webrtc_session(session_id)

        # Set remote description (offer from client)
        await pc.setRemoteDescription(RTCSessionDescription(sdp=offer["sdp"], type=offer["type"]))

        # Create and set local description (answer)
        answer = await pc.createAnswer()
        await pc.setLocalDescription(answer)

        # Wait for ICE gathering to complete before sending the answer
        # This is required because aiortc doesn't support trickle ICE -
        # candidates are only embedded in the SDP after gathering is complete
        # See: https://github.com/aiortc/aiortc/issues/1344
        gather_timeout = 30  # seconds
        gather_start = asyncio.get_event_loop().time()
        while pc.iceGatheringState != "complete":
            if pc.connectionState in ("closed", "failed"):
                self.logger.debug(
                    "Sendspin session %s closed during ICE gathering, aborting",
                    session_id,
                )
                raise RuntimeError("Connection closed during ICE gathering")
            if asyncio.get_event_loop().time() - gather_start > gather_timeout:
                self.logger.warning(
                    "Sendspin session %s ICE gathering timeout after %ds, sending answer anyway",
                    session_id,
                    gather_timeout,
                )
                break
            await asyncio.sleep(0.1)

        self.logger.debug(
            "Sendspin session %s ICE gathering completed in %.1fs",
            session_id,
            asyncio.get_event_loop().time() - gather_start,
        )

        return {
            "session_id": session_id,
            "answer": {
                "sdp": pc.localDescription.sdp,
                "type": pc.localDescription.type,
            },
            "ice_candidates": ice_candidates,
            # Include local WebSocket URL for direct connection attempts
            # Frontend can try this first before falling back to WebRTC
            "local_ws_url": f"ws://{self.mass.streams.publish_ip}:8927/sendspin",
        }

    async def handle_webrtc_ice(
        self, session_id: str, candidate: dict[str, Any]
    ) -> dict[str, bool]:
        """
        Handle ICE candidate from client.

        :param session_id: The WebRTC session ID.
        :param candidate: ICE candidate with 'candidate', 'sdpMid', 'sdpMLineIndex'.
        :return: Dictionary with success status.
        """
        session = self._webrtc_sessions.get(session_id)
        if not session:
            self.logger.warning("ICE candidate for unknown session %s", session_id)
            return {"success": False}

        try:
            candidate_str = candidate.get("candidate")
            sdp_mid = candidate.get("sdpMid")
            sdp_mline_index = candidate.get("sdpMLineIndex")

            if not candidate_str:
                return {"success": False}

            # Parse the ICE candidate using aiortc's candidate_from_sdp function
            # Browser sends "candidate:..." format, candidate_from_sdp expects without prefix
            if candidate_str.startswith("candidate:"):
                sdp_candidate_str = candidate_str[len("candidate:") :]
            else:
                sdp_candidate_str = candidate_str

            ice_candidate = candidate_from_sdp(sdp_candidate_str)
            ice_candidate.sdpMid = str(sdp_mid) if sdp_mid else None
            ice_candidate.sdpMLineIndex = (
                int(sdp_mline_index) if sdp_mline_index is not None else None
            )

            await session.peer_connection.addIceCandidate(ice_candidate)
            return {"success": True}
        except Exception as err:
            self.logger.exception("Failed to add ICE candidate: %s", err)
            return {"success": False}

    async def handle_webrtc_disconnect(self, session_id: str) -> dict[str, bool]:
        """
        Handle WebRTC disconnect request.

        :param session_id: The WebRTC session ID to disconnect.
        :return: Dictionary with success status.
        """
        await self._close_webrtc_session(session_id)
        return {"success": True}

    async def handle_get_ice_servers(self) -> list[dict[str, str]]:
        """
        Get ICE servers for Sendspin WebRTC connections.

        Returns HA Cloud TURN servers if available, otherwise returns public STUN servers.

        :return: List of ICE server configurations.
        """
        return await self.mass.webserver.remote_access.get_ice_servers()

    async def handle_get_connection_info(self, client_id: str | None = None) -> dict[str, Any]:
        """
        Get connection info for Sendspin.

        Returns the local WebSocket URL for direct connection attempts,
        and ICE servers for WebRTC fallback.

        The frontend should try the local WebSocket URL first for lower latency,
        and fall back to WebRTC if the direct connection fails.

        :param client_id: Optional Sendspin client ID for auto-whitelisting.
        :return: Dictionary with local_ws_url and ice_servers.
        """
        # Auto-whitelist the player for users with player filters enabled
        # This allows users with restricted player access to still use the web player
        if client_id and (user := get_current_user()):
            if user.player_filter and client_id not in user.player_filter:
                self.logger.debug(
                    "Auto-whitelisting Sendspin player %s for user %s",
                    client_id,
                    user.username,
                )
                new_filter = [*user.player_filter, client_id]
                await self.mass.webserver.auth.update_user_filters(
                    user, player_filter=new_filter, provider_filter=None
                )
                user.player_filter = new_filter

        return {
            "local_ws_url": f"ws://{self.mass.streams.publish_ip}:8927/sendspin",
            "ice_servers": await self.mass.webserver.remote_access.get_ice_servers(),
        }

    async def _close_webrtc_session(self, session_id: str) -> None:
        """Close a WebRTC session and clean up resources."""
        session = self._webrtc_sessions.pop(session_id, None)
        if not session:
            return

        self.logger.debug("Closing Sendspin WebRTC session %s", session_id)

        # Cancel forward task
        if session.forward_task and not session.forward_task.done():
            session.forward_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await session.forward_task

        # Close Sendspin WebSocket
        if session.sendspin_ws and not session.sendspin_ws.closed:
            await session.sendspin_ws.close()

        # Close peer connection
        await session.peer_connection.close()

    async def _forward_to_sendspin(self, session: SendspinWebRTCSession) -> None:
        """Forward messages from queue to Sendspin WebSocket."""
        try:
            while session.sendspin_ws and not session.sendspin_ws.closed:
                msg = await session.message_queue.get()
                if session.sendspin_ws and not session.sendspin_ws.closed:
                    if isinstance(msg, bytes):
                        await session.sendspin_ws.send_bytes(msg)
                    else:
                        await session.sendspin_ws.send_str(msg)
        except asyncio.CancelledError:
            raise
        except Exception:
            self.logger.exception("Error forwarding to Sendspin")

    async def _forward_from_sendspin(self, session: SendspinWebRTCSession, channel: Any) -> None:
        """Forward messages from Sendspin WebSocket to DataChannel."""
        try:
            ws = session.sendspin_ws
            if ws is None:
                return
            async for msg in ws:
                if msg.type in {aiohttp.WSMsgType.TEXT, aiohttp.WSMsgType.BINARY}:
                    if channel and channel.readyState == "open":
                        channel.send(msg.data)
                elif msg.type in {aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSED}:
                    break
        except asyncio.CancelledError:
            raise
        except Exception:
            self.logger.exception("Error forwarding from Sendspin")

    async def _setup_sendspin_bridge(self, session: SendspinWebRTCSession) -> None:
        """Set up the bridge between WebRTC DataChannel and internal Sendspin server."""
        channel = session.data_channel
        if not channel:
            return

        loop = asyncio.get_event_loop()

        # Register message handler FIRST to capture any messages sent immediately
        @channel.on("message")  # type: ignore[misc]
        def on_message(message: str | bytes) -> None:
            if session.forward_task and not session.forward_task.done():
                loop.call_soon_threadsafe(session.message_queue.put_nowait, message)
            else:
                # Queue message even if forward task not started yet
                session.message_queue.put_nowait(message)

        @channel.on("close")  # type: ignore[misc]
        def on_close() -> None:
            asyncio.run_coroutine_threadsafe(self._close_webrtc_session(session.session_id), loop)

        try:
            # Connect to internal Sendspin server
            sendspin_url = "ws://127.0.0.1:8927/sendspin"
            session.sendspin_ws = await self.mass.http_session.ws_connect(sendspin_url)
            self.logger.debug(
                "Connected to internal Sendspin server for session %s", session.session_id
            )

            # Start forwarding tasks
            session.forward_task = asyncio.create_task(self._forward_to_sendspin(session))
            asyncio.create_task(self._forward_from_sendspin(session, channel))

            self.logger.debug("Sendspin bridge established for session %s", session.session_id)

        except Exception:
            self.logger.exception(
                "Failed to setup Sendspin bridge for session %s", session.session_id
            )
            await self._close_webrtc_session(session.session_id)
