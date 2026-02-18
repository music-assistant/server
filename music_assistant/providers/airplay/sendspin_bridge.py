"""
Sendspin Bridge for AirPlay - allows Sendspin to stream to AirPlay devices.

This module enables AirPlay devices to be controlled via the Sendspin protocol.
Sendspin handles all synchronization and timing - AirPlay is just the output.

The bridge:
1. Registers AirPlay players as external Sendspin clients (using MAC as client_id)
2. The Sendspin provider creates a SendspinPlayer for this external client
3. Protocol linking matches the SendspinPlayer with the AirPlayPlayer via MAC
4. When grouped, Sendspin handles timing/sync, AirPlay streams audio

Audio flow:
Sendspin PushStream → BridgePlayerRole.on_audio_chunk → AirPlay CLI process
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import suppress
from typing import TYPE_CHECKING, cast

from aiosendspin.models.core import ClientHelloPayload
from aiosendspin.models.core import DeviceInfo as SendspinDeviceInfo
from aiosendspin.models.player import ClientHelloPlayerSupport, SupportedAudioFormat
from aiosendspin.models.types import AudioCodec, PlayerCommand, Roles
from aiosendspin.server.roles import AudioRequirements, Role

from music_assistant.helpers.util import is_valid_mac_address

from .constants import StreamingProtocol
from .helpers import player_id_to_mac_address
from .protocols.airplay2 import AirPlay2Stream
from .protocols.raop import RaopStream

if TYPE_CHECKING:
    from aiosendspin.server import SendspinClient, SendspinServer
    from aiosendspin.server.roles import AudioChunk
    from aiosendspin.server.server import ExternalStreamStartRequest

    from music_assistant.providers.sendspin.provider import SendspinProvider

    from .player import AirPlayPlayer
    from .protocols._protocol import AirPlayProtocol
    from .provider import AirPlayProvider


def get_bridge_client_id(airplay_player: AirPlayPlayer) -> str | None:
    """Get the Sendspin bridge client ID for an AirPlay player.

    Uses the MAC address as the client_id to enable protocol linking.
    The Sendspin provider will create a SendspinPlayer with this client_id.

    :param airplay_player: The AirPlay player to bridge.
    :return: The MAC address for use as client_id, or None if not available.
    """
    mac = player_id_to_mac_address(airplay_player.player_id)
    if is_valid_mac_address(mac):
        return mac
    return None


class BridgePlayerRole(Role):
    """Custom Sendspin player role for the AirPlay bridge.

    This role receives audio from Sendspin's PushStream and forwards it
    to the AirPlay device via a callback. It bypasses the normal WebSocket
    audio delivery since external players don't have a WebSocket connection.
    """

    def __init__(
        self,
        client: SendspinClient,
        on_audio_chunk: Callable[[AudioChunk], None],
    ) -> None:
        """Initialize the bridge player role.

        :param client: The Sendspin client this role belongs to.
        :param on_audio_chunk: Callback to receive audio chunks.
        """
        self._client = client
        self._on_audio_chunk_cb = on_audio_chunk
        self._audio_requirements: AudioRequirements | None = None

    @property
    def role_id(self) -> str:
        """Return role identifier."""
        return "player@bridge"

    @property
    def role_family(self) -> str:
        """Return role family name."""
        return "player"

    def setup_audio_requirements(self) -> None:
        """Set up audio requirements for 44.1kHz 16-bit stereo PCM."""
        self._audio_requirements = AudioRequirements(
            sample_rate=44100,
            bit_depth=16,
            channels=2,
            transformer=None,  # Raw PCM, no encoding
        )

    def get_audio_requirements(self) -> AudioRequirements | None:
        """Return audio requirements for PushStream."""
        return self._audio_requirements

    def on_audio_chunk(self, chunk: AudioChunk) -> None:
        """Receive audio chunk from PushStream and forward to callback."""
        self._on_audio_chunk_cb(chunk)

    def on_connect(self) -> None:
        """Handle connection (no-op for bridge)."""

    def on_disconnect(self) -> None:
        """Handle disconnection (no-op for bridge)."""

    def has_connection(self) -> bool:
        """Return True to indicate bridge is "connected" for audio purposes."""
        return True


class SendspinAirPlayBridge:
    """Manages the Sendspin to AirPlay bridge for a single player.

    This class handles:
    1. Registering the AirPlay player as an external Sendspin client
    2. Creating a BridgePlayerRole to receive audio from PushStream
    3. Streaming audio to the AirPlay device via RAOP/AirPlay2 protocol
    """

    def __init__(
        self,
        provider: AirPlayProvider,
        airplay_player: AirPlayPlayer,
        sendspin_server: SendspinServer,
    ) -> None:
        """Initialize the bridge.

        :param provider: The AirPlay provider instance.
        :param airplay_player: The AirPlay player to bridge.
        :param sendspin_server: The Sendspin server to register with.
        """
        self.provider = provider
        self.mass = provider.mass
        self.airplay_player = airplay_player
        self.sendspin_server = sendspin_server
        self.logger = provider.logger.getChild(f"bridge.{airplay_player.player_id}")

        self._sendspin_client: SendspinClient | None = None
        self._bridge_client_id: str | None = None
        self._bridge_role: BridgePlayerRole | None = None
        self._protocol: AirPlayProtocol | None = None
        self._is_streaming = False
        self._lock = asyncio.Lock()

    @property
    def is_registered(self) -> bool:
        """Return whether the bridge is registered with Sendspin."""
        return self._sendspin_client is not None

    async def start(self) -> None:
        """Register the AirPlay player as an external Sendspin client."""
        self._bridge_client_id = get_bridge_client_id(self.airplay_player)
        if not self._bridge_client_id:
            self.logger.warning(
                "Cannot create Sendspin bridge for %s: no valid MAC address",
                self.airplay_player.display_name,
            )
            return

        hello = ClientHelloPayload(
            client_id=self._bridge_client_id,
            name=f"{self.airplay_player.display_name} (AirPlay)",
            version=1,
            supported_roles=[Roles.PLAYER.value],
            device_info=SendspinDeviceInfo(
                product_name=self.airplay_player.device_info.model,
                manufacturer=self.airplay_player.device_info.manufacturer,
            ),
            player_support=ClientHelloPlayerSupport(
                supported_formats=[
                    SupportedAudioFormat(
                        codec=AudioCodec.PCM,
                        channels=2,
                        sample_rate=44100,
                        bit_depth=16,
                    )
                ],
                buffer_capacity=100_000,
                supported_commands=[PlayerCommand.VOLUME, PlayerCommand.MUTE],
            ),
        )

        self.logger.debug(
            "Registering Sendspin bridge for %s with client_id=%s",
            self.airplay_player.display_name,
            self._bridge_client_id,
        )

        self._sendspin_client = self.sendspin_server.register_external_player(
            hello, on_stream_start=self._on_stream_start
        )

        self.logger.info(
            "Sendspin bridge registered for %s (client_id=%s)",
            self.airplay_player.display_name,
            self._bridge_client_id,
        )

    async def stop(self) -> None:
        """Stop and unregister the Sendspin bridge."""
        async with self._lock:
            await self._stop_streaming()
            if self._sendspin_client and self._bridge_client_id:
                await self.sendspin_server.remove_client(self._bridge_client_id)
                self._sendspin_client = None
                self._bridge_role = None

        self.logger.debug("Sendspin bridge stopped for %s", self.airplay_player.display_name)

    def _on_stream_start(self, request: ExternalStreamStartRequest) -> None:
        """Handle stream start request from Sendspin server.

        Called when Sendspin wants to play audio to this bridge player.
        """
        self.logger.debug(
            "Sendspin stream start request for %s (reason=%s)",
            self.airplay_player.display_name,
            request.connection_reason,
        )
        self.mass.create_task(self._handle_stream_start(request))

    async def _handle_stream_start(self, request: ExternalStreamStartRequest) -> None:
        """Handle the stream start request asynchronously."""
        async with self._lock:
            await self._stop_streaming()
            client = request.server.get_client(request.client_id)
            if not client:
                self.logger.error("Client not found for stream start")
                return

            self._bridge_role = BridgePlayerRole(client, self._on_audio_chunk)
            self._bridge_role.setup_audio_requirements()
            client._roles[self._bridge_role.role_id] = self._bridge_role

            group = client.group
            if group and group.has_active_stream:
                push_stream = group._push_stream
                if push_stream and not push_stream.is_stopped:
                    push_stream.on_role_join(self._bridge_role)
                    self.logger.info(
                        "Bridge role joined PushStream for %s",
                        self.airplay_player.display_name,
                    )

            self._is_streaming = True

    def _on_audio_chunk(self, chunk: AudioChunk) -> None:
        """Handle audio chunk from Sendspin PushStream."""
        if not self._is_streaming:
            return
        if self._protocol and self._protocol.running and self._protocol._cli_proc:
            self.mass.create_task(self._protocol._cli_proc.write(chunk.data))

    async def start_airplay_stream(self, start_ntp: int) -> None:
        """Start the AirPlay protocol stream.

        :param start_ntp: NTP timestamp when playback should start.
        """
        async with self._lock:
            if self._protocol:
                await self._protocol.stop(force=True)
            if self.airplay_player.protocol == StreamingProtocol.AIRPLAY2:
                self._protocol = AirPlay2Stream(self.airplay_player)
            else:
                self._protocol = RaopStream(self.airplay_player)

            await self._protocol.start(start_ntp)
            await self._protocol.wait_for_connection()

            self.logger.info(
                "AirPlay stream started for %s (NTP=%s)",
                self.airplay_player.display_name,
                start_ntp,
            )

    async def _stop_streaming(self) -> None:
        """Stop streaming (internal, called with lock held)."""
        self._is_streaming = False
        if self._sendspin_client and self._bridge_role:
            self._sendspin_client._roles.pop(self._bridge_role.role_id, None)
            self._bridge_role = None
        if self._protocol:
            await self._protocol.stop(force=True)
            self._protocol = None


class SendspinBridgeManager:
    """Manages Sendspin bridges for all AirPlay players."""

    def __init__(self, provider: AirPlayProvider) -> None:
        """Initialize the bridge manager.

        :param provider: The AirPlay provider instance.
        """
        self.provider = provider
        self.mass = provider.mass
        self.logger = provider.logger.getChild("bridge_manager")
        self._bridges: dict[str, SendspinAirPlayBridge] = {}
        self._lock = asyncio.Lock()

    @property
    def sendspin_provider(self) -> SendspinProvider | None:
        """Get the Sendspin provider if available."""
        return cast(
            "SendspinProvider | None",
            self.mass.get_provider("sendspin"),
        )

    @property
    def sendspin_server(self) -> SendspinServer | None:
        """Get the Sendspin server if available."""
        if provider := self.sendspin_provider:
            return provider.server_api
        return None

    async def setup_bridge(self, airplay_player: AirPlayPlayer) -> None:
        """Set up a Sendspin bridge for an AirPlay player."""
        async with self._lock:
            player_id = airplay_player.player_id

            sendspin_server = self.sendspin_server
            if not sendspin_server:
                self.logger.debug(
                    "Sendspin provider not available, skipping bridge for %s",
                    airplay_player.display_name,
                )
                return

            if player_id in self._bridges:
                self.logger.debug("Bridge already exists for %s", airplay_player.display_name)
                return

            bridge = SendspinAirPlayBridge(self.provider, airplay_player, sendspin_server)
            self._bridges[player_id] = bridge

            await bridge.start()

            self.logger.info("Sendspin bridge created for %s", airplay_player.display_name)

    async def remove_bridge(self, airplay_player_id: str) -> None:
        """Remove the Sendspin bridge for an AirPlay player."""
        async with self._lock:
            if bridge := self._bridges.pop(airplay_player_id, None):
                await bridge.stop()

            self.logger.debug("Sendspin bridge removed for AirPlay player %s", airplay_player_id)

    async def stop_all(self) -> None:
        """Stop all Sendspin bridges."""
        async with self._lock:
            for bridge in list(self._bridges.values()):
                with suppress(Exception):
                    await bridge.stop()
            self._bridges.clear()

        self.logger.debug("All Sendspin bridges stopped")

    def get_bridge(self, airplay_player_id: str) -> SendspinAirPlayBridge | None:
        """Get the bridge for an AirPlay player."""
        return self._bridges.get(airplay_player_id)
