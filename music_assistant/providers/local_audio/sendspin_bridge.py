"""Sendspin Bridge for Local Audio Out - streams audio to local soundcards."""

from __future__ import annotations

import asyncio
import sys
from contextlib import suppress
from typing import TYPE_CHECKING, Any, cast

import numpy as np
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

from .constants import DEFAULT_BUFFER_FRAMES
from .player import LocalAudioPlayer, get_device_uuid

if sys.platform == "linux":
    from .pa_simple import PASimpleStream, enumerate_pa_sinks

if TYPE_CHECKING:
    import sounddevice as sd  # noqa: F401

    from aiosendspin.server import (
        ExternalStreamStartRequest,
        SendspinClient,
        SendspinServer,
    )
    from aiosendspin.server.roles import AudioChunk

    from music_assistant.providers.sendspin.provider import SendspinProvider

    from .provider import LocalAudioProvider


class SendspinLocalAudioBridge:
    """Manages the Sendspin to local soundcard bridge for a single device."""

    def __init__(
        self,
        provider: LocalAudioProvider,
        player: LocalAudioPlayer,
        device_info: dict[str, Any],
        sendspin_server: SendspinServer,
    ) -> None:
        """
        Initialize the bridge.

        :param provider: The Local Audio provider instance.
        :param player: The LocalAudioPlayer that owns this bridge.
        :param device_info: Device info dict — on Linux: from enumerate_pa_sinks();
            on Darwin: from sounddevice.query_devices() with 'index' added.
        :param sendspin_server: The Sendspin server to register with.
        """
        self.provider = provider
        self.mass = provider.mass
        self.player = player
        self.sendspin_server = sendspin_server
        self.device_info = device_info
        self.device_name: str = device_info["name"]
        # Linux: PA sink name; Darwin: not used for streaming
        self.pa_sink_name: str | None = device_info.get("pa_sink_name")
        # Linux: from PA sample_spec; Darwin: fixed bridge defaults
        self.sample_rate: int = device_info.get("sample_rate", BRIDGE_SAMPLE_RATE)
        self.bit_depth: int = device_info.get("bit_depth", BRIDGE_BIT_DEPTH)
        # Darwin only
        self.device_index: int | None = device_info.get("index")
        self.logger = provider.logger.getChild(f"bridge.{self.device_name}")

        self._sendspin_client: SendspinClient | None = None
        self._bridge_client_id: str | None = None
        self._bridge_role: BridgePlayerRole | None = None
        self._is_streaming = False
        self._logged_chunk_fmt: bool = False
        self._write_queue: asyncio.Queue[bytes | None] = asyncio.Queue()
        self._writer_task: asyncio.Task[None] | None = None
        self._output_stream: Any | None = None  # sd.RawOutputStream on Darwin
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

        if sendspin_prov := self._get_sendspin_provider():
            sendspin_prov.register_bridge_identifiers(
                self._bridge_client_id,
                {IdentifierType.UUID: device_uuid},
            )

        # On Linux advertise the sink's native format so MA transcodes correctly.
        # On Darwin use fixed bridge defaults.
        _depths = sorted({self.bit_depth, BRIDGE_BIT_DEPTH}, reverse=True)
        supported_formats = [
            SupportedAudioFormat(
                codec=AudioCodec.PCM,
                channels=BRIDGE_CHANNELS,
                sample_rate=self.sample_rate,
                bit_depth=d,
            )
            for d in _depths
        ]

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
                supported_formats=supported_formats,
                buffer_capacity=1_000,
                supported_commands=[PlayerCommand.VOLUME, PlayerCommand.MUTE],
            ),
        )

        self.logger.debug(
            "Registering Sendspin bridge for %s (client_id=%s)",
            self.device_name,
            self._bridge_client_id,
        )

        self._sendspin_client = self.sendspin_server.register_external_player(
            hello, on_stream_start=self._on_stream_start
        )

        for role in self._sendspin_client.roles_by_family("player"):
            self.logger.debug(
                "Found player role: %s type=%s", role.role_id, type(role).__name__
            )
            if isinstance(role, BridgePlayerRole):
                self._bridge_role = role
                break

        if self._bridge_role is None:
            self.logger.error("No BridgePlayerRole found for %s", self.device_name)
            return

        # Restore last volume from cache, fall back to default
        init_volume = self.player._attr_volume_level

        self._bridge_role.set_callbacks(
            on_audio_chunk=self._on_audio_chunk,
            on_volume_change=self._on_volume_change,
            on_mute_change=self._on_mute_change,
            on_stream_start=self._on_bridge_stream_start,
            on_stream_end=self._on_bridge_stream_end,
            initial_volume=int(init_volume) if init_volume is not None else 25,
        )
        self._bridge_role.setup_audio_requirements(
            sample_rate=self.sample_rate,
            bit_depth=self.bit_depth,
            channels=BRIDGE_CHANNELS,
        )

        self.logger.info(
            "Sendspin bridge registered for %s (client_id=%s)",
            self.device_name,
            self._bridge_client_id,
        )

    def _get_sendspin_provider(self) -> SendspinProvider | None:
        """Get the Sendspin provider if available."""
        return cast("SendspinProvider | None", self.mass.get_provider("sendspin"))

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
        if not self._logged_chunk_fmt:
            self.logger.debug(
                "First chunk: len=%d  sample_rate=%d bit_depth=%d",
                len(chunk.data),
                self.sample_rate,
                self.bit_depth,
            )
            self._logged_chunk_fmt = True
        self._write_queue.put_nowait(chunk.data)

    def _apply_software_volume(self, pcm_data: bytes) -> bytes:
        """Apply software volume scaling and format conversion."""
        if self.player.volume_muted:
            if self.bit_depth == 24:
                # PA expects packed s24le: 3 bytes/sample, not 4
                return b"\x00" * (len(pcm_data) * 3 // 4)
            return b"\x00" * len(pcm_data)
        volume = self.player.volume_level
        scale = volume / 100.0 if (volume is not None and volume < 100) else None

        if self.bit_depth == 32:
            if scale is None:
                return pcm_data
            samples = np.frombuffer(pcm_data, dtype=np.int32).copy()
            scaled = np.clip(
                samples.astype(np.float64) * scale, -2147483648, 2147483647
            )
            return scaled.astype(np.int32).tobytes()

        if self.bit_depth == 24:
            # MA delivers 24-bit audio left-justified in 32-bit containers.
            # Always repack to packed s24le (bytes 1-3 of each int32).
            samples = np.frombuffer(pcm_data, dtype=np.int32).copy()
            if scale is not None:
                samples = np.clip(
                    samples.astype(np.float64) * scale, -2147483648, 2147483647
                ).astype(np.int32)
            return samples.view(np.uint8).reshape(-1, 4)[:, 1:].tobytes()

        # 16-bit
        if scale is None:
            return pcm_data
        samples_16 = np.frombuffer(pcm_data, dtype=np.int16).copy()
        scaled = np.clip(samples_16.astype(np.float64) * scale, -32768, 32767)
        return scaled.astype(np.int16).tobytes()

    async def _audio_writer(self) -> None:
        """Write queued audio to the output device."""
        if sys.platform == "linux":
            await self._audio_writer_pulse()
        else:
            await self._audio_writer_sounddevice()

    async def _audio_writer_pulse(self) -> None:
        """Write queued audio to a PA sink via PASimpleStream (Linux)."""
        stream: PASimpleStream | None = None
        write_future: asyncio.Future[None] | None = None
        try:
            self.logger.debug(
                "Opening PA stream: sink=%s rate=%d channels=%d bit_depth=%d",
                self.pa_sink_name,
                self.sample_rate,
                BRIDGE_CHANNELS,
                self.bit_depth,
            )
            assert self.pa_sink_name is not None  # guarded by Linux-only call path
            pa_sink_name = (
                self.pa_sink_name
            )  # capture for lambda — assert doesn't narrow closures
            stream = await self.mass.loop.run_in_executor(
                None,
                lambda: PASimpleStream(
                    sink_name=pa_sink_name,
                    app_name="music-assistant",
                    rate=self.sample_rate,
                    channels=BRIDGE_CHANNELS,
                    bit_depth=self.bit_depth,
                ),
            )
            self.logger.debug("PA stream opened for %s", self.pa_sink_name)
            assert stream is not None  # assigned above; satisfies mypy

            while True:
                data = await self._write_queue.get()
                if data is None or not self._is_streaming:
                    break
                data = self._apply_software_volume(data)
                write_future = self.mass.loop.run_in_executor(None, stream.write, data)
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
                    await self.mass.loop.run_in_executor(None, stream.close)
            if self._writer_task is asyncio.current_task():
                self._writer_task = None

    async def _audio_writer_sounddevice(self) -> None:
        """Write queued audio to a sounddevice output stream (Darwin)."""
        import sounddevice as _sd  # noqa: PLC0415

        try:
            self._output_stream = _sd.RawOutputStream(
                device=self.device_index,
                samplerate=self.sample_rate,
                channels=BRIDGE_CHANNELS,
                dtype="int16",
                blocksize=DEFAULT_BUFFER_FRAMES,
            )
            self._output_stream.start()
            self.logger.debug("sounddevice stream opened for %s", self.device_name)

            while True:
                data = await self._write_queue.get()
                if data is None or not self._is_streaming:
                    break
                data = self._apply_software_volume(data)
                try:
                    await self.mass.loop.run_in_executor(
                        None, self._output_stream.write, data
                    )
                except _sd.PortAudioError as err:
                    self.logger.error(
                        "PortAudio error for %s: %s", self.device_name, err
                    )
                    break
        except _sd.PortAudioError as err:
            self.logger.error(
                "Failed to open sounddevice stream for %s: %s", self.device_name, err
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
        if self._writer_task and not self._writer_task.done():
            if sys.platform == "linux":
                # PA writer handles CancelledError cleanly
                self._writer_task.cancel()
                with suppress(asyncio.CancelledError, Exception):
                    await self._writer_task
            else:
                # sounddevice: signal gracefully via None to avoid segfault
                # on a cancelled blocking write
                while not self._write_queue.empty():
                    self._write_queue.get_nowait()
                self._write_queue.put_nowait(None)
                try:
                    await asyncio.wait_for(self._writer_task, timeout=2.0)
                except TimeoutError:
                    self._writer_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await self._writer_task
                except asyncio.CancelledError:
                    pass
            self._writer_task = None
        while not self._write_queue.empty():
            self._write_queue.get_nowait()


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
        if provider := cast(
            "SendspinProvider | None", self.mass.get_provider("sendspin")
        ):
            return provider.server_api
        return None

    async def discover_and_register(self) -> None:
        """Enumerate output devices, register players and Sendspin bridges."""
        sendspin_server = self.sendspin_server
        if not sendspin_server:
            self.logger.debug(
                "Sendspin provider not available, skipping device enumeration"
            )
            return

        try:
            devices: list[dict[str, Any]] = await self.mass.loop.run_in_executor(
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
                device_name: str = device["name"]
                hostapi_index: int = device.get("hostapi", 0)
                pa_sink_name: str | None = device.get("pa_sink_name")
                device_uuid = get_device_uuid(device_name, hostapi_index)
                client_id = bridge_client_id_from_uuid(device_uuid)

                if client_id in self._bridges:
                    self.logger.debug("Bridge already exists for %s", device_name)
                    continue

                player = LocalAudioPlayer(
                    self.provider,
                    player_id=device_uuid,
                    device_name=device_name,
                    hostapi_index=hostapi_index,
                    device_index=device.get("index", 0),
                    pa_sink_name=pa_sink_name,
                )
                await self.mass.players.register_or_update(player)
                # Restore cached volume/mute state from previous session
                await player.restore_state()
                # Set PA sink hardware volume to 100% on init
                await player.apply_hardware_ceiling()

                bridge = SendspinLocalAudioBridge(
                    self.provider, player, device, sendspin_server
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
                self.logger.info(
                    "Bridge created for %s (pa_sink=%s)",
                    device_name,
                    pa_sink_name or "n/a",
                )

    @staticmethod
    def _enumerate_output_devices() -> list[dict[str, Any]]:
        """Enumerate available audio output devices.

        On Linux: uses enumerate_pa_sinks() from pa_simple — returns PA sinks
            directly with native sample_rate and bit_depth populated.
        On Darwin: uses sounddevice, testing each device can be opened, with
            fixed bridge sample rate/bit depth defaults.
        """
        if sys.platform != "linux":
            # Darwin / other: sounddevice path
            import sounddevice as _sd  # noqa: PLC0415

            devices: list[dict[str, Any]] = []
            for idx, dev in enumerate(_sd.query_devices()):
                if dev.get("max_output_channels", 0) < 2:
                    continue
                try:
                    test_stream = _sd.RawOutputStream(
                        device=idx,
                        samplerate=BRIDGE_SAMPLE_RATE,
                        channels=BRIDGE_CHANNELS,
                        dtype="int16",
                    )
                    test_stream.close()
                except _sd.PortAudioError:
                    continue
                dev_with_index = dict(dev)
                dev_with_index["index"] = idx
                devices.append(dev_with_index)
            return devices
        return enumerate_pa_sinks()

    async def stop_all(self) -> None:
        """Stop all Sendspin bridges."""
        async with self._lock:
            for bridge in list(self._bridges.values()):
                with suppress(Exception):
                    await bridge.stop()
            self._bridges.clear()
        self.logger.debug("All local audio bridges stopped")
