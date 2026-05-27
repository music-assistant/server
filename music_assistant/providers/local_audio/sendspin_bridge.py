"""Sendspin Bridge for Local Audio Out - streams audio to local soundcards."""

from __future__ import annotations

import asyncio
import os
import sys
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

from .constants import (
    AUDIO_BACKEND_ALSA,
    AUDIO_BACKEND_AUTO,
    AUDIO_BACKEND_PULSEAUDIO,
    CONF_AUDIO_BACKEND,
    DEFAULT_BUFFER_FRAMES,
)
from .player import LocalAudioPlayer, get_device_uuid

if sys.platform == "linux":
    from .pa_simple import PASimpleStream, enumerate_pa_sinks

if TYPE_CHECKING:
    from aiosendspin.server import ExternalStreamStartRequest, SendspinClient, SendspinServer
    from aiosendspin.server.roles import AudioChunk

    from music_assistant.providers.sendspin.provider import SendspinProvider

    from .provider import LocalAudioProvider


def _detect_backend(configured: str) -> str:
    """Resolve the effective audio backend for Linux.

    Auto: use PulseAudio/PipeWire if PA socket is available, else ALSA.
    Explicit: use the configured backend directly.
    Non-Linux: always returns 'alsa' (sounddevice/PortAudio path).
    """
    if sys.platform != "linux":
        return AUDIO_BACKEND_ALSA
    if configured == AUDIO_BACKEND_PULSEAUDIO:
        return AUDIO_BACKEND_PULSEAUDIO
    if configured == AUDIO_BACKEND_ALSA:
        return AUDIO_BACKEND_ALSA
    # Auto: check for PA socket
    for path in (
        "/run/audio/pulse.sock",
        "/run/pulse/native",
        "/var/run/pulse/native",
        os.environ.get("PULSE_SERVER", "").removeprefix("unix:"),
    ):
        if path and os.path.exists(path):
            return AUDIO_BACKEND_PULSEAUDIO
    return AUDIO_BACKEND_ALSA


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
        self.device_name: str = device_info.get("description", device_info["name"])
        self.pa_sink_name: str | None = device_info.get("pa_sink_name")
        self.sample_rate: int = device_info.get("sample_rate", BRIDGE_SAMPLE_RATE)
        self.bit_depth: int = device_info.get("bit_depth", BRIDGE_BIT_DEPTH)
        self.device_info = device_info
        self.logger = provider.logger.getChild(f"bridge.{self.device_name}")

        self._sendspin_client: SendspinClient | None = None
        self._bridge_client_id: str | None = None
        self._bridge_role: BridgePlayerRole | None = None
        self._is_streaming = False
        self._write_queue: asyncio.Queue[bytes | None] = asyncio.Queue()
        self._writer_task: asyncio.Task[None] | None = None
        self._output_stream: sd.RawOutputStream | None = None
        self._pa_stream: PASimpleStream | None = None
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
                        sample_rate=self.sample_rate,  # sink native rate
                        bit_depth=self.bit_depth,      # sink native depth
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

        backend = _detect_backend(
            str(self.provider.config.get_value(CONF_AUDIO_BACKEND) or AUDIO_BACKEND_AUTO)
        )
        if backend == AUDIO_BACKEND_PULSEAUDIO and self.pa_sink_name:
            self._writer_task = self.mass.create_task(self._audio_writer_pa())
        else:
            self._writer_task = self.mass.create_task(self._audio_writer())
        self.logger.info("Bridge writer started for %s (backend=%s)", self.device_name, backend)

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
        """Apply software volume scaling to PCM data.

        Uses self.bit_depth (sink native depth, which MA is told to send)
        to select the correct numpy dtype for PCM scaling.
        """
        if self.player.volume_muted:
            return b"\x00" * len(pcm_data)
        volume = self.player.volume_level
        if volume is None or volume >= 100:
            return pcm_data
        if self.bit_depth == 32:
            dtype: np.dtype = np.dtype(np.int32)
            clip_min, clip_max = -(2**31), 2**31 - 1
        elif self.bit_depth == 24:
            # s24 arrives in 32-bit containers (s24-32le); treat as int32
            dtype = np.dtype(np.int32)
            clip_min, clip_max = -(2**23), 2**23 - 1
        else:
            dtype = np.dtype(np.int16)
            clip_min, clip_max = -32768, 32767
        scale = volume / 100.0
        samples = np.frombuffer(pcm_data, dtype=dtype).copy()
        samples = np.clip(samples * scale, clip_min, clip_max).astype(dtype)
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

    async def _audio_writer_pa(self) -> None:
        """Write queued audio data to a PulseAudio sink via PASimpleStream."""
        loop = asyncio.get_running_loop()
        write_future: asyncio.Future[None] | None = None
        stream: PASimpleStream | None = None
        try:
            pa_sink_name = self.pa_sink_name
            assert pa_sink_name is not None  # guarded by caller
            self.logger.debug(
                "Opening PA stream: sink=%s rate=%d channels=%d bit_depth=%d",
                pa_sink_name,
                self.sample_rate,
                BRIDGE_CHANNELS,
                self.bit_depth,
            )
            stream = await loop.run_in_executor(
                None,
                lambda: PASimpleStream(
                    sink_name=pa_sink_name,
                    app_name="music-assistant",
                    rate=self.sample_rate,    # matches what MA was told to send
                    channels=BRIDGE_CHANNELS,
                    bit_depth=self.bit_depth, # matches what MA was told to send
                ),
            )
            self.logger.debug("PA stream opened for %s", pa_sink_name)
            assert stream is not None  # assigned above; satisfies mypy

            while True:
                data = await self._write_queue.get()
                if data is None or not self._is_streaming:
                    break
                data = self._apply_software_volume(data)
                write_future = loop.run_in_executor(None, stream.write, data)
                await write_future
                write_future = None

        except asyncio.CancelledError:
            pass
        except OSError as err:
            self.logger.error("PA stream error for %s: %s", self.pa_sink_name, err)
        finally:
            self._is_streaming = False
            if write_future is not None:
                with suppress(Exception):
                    await asyncio.shield(write_future)
            if stream is not None:
                with suppress(Exception):
                    await asyncio.shield(loop.run_in_executor(None, stream.close))
            if self._writer_task is asyncio.current_task():
                self._writer_task = None

    async def _stop_streaming_locked(self) -> None:
        """Serialize streaming teardown."""
        async with self._lock:
            await self._stop_streaming()

    async def _stop_streaming(self) -> None:
        """Stop streaming (internal, called with lock held)."""
        self._is_streaming = False
        if self._writer_task and not self._writer_task.done():
            # Signal the writer to stop gracefully by sending None.
            # Don't cancel the task directly as it may be blocked in
            # run_in_executor writing to the audio device - cancelling
            # while a write is in progress can cause a segfault.
            while not self._write_queue.empty():
                self._write_queue.get_nowait()
            self._write_queue.put_nowait(None)
            try:
                await asyncio.wait_for(self._writer_task, timeout=2.0)
            except TimeoutError:
                # Writer didn't stop in time - it may be stuck in a blocking write.
                # Cancel it as a last resort but don't close the stream until it's done.
                self._writer_task.cancel()
                with suppress(asyncio.CancelledError):
                    await self._writer_task
            except asyncio.CancelledError:
                pass
            self._writer_task = None
        # Drain any remaining items
        while not self._write_queue.empty():
            self._write_queue.get_nowait()
        # Stream cleanup is handled in _audio_writer's finally block


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
                None,
                self._enumerate_output_devices,
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
                device_index: int = device.get("index", 0)
                # Use stable PA sink name (or sounddevice name) for UUID generation
                # Use human-readable description for MA player display name
                device_name: str = device["name"]
                display_name: str = device.get("description", device_name)
                pa_sink_name: str | None = device.get("pa_sink_name")
                is_remap: bool = device.get("is_remap", False)
                hostapi_index: int = device.get("hostapi", 0)
                device_uuid = get_device_uuid(device_name, hostapi_index)
                client_id = bridge_client_id_from_uuid(device_uuid)

                if client_id in self._bridges:
                    self.logger.debug("Bridge already exists for %s", display_name)
                    continue

                # Register (or update) our player with MA
                player = LocalAudioPlayer(
                    self.provider,
                    player_id=device_uuid,
                    device_name=display_name,
                    hostapi_index=hostapi_index,
                    device_index=device_index,
                    pa_sink_name=pa_sink_name,
                    is_remap=is_remap,
                )
                # Restore cached volume/mute state before registering so the
                # correct values are included in the initial PLAYER_ADDED event
                await player.restore_state()
                await self.mass.players.register_or_update(player)
                # Set PA sink hardware volume to 100% on init
                await player.apply_hardware_ceiling()

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

    def _enumerate_output_devices(self) -> list[dict[str, Any]]:
        """Enumerate available audio output devices based on configured backend."""
        configured = str(self.provider.config.get_value(CONF_AUDIO_BACKEND) or AUDIO_BACKEND_AUTO)
        backend = _detect_backend(configured)
        self.logger.debug("Audio backend: %s (configured=%s)", backend, configured)

        if backend == AUDIO_BACKEND_PULSEAUDIO:
            return enumerate_pa_sinks()

        # ALSA / macOS / fallback: use sounddevice
        devices: list[dict[str, Any]] = []
        all_devices = sd.query_devices()
        for idx, dev in enumerate(all_devices):
            max_output_channels: int = dev.get("max_output_channels", 0)
            if max_output_channels < 2:
                continue
            # Test if device can actually be opened — filters ALSA virtual devices
            # that may be unavailable when PipeWire controls the hardware
            try:
                test_stream = sd.RawOutputStream(
                    device=idx,
                    samplerate=BRIDGE_SAMPLE_RATE,
                    channels=BRIDGE_CHANNELS,
                    dtype="int16",
                )
                test_stream.close()
            except sd.PortAudioError:
                continue
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
