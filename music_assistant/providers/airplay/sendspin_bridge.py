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
import time
from collections.abc import Callable
from contextlib import suppress
from typing import TYPE_CHECKING, cast

from aiosendspin.models.core import ClientHelloPayload
from aiosendspin.models.core import DeviceInfo as SendspinDeviceInfo
from aiosendspin.models.player import ClientHelloPlayerSupport, SupportedAudioFormat
from aiosendspin.models.types import AudioCodec, PlayerCommand
from aiosendspin.server.roles import AudioRequirements, Role
from aiosendspin.server.roles.registry import register_role

from music_assistant.helpers.util import is_valid_mac_address
from music_assistant.mass import LOGGER

from .constants import StreamingProtocol
from .helpers import player_id_to_mac_address, unix_time_to_ntp
from .protocols.airplay2 import AirPlay2Stream
from .protocols.raop import RaopStream

if TYPE_CHECKING:
    from aiosendspin.server import ExternalStreamStartRequest, SendspinClient, SendspinServer
    from aiosendspin.server.roles import AudioChunk

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

    Created by the role factory registry. After creation, the bridge must
    call set_callbacks() to wire up audio/volume/stream callbacks.
    """

    def __init__(self, client: SendspinClient) -> None:
        """Initialize the bridge player role.

        :param client: The Sendspin client this role belongs to.
        """
        self._client = client
        self._on_audio_chunk_cb: Callable[[AudioChunk], None] | None = None
        self._on_volume_change_cb: Callable[[int, bool], None] | None = None
        self._on_stream_start_cb: Callable[[], None] | None = None
        self._on_stream_end_cb: Callable[[], None] | None = None
        self._audio_requirements: AudioRequirements | None = None
        self._volume: int = 100
        self._muted: bool = False

    def set_callbacks(
        self,
        *,
        on_audio_chunk: Callable[[AudioChunk], None],
        on_volume_change: Callable[[int, bool], None],
        on_stream_start: Callable[[], None],
        on_stream_end: Callable[[], None],
        initial_volume: int = 100,
    ) -> None:
        """Wire up bridge callbacks after role creation.

        :param on_audio_chunk: Callback to receive audio chunks.
        :param on_volume_change: Callback when volume or mute state changes.
        :param on_stream_start: Callback when the stream starts.
        :param on_stream_end: Callback when the stream ends.
        :param initial_volume: Initial volume level (0-100).
        """
        self._on_audio_chunk_cb = on_audio_chunk
        self._on_volume_change_cb = on_volume_change
        self._on_stream_start_cb = on_stream_start
        self._on_stream_end_cb = on_stream_end
        self._volume = initial_volume

    @property
    def role_id(self) -> str:
        """Return role identifier."""
        return "player@_airplay_bridge"

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

    def get_player_volume(self) -> int | None:
        """Return current volume level."""
        return self._volume

    def get_player_muted(self) -> bool | None:
        """Return current mute state."""
        return self._muted

    def set_player_volume(self, volume: int) -> None:
        """Set volume and notify bridge."""
        self._volume = volume
        if self._on_volume_change_cb:
            self._on_volume_change_cb(volume, self._muted)

    def set_player_mute(self, muted: bool) -> None:
        """Set mute state and notify bridge."""
        self._muted = muted
        if self._on_volume_change_cb:
            self._on_volume_change_cb(self._volume, muted)

    def on_audio_chunk(self, chunk: AudioChunk) -> None:
        """Receive audio chunk from PushStream and forward to callback."""
        if self._on_audio_chunk_cb:
            self._on_audio_chunk_cb(chunk)

    def on_connect(self) -> None:
        """Subscribe to PlayerGroupRole on attach."""
        self._subscribe_to_group_role()

    def on_disconnect(self) -> None:
        """Unsubscribe from PlayerGroupRole on detach."""
        self._unsubscribe_from_group_role()

    def has_connection(self) -> bool:
        """Return True to indicate bridge is "connected" for audio purposes."""
        return True

    def supports_preconnect_audio(self) -> bool:
        """Return True — AirPlay bridge can receive audio before the stream starts."""
        return True

    def on_stream_start(self) -> None:
        """Log stream start and invoke callback."""
        LOGGER.debug("BridgePlayerRole stream started for client %s", self._client.client_id)
        if self._on_stream_start_cb:
            self._on_stream_start_cb()

    def on_stream_end(self) -> None:
        """Log stream end and invoke the stream-end callback."""
        LOGGER.debug("BridgePlayerRole stream ended for client %s", self._client.client_id)
        if self._on_stream_end_cb:
            self._on_stream_end_cb()


BRIDGE_ROLE_ID = "player@_airplay_bridge"

register_role(BRIDGE_ROLE_ID, lambda client: BridgePlayerRole(client=client))


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
        self._next_expected_timestamp_us: int | None = None
        self._write_queue: asyncio.Queue[bytes | None] = asyncio.Queue()
        self._writer_task: asyncio.Task[None] | None = None
        self._protocol_start_task: asyncio.Task[None] | None = None
        self._protocol_ready = asyncio.Event()
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
            supported_roles=[BRIDGE_ROLE_ID],
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
                buffer_capacity=1_000,
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

        # Role is created by register_external_player via the factory registry.
        # Retrieve it and wire up the bridge callbacks.
        roles = self._sendspin_client.roles_by_family("player")
        if roles:
            self._bridge_role = cast("BridgePlayerRole", roles[0])
            self._bridge_role.set_callbacks(
                on_audio_chunk=self._on_audio_chunk,
                on_volume_change=self._on_volume_change,
                on_stream_start=self._on_bridge_stream_start,
                on_stream_end=self._on_bridge_stream_end,
                initial_volume=self.airplay_player.volume_level or 100,
            )
            self._bridge_role.setup_audio_requirements()

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
        aiosendspin handles role lifecycle (on_connect, push stream join).
        We just need to reset local streaming state.
        """
        self.logger.debug(
            "Sendspin stream start request for %s (reason=%s)",
            self.airplay_player.display_name,
            request.connection_reason,
        )
        self._is_streaming = True
        self._next_expected_timestamp_us = None

    def _on_bridge_stream_start(self) -> None:
        """Start the writer task when the PushStream notifies us the stream has started.

        Called via the BridgePlayerRole.on_stream_start callback when the
        PushStream begins delivering audio chunks.
        """
        if self._writer_task is not None:
            return
        self._next_expected_timestamp_us = None
        self._writer_task = self.mass.create_task(self._cli_writer())
        self.logger.info(
            "Bridge writer started for %s, awaiting first chunk",
            self.airplay_player.display_name,
        )

    async def _start_protocol_from_chunk(self, chunk: AudioChunk) -> None:
        """Start the AirPlay protocol, deriving start_ntp from the first chunk's timestamp.

        :param chunk: The first audio chunk delivered by the PushStream.
        """
        future_s = (chunk.timestamp_us - time.monotonic() * 1_000_000) / 1_000_000
        start_ntp = unix_time_to_ntp(time.time() + future_s)

        if self.airplay_player.protocol == StreamingProtocol.AIRPLAY2:
            self._protocol = AirPlay2Stream(self.airplay_player)
        else:
            self._protocol = RaopStream(self.airplay_player)

        await self._protocol.start(start_ntp)
        self._protocol_ready.set()
        self.logger.info(
            "Bridge protocol started for %s (NTP=%s, lookahead=%.0fms)",
            self.airplay_player.display_name,
            start_ntp,
            future_s * 1000,
        )
        self.mass.create_task(self._wait_for_airplay_connection())

    async def _wait_for_airplay_connection(self) -> None:
        """Wait for AirPlay connection in the background and log the result."""
        if not self._protocol:
            return
        try:
            await self._protocol.wait_for_connection()
            self.logger.info(
                "AirPlay connection established for %s", self.airplay_player.display_name
            )
        except Exception as err:
            self.logger.warning(
                "AirPlay connection failed for %s: %s",
                self.airplay_player.display_name,
                err,
            )

    def _on_volume_change(self, volume: int, muted: bool) -> None:
        """Forward volume/mute changes to the AirPlay CLI."""
        effective_volume = 0 if muted else volume
        self.mass.create_task(self._send_volume_command(effective_volume))

    async def _send_volume_command(self, volume: int) -> None:
        """Send VOLUME command to the AirPlay CLI."""
        if self._protocol and self._protocol.running:
            await self._protocol.send_cli_command(f"VOLUME={volume}")

    def _on_bridge_stream_end(self) -> None:
        """Signal the writer task that the stream has ended."""
        self._is_streaming = False
        self._next_expected_timestamp_us = None
        self._write_queue.put_nowait(None)

    def _on_audio_chunk(self, chunk: AudioChunk) -> None:
        """Handle audio chunk from Sendspin PushStream."""
        if not self._is_streaming:
            return

        if self._protocol_start_task is None:
            self._protocol_start_task = self.mass.create_task(
                self._start_protocol_from_chunk(chunk)
            )

        if self._next_expected_timestamp_us is not None:
            gap_us = chunk.timestamp_us - self._next_expected_timestamp_us
            if gap_us > 1_000:
                # Forward gap: fill with silence, capped at 2 seconds to avoid huge fills on seeks
                fill_us = min(gap_us, 2_000_000)
                # 44100 Hz * 2 channels * 2 bytes/sample = 176400 bytes/sec = 0.1764 bytes/µs
                silence = bytes(int(fill_us * 44100 * 2 * 2 / 1_000_000))
                self._write_queue.put_nowait(silence)
            elif gap_us < -1_000:
                self.logger.debug("Discarding late audio chunk (%d µs behind)", -gap_us)
                return

        self._next_expected_timestamp_us = chunk.timestamp_us + chunk.duration_us
        self._write_queue.put_nowait(chunk.data)

    async def _cli_writer(self) -> None:
        """Write queued audio data to the CLI process stdin.

        Waits for the protocol to be ready before writing. Runs as a single
        task so writes are serialised and ordered. A None sentinel signals
        end-of-stream: write EOF to stdin and exit.
        """
        await self._protocol_ready.wait()
        while True:
            data = await self._write_queue.get()
            if not self._protocol:
                if data is None:
                    return
                continue
            if data is None:
                with suppress(Exception):
                    await self._protocol.write_audio_eof()
                return
            with suppress(Exception):
                await self._protocol.write_audio(data)

    async def _stop_streaming(self) -> None:
        """Stop streaming (internal, called with lock held)."""
        self._is_streaming = False
        self._next_expected_timestamp_us = None
        self._protocol_ready.clear()
        if self._protocol_start_task:
            self._protocol_start_task.cancel()
            with suppress(Exception):
                await self._protocol_start_task
            self._protocol_start_task = None
        if self._writer_task:
            self._writer_task.cancel()
            with suppress(Exception):
                await self._writer_task
            self._writer_task = None
        while not self._write_queue.empty():
            self._write_queue.get_nowait()
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
