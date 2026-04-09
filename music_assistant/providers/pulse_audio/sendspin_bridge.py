"""Sendspin Bridge for Local PulseAudio Out - streams audio to PA sinks."""
from __future__ import annotations

import asyncio
import json
import subprocess
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

from .constants import VOLUME_CONTROL_SOFTWARE
from .helpers import find_pactl, pactl_env
from .pa_simple import PASimpleStream
from .player import LocalPulseAudioPlayer, get_sink_uuid

if TYPE_CHECKING:
    from aiosendspin.server import ExternalStreamStartRequest, SendspinClient, SendspinServer
    from aiosendspin.server.roles import AudioChunk

    from music_assistant.providers.sendspin.provider import SendspinProvider

    from .provider import LocalPulseAudioProvider


class SendspinPulseAudioBridge:
    """Manages the Sendspin to PulseAudio sink bridge for a single sink."""

    def __init__(
        self,
        provider: LocalPulseAudioProvider,
        player: LocalPulseAudioPlayer,
        sink_info: dict[str, Any],
        sendspin_server: SendspinServer,
    ) -> None:
        self.provider = provider
        self.mass = provider.mass
        self.player = player
        self.sendspin_server = sendspin_server
        self.sink_name: str = sink_info["pa_sink_name"]
        self.display_name: str = sink_info["name"]
        self.sink_info = sink_info
        self.logger = provider.logger.getChild(f"bridge.{self.sink_name}")

        self._sendspin_client: SendspinClient | None = None
        self._bridge_client_id: str | None = None
        self._bridge_role: BridgePlayerRole | None = None
        self._is_streaming = False
        self._write_queue: asyncio.Queue[bytes | None] = asyncio.Queue()
        self._writer_task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()

    @property
    def is_registered(self) -> bool:
        """Return whether the bridge is registered with Sendspin."""
        return self._sendspin_client is not None

    async def start(self) -> None:
        """Register the PA sink as an external Sendspin client."""
        device_uuid = get_sink_uuid(self.sink_name)
        self._bridge_client_id = bridge_client_id_from_uuid(device_uuid)

        if sendspin_prov := self._get_sendspin_provider():
            sendspin_prov.register_bridge_identifiers(
                self._bridge_client_id,
                {IdentifierType.UUID: device_uuid},
            )

        hello = ClientHelloPayload(
            client_id=self._bridge_client_id,
            name=self.display_name,
            version=1,
            supported_roles=[BRIDGE_ROLE_ID, "player@v1"],
            device_info=SendspinDeviceInfo(
                product_name=self.display_name,
                manufacturer="PulseAudio",
            ),
            player_support=ClientHelloPlayerSupport(
                supported_formats=[
                    SupportedAudioFormat(
                        codec=AudioCodec.PCM,
                        channels=BRIDGE_CHANNELS,
                        sample_rate=96000,   # instead of BRIDGE_SAMPLE_RATE
                        bit_depth=24,        # instead of BRIDGE_BIT_DEPTH
                        #rate=BRIDGE_SAMPLE_RATE,
                        #bit_depth=BRIDGE_BIT_DEPTH
                    )
                ],
                buffer_capacity=1_000,
                supported_commands=[PlayerCommand.VOLUME, PlayerCommand.MUTE],
            ),
        )

        self.logger.debug(
            "Registering Sendspin bridge for sink %s (client_id=%s)",
            self.sink_name,
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
                initial_volume=25,
            )
            self._bridge_role.setup_audio_requirements()

        self.logger.info(
            "Sendspin bridge registered for sink %s (client_id=%s)",
            self.sink_name,
            self._bridge_client_id,
        )

    def _get_sendspin_provider(self) -> SendspinProvider | None:
        return cast("SendspinProvider | None", self.mass.get_provider("sendspin"))

    async def stop(self) -> None:
        """Stop and unregister the bridge."""
        async with self._lock:
            await self._stop_streaming()
            if self._sendspin_client and self._bridge_client_id:
                await self.sendspin_server.remove_client(self._bridge_client_id)
                self._sendspin_client = None
                self._bridge_role = None
        self.logger.debug("Sendspin bridge stopped for sink %s", self.sink_name)

    def _on_stream_start(self, request: ExternalStreamStartRequest) -> None:
        self.logger.debug(
            "Stream start request for sink %s (reason=%s)",
            self.sink_name,
            request.connection_reason,
        )
        self._is_streaming = True

    def _on_bridge_stream_start(self) -> None:
        """Start the audio writer task."""
        if self._writer_task is not None and not self._writer_task.done():
            self._writer_task.cancel()
        self._is_streaming = True
        while not self._write_queue.empty():
            self._write_queue.get_nowait()
        self._writer_task = self.mass.create_task(self._audio_writer())
        self.logger.info("Bridge writer started for sink %s", self.sink_name)

    def _on_bridge_stream_end(self) -> None:
        self._is_streaming = False
        self.mass.create_task(self._stop_streaming_locked())

    def _on_volume_change(self, volume: int) -> None:
        self.mass.create_task(self.player.volume_set(volume))

    def _on_mute_change(self, muted: bool) -> None:
        self.mass.create_task(self.player.volume_mute(muted))

    def _on_audio_chunk(self, chunk: AudioChunk) -> None:
        if not self._is_streaming:
            return
        self._write_queue.put_nowait(chunk.data)

    def _apply_software_volume(self, pcm_data: bytes) -> bytes:
        """Apply software volume scaling."""
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
        """Write queued audio to the PA sink via pa_simple."""
        loop = asyncio.get_running_loop()
        stream: PASimpleStream | None = None
        write_future: asyncio.Future | None = None
        try:
            stream = await loop.run_in_executor(
                None,
                lambda: PASimpleStream(
                    sink_name=self.sink_name,
                    app_name="music-assistant",
                    rate=BRIDGE_SAMPLE_RATE,
                    channels=BRIDGE_CHANNELS,
                ),
            )
            self.logger.debug("pa_simple stream opened for sink %s", self.sink_name)

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
            self.logger.error("pa_simple error for sink %s: %s", self.sink_name, err)
        finally:
            self._is_streaming = False
            if write_future is not None:
                with suppress(Exception):
                    await asyncio.shield(write_future)
            if stream is not None:
                with suppress(Exception):
                    await loop.run_in_executor(None, stream.close)
            if self._writer_task is asyncio.current_task():
                self._writer_task = None

    async def _stop_streaming_locked(self) -> None:
        async with self._lock:
            await self._stop_streaming()

    async def _stop_streaming(self) -> None:
        """Stop streaming (called with lock held)."""
        self._is_streaming = False
        if self._writer_task:
            self._writer_task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await self._writer_task
            self._writer_task = None
        while not self._write_queue.empty():
            self._write_queue.get_nowait()


