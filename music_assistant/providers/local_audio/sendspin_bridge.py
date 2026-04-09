"""Sendspin Bridge for Local Audio Out - streams audio to local soundcards."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from typing import TYPE_CHECKING, Any, cast

import numpy as np
import sounddevice as sd
from aiosendspin.models.core import ClientHelloPayload
from aiosendspin.models.core import DeviceInfo as SendspinDeviceInfo
from aiosendspin.models.player import ClientHelloPlayerSupport, SupportedAudioFormat
from aiosendspin.models.types import AudioCodec, PlayerCommand
from music_assistant_models.enums import IdentifierType

from music_assistant.providers.sendspin.bridge_role import (
    BRIDGE_BIT_DEPTH,
    BRIDGE_CHANNELS,
    BRIDGE_ROLE_ID,
    BRIDGE_SAMPLE_RATE,
    BridgePlayerRole,
)
from music_assistant.providers.sendspin.helpers import bridge_client_id_from_uuid

from .constants import DEFAULT_BUFFER_FRAMES, VOLUME_CONTROL_SOFTWARE
from .player import LocalAudioPlayer, get_device_uuid

if TYPE_CHECKING:
    from aiosendspin.server import ExternalStreamStartRequest, SendspinClient, SendspinServer
    from aiosendspin.server.roles import AudioChunk

    from music_assistant.providers.sendspin.provider import SendspinProvider

    from .provider import LocalAudioProvider


class SendspinLocalAudioBridge:
    """Manages the Sendspin to local soundcard bridge for a single device."""

    def __init__(
        self,
        provider: LocalAudioProvider,
        player: LocalAudioPlayer,
        device_index: int,
        device_info: dict[str, Any],
        sendspin_server: SendspinServer,
    ) -> None:
        """
        Initialize the bridge.

        :param provider: The Local Audio provider instance.
        :param player: The LocalAudioPlayer that owns this bridge.
        :param device_index: The PortAudio device index.
        :param device_info: The device info dict from sounddevice.query_devices().
        :param sendspin_server: The Sendspin server to register with.
        """
        self.provider = provider
        self.mass = provider.mass
        self.player = player
        self.sendspin_server = sendspin_server
        self.device_index = device_index
        self.device_name: str = device_info["name"]
        self.device_info = device_info
        self.logger = provider.logger.getChild(f"bridge.{self.device_name}")

        self._sendspin_client: SendspinClient | None = None
        self._bridge_client_id: str | None = None
        self._bridge_role: BridgePlayerRole | None = None
        self._is_streaming = False
        self._write_queue: asyncio.Queue[bytes | None] = asyncio.Queue()
        self._writer_task: asyncio.Task[None] | None = None
        self._output_stream: sd.RawOutputStream | None = None
        self._lock = asyncio.Lock()

    @property
    def is_registered(self) -> bool:
        """Return whether the bridge is registered with Sendspin."""
        return self._sendspin_client is not None

    async def start(self) -> None:
        """Register the local audio device as an external Sendspin client."""
        hostapi_index: int = self.device_info.get("hostapi", 0)
        device_uuid = get_device_uuid(self.device_name, hostapi_index)
        self._bridge_client_id = bridge_client_id_from_uuid(device_uuid)

        # Pre-register identifier so protocol linking matches our LocalAudioPlayer
        if sendspin_prov := self._get_sendspin_provider():
            sendspin_prov.register_bridge_identifiers(
                self._bridge_client_id,
                {IdentifierType.UUID: device_uuid},
            )

        hello = ClientHelloPayload(
            client_id=self._bridge_client_id,
            name=self.device_name,
            version=1,
            supported_roles=[BRIDGE_ROLE_ID, "player@v1"],
            device_info=SendspinDeviceInfo(
                product_name=self.device_name,
                manufacturer="Local Audio",
            ),
            player_support=ClientHelloPlayerSupport(
                supported_formats=[
                    SupportedAudioFormat(
                        codec=AudioCodec.PCM,
                        channels=BRIDGE_CHANNELS,
                        sample_rate=BRIDGE_SAMPLE_RATE,
                        bit_depth=BRIDGE_BIT_DEPTH,
                    )
                ],
                buffer_capacity=1_000,
                supported_commands=[PlayerCommand.VOLUME, PlayerCommand.MUTE],
            ),
        )

        self.logger.debug(
            "Registering Sendspin bridge for %s with client_id=%s",
            self.device_name,
            self._bridge_client_id,
        )

        self._sendspin_client = self.sendspin_server.register_external_player(
            hello, on_stream_start=self._on_stream_start
        )

        roles = self._sendspin_client.roles_by_family("player")
        if roles:
            self._bridge_role = cast("BridgePlayerRole", roles[0])
            self._bridge_role.set_callbacks(
                on_audio_chunk=self._on_audio_chunk,
                on_volume_change=self._on_volume_change,
                on_mute_change=self._on_mute_change,
                on_stream_start=self._on_bridge_stream_start,
                on_stream_end=self._on_bridge_stream_end,
            )
            self._bridge_role.setup_audio_requirements()

        self.logger.info(
            "Sendspin bridge registered for %s (client_id=%s)",
            self.device_name,
            self._bridge_client_id,
        )

    def _get_sendspin_provider(self) -> SendspinProvider | None:
        """Get the Sendspin provider if available."""
        return cast(
            "SendspinProvider | None",
            self.mass.get_provider("sendspin"),
        )

    async def stop(self) -> None:
        """Stop and unregister the Sendspin bridge."""
        async with self._lock:
            await self._stop_streaming()
            if self._sendspin_client and self._bridge_client_id:
                await self.sendspin_server.remove_client(self._bridge_client_id)
                self._sendspin_client = None
                self._bridge_role = None

        self.logger.debug("Sendspin bridge stopped for %s", self.device_name)

    def _on_stream_start(self, request: ExternalStreamStartRequest) -> None:
        """Handle stream start request from Sendspin server."""
        self.logger.debug(
            "Sendspin stream start request for %s (reason=%s)",
            self.device_name,
            request.connection_reason,
        )
        self._is_streaming = True

    def _on_bridge_stream_start(self) -> None:
        """Start the audio writer task for a new stream."""
        if self._writer_task is not None and not self._writer_task.done():
            self._writer_task.cancel()

        self._is_streaming = True

        # Drain stale audio data from the previous stream
        while not self._write_queue.empty():
            self._write_queue.get_nowait()

        self._writer_task = self.mass.create_task(self._audio_writer())
        self.logger.info("Bridge writer started for %s", self.device_name)

    def _on_bridge_stream_end(self) -> None:
        """Stop streaming when the stream ends."""
        self._is_streaming = False
        self.mass.create_task(self._stop_streaming_locked())

    def _on_volume_change(self, volume: int) -> None:
        """Sync volume from Sendspin side back to our player."""
        self.mass.create_task(self.player.volume_set(volume))

    def _on_mute_change(self, muted: bool) -> None:
        """Sync mute from Sendspin side back to our player."""
        self.mass.create_task(self.player.volume_mute(muted))

    def _on_audio_chunk(self, chunk: AudioChunk) -> None:
        """Handle an incoming audio chunk."""
        if not self._is_streaming:
            return
        self._write_queue.put_nowait(chunk.data)

    def _apply_software_volume(self, pcm_data: bytes) -> bytes:
        """Apply software volume scaling if configured, reading current player state."""
        if self.player.volume_control_mode != VOLUME_CONTROL_SOFTWARE:
            return pcm_data
        if self.player.volume_muted:
            return b"\x00" * len(pcm_data)
        volume = self.player.volume_level
        if volume is None or volume >= 100:
            return pcm_data
        samples = np.frombuffer(pcm_data, dtype=np.int16).copy()
        scale = volume / 100.0
        samples = np.clip(samples * scale, -32768, 32767).astype(np.int16)
        return samples.tobytes()

    async def _audio_writer(self) -> None:
        """Write queued audio data to the soundcard."""
        loop = asyncio.get_running_loop()
        try:
            self._output_stream = sd.RawOutputStream(
                device=self.device_index,
                samplerate=BRIDGE_SAMPLE_RATE,
                channels=BRIDGE_CHANNELS,
                dtype="int16",
                blocksize=DEFAULT_BUFFER_FRAMES,
            )
            self._output_stream.start()
            self.logger.debug("Audio output stream opened for %s", self.device_name)

            while True:
                data = await self._write_queue.get()
                if data is None:
                    break
                if not self._is_streaming:
                    break
                # Apply software volume right before writing to minimize latency
                data = self._apply_software_volume(data)
                try:
                    await loop.run_in_executor(None, self._output_stream.write, data)
                except sd.PortAudioError as err:
                    self.logger.error("PortAudio error writing to %s: %s", self.device_name, err)
                    break
        except sd.PortAudioError as err:
            self.logger.error(
                "Failed to open audio output stream for %s: %s", self.device_name, err
            )
        finally:
            self._is_streaming = False
            if self._output_stream is not None:
                with suppress(Exception):
                    self._output_stream.stop()
                with suppress(Exception):
                    self._output_stream.close()
                self._output_stream = None
            if self._writer_task is asyncio.current_task():
                self._writer_task = None

    async def _stop_streaming_locked(self) -> None:
        """Serialize streaming teardown."""
        async with self._lock:
            await self._stop_streaming()

    async def _stop_streaming(self) -> None:
        """Stop streaming (internal, called with lock held)."""
        self._is_streaming = False
        if self._writer_task:
            self._writer_task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await self._writer_task
            self._writer_task = None
        while not self._write_queue.empty():
            self._write_queue.get_nowait()
        if self._output_stream is not None:
            with suppress(Exception):
                self._output_stream.stop()
            with suppress(Exception):
                self._output_stream.close()
            self._output_stream = None


class LocalAudioBridgeManager:
    """Manages Sendspin bridges for all local audio output devices."""

    def __init__(self, provider: LocalAudioProvider) -> None:
        """
        Initialize the bridge manager.

        :param provider: The Local Audio provider instance.
        """
        self.provider = provider
        self.mass = provider.mass
        self.logger = provider.logger.getChild("bridge_manager")
        self._bridges: dict[str, SendspinLocalAudioBridge] = {}
        self._lock = asyncio.Lock()

    @property
    def sendspin_server(self) -> SendspinServer | None:
        """Get the Sendspin server if available."""
        if provider := cast("SendspinProvider | None", self.mass.get_provider("sendspin")):
            return provider.server_api
        return None

    async def discover_and_register(self) -> None:
        """Enumerate local audio devices, register players and Sendspin bridges."""
        sendspin_server = self.sendspin_server
        if not sendspin_server:
            self.logger.debug("Sendspin provider not available, skipping device enumeration")
            return

        loop = asyncio.get_running_loop()
        try:
            devices: list[dict[str, Any]] = await loop.run_in_executor(
                None, self._enumerate_output_devices
            )
        except Exception as err:
            self.logger.warning("Failed to enumerate audio devices: %s", err)
            return

        if not devices:
            self.logger.info("No local audio output devices found")
            return

        self.logger.info("Found %d local audio output device(s)", len(devices))

        async with self._lock:
            for device in devices:
                device_index: int = device["index"]
                device_name: str = device["name"]
                hostapi_index: int = device.get("hostapi", 0)
                device_uuid = get_device_uuid(device_name, hostapi_index)
                client_id = bridge_client_id_from_uuid(device_uuid)

                if client_id in self._bridges:
                    self.logger.debug("Bridge already exists for %s", device_name)
                    continue

                # Register (or update) our player with MA
                player = LocalAudioPlayer(
                    self.provider, device_uuid, device_name, hostapi_index, device_index
                )
                await self.mass.players.register_or_update(player)

                # Then set up the Sendspin bridge with identifier for protocol linking
                bridge = SendspinLocalAudioBridge(
                    self.provider, player, device_index, device, sendspin_server
                )
                try:
                    await bridge.start()
                except Exception:
                    self.logger.warning("Failed to start bridge for %s", device_name)
                    with suppress(Exception):
                        await bridge.stop()
                    player._attr_available = False
                    player.update_state()
                    continue

                if not bridge.is_registered:
                    player._attr_available = False
                    player.update_state()
                    continue

                self._bridges[client_id] = bridge
                self.logger.info("Bridge created for %s", device_name)

    @staticmethod
    def _enumerate_output_devices() -> list[dict[str, Any]]:
        """Enumerate available audio output devices via sounddevice."""
        devices: list[dict[str, Any]] = []
        # sd.query_devices() returns a DeviceList (not a plain list),
        # where each element is a dict with device properties.
        all_devices = sd.query_devices()
        for idx, dev in enumerate(all_devices):
            max_output_channels: int = dev.get("max_output_channels", 0)
            if max_output_channels >= 2:
                dev_with_index = dict(dev)
                dev_with_index["index"] = idx
                devices.append(dev_with_index)
        return devices

    async def stop_all(self) -> None:
        """Stop all Sendspin bridges."""
        async with self._lock:
            for bridge in list(self._bridges.values()):
                with suppress(Exception):
                    await bridge.stop()
            self._bridges.clear()

        self.logger.debug("All local audio bridges stopped")