class LocalPulseAudioBridgeManager:
    """Manages Sendspin bridges for all PulseAudio output sinks."""

    def __init__(self, provider: LocalPulseAudioProvider) -> None:
        self.provider = provider
        self.mass = provider.mass
        self.logger = provider.logger.getChild("bridge_manager")
        self._bridges: dict[str, SendspinPulseAudioBridge] = {}
        self._lock = asyncio.Lock()

    @property
    def sendspin_server(self) -> SendspinServer | None:
        if provider := cast("SendspinProvider | None", self.mass.get_provider("sendspin")):
            return provider.server_api
        return None

    async def discover_and_register(self) -> None:
        """Enumerate PA sinks and register players and Sendspin bridges."""
        sendspin_server = self.sendspin_server
        if not sendspin_server:
            self.logger.debug("Sendspin provider not available, skipping sink enumeration")
            return

        loop = asyncio.get_running_loop()
        try:
            sinks: list[dict[str, Any]] = await loop.run_in_executor(
                None, self._enumerate_pa_sinks
            )
        except Exception as err:
            self.logger.warning("Failed to enumerate PA sinks: %s", err, exc_info=True)
            return

        if not sinks:
            self.logger.info("No PulseAudio output sinks found")
            return

        self.logger.info("Found %d PulseAudio sink(s)", len(sinks))

        async with self._lock:
            for sink in sinks:
                pa_sink_name: str = sink["pa_sink_name"]
                display_name: str = sink["name"]
                device_uuid = get_sink_uuid(pa_sink_name)
                client_id = bridge_client_id_from_uuid(device_uuid)

                if client_id in self._bridges:
                    self.logger.debug("Bridge already exists for sink %s", pa_sink_name)
                    continue

                player = LocalPulseAudioPlayer(
                    self.provider,
                    player_id=device_uuid,
                    display_name=display_name,
                    pa_sink_name=pa_sink_name,
                )
                await self.mass.players.register_or_update(player)
                await player.apply_hardware_ceiling()

                bridge = SendspinPulseAudioBridge(
                    self.provider, player, sink, sendspin_server
                )
                try:
                    await bridge.start()
                except Exception:
                    self.logger.warning("Failed to start bridge for sink %s", pa_sink_name)
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
                    "Bridge created for sink %s (%s)", pa_sink_name, display_name
                )

    @staticmethod
    def _enumerate_pa_sinks() -> list[dict[str, Any]]:
        """Enumerate stereo-capable PulseAudio sinks via pactl."""
        sinks: list[dict[str, Any]] = []
        result = subprocess.run(
            [find_pactl(), "--format=json", "list", "sinks"],
            capture_output=True,
            text=True,
            timeout=5,
            env=pactl_env(),
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"pactl exited {result.returncode}: {result.stderr.strip()}"
            )
        for sink in json.loads(result.stdout):
            name: str = sink.get("name", "")
            desc: str = sink.get("description", name)
            spec_str: str = sink.get("sample_specification", "")
            # spec_str format: "s32le 2ch 96000Hz"
            try:
                parts = spec_str.split()
                fmt = parts[0]           # e.g. "s32le"
                channels = int(parts[1].replace("ch", ""))
                sample_rate = int(parts[2].replace("Hz", ""))
                # Extract bit depth from format string: s16le→16, s32le→32
                bit_depth = int("".join(filter(str.isdigit, fmt.split("le")[0].split("be")[0])))
            except (IndexError, ValueError):
                continue
            if channels < 2:
                continue
            sinks.append({
                "name": desc,
                "pa_sink_name": name,
                "max_output_channels": channels,
                "sample_rate": sample_rate,
                "bit_depth": bit_depth,
            })
        return sinks

    async def stop_all(self) -> None:
        """Stop all bridges."""
        async with self._lock:
            for bridge in list(self._bridges.values()):
                with suppress(Exception):
                    await bridge.stop()
            self._bridges.clear()
        self.logger.debug("All PulseAudio bridges stopped")
