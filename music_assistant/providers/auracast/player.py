"""Music Assistant player backed by a Bluetooth LE Audio broadcast."""

from __future__ import annotations

import asyncio
import os
import secrets
import time
from contextlib import aclosing, suppress
from typing import TYPE_CHECKING, cast

import numpy as np
from music_assistant_models.enums import ContentType, PlaybackState, PlayerFeature, PlayerType
from music_assistant_models.errors import MusicAssistantError, PlayerUnavailableError
from music_assistant_models.media_items.audio_format import AudioFormat

from music_assistant.controllers.streams.audio_processing import get_media_session_id
from music_assistant.helpers.audio import iter_pcm_slices
from music_assistant.helpers.ffmpeg import get_ffmpeg_stream
from music_assistant.models.player import Player, PlayerMedia

from .broadcaster import (
    BROADCAST_ERRORS,
    CONTROLLER_STACK_SETUP_KEY,
    AuracastBroadcaster,
    BroadcastConfig,
    parse_manufacturer_data,
)
from .constants import (
    CONF_BAUDRATE,
    CONF_BROADCAST_NAME,
    CONF_MANUFACTURER_DATA,
    CONF_SERIAL_PORT,
    DEFAULT_BAUDRATE,
    DEFAULT_BROADCAST_NAME,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from . import AuracastProvider

PCM_FORMAT = AudioFormat(
    content_type=ContentType.PCM_S16LE,
    codec_type=ContentType.PCM_S16LE,
    sample_rate=48_000,
    bit_depth=16,
    channels=2,
)
FRAME_DURATION_MS = 10
PCM_FRAME_BYTES = PCM_FORMAT.channels * PCM_FORMAT.bit_depth // 8
PCM_BYTES_PER_SECOND = PCM_FORMAT.sample_rate * PCM_FRAME_BYTES
SILENCE = bytes(PCM_FORMAT.sample_rate * FRAME_DURATION_MS // 1000 * PCM_FRAME_BYTES)
CONF_STORED_VOLUME = "auracast_stored_volume"
CONF_STORED_BROADCAST_ID = "auracast_broadcast_id"
CONTROLLER_POLL_INTERVAL = 5


class AuracastPlayer(Player):
    """Expose the configured Auracast broadcast as an MA player."""

    def __init__(self, provider: AuracastProvider, player_id: str) -> None:
        """Initialize the player and its PCM stream state."""
        super().__init__(provider, player_id)
        self._attr_name = str(
            provider.config.get_value(CONF_BROADCAST_NAME, DEFAULT_BROADCAST_NAME)
        )
        self._serial_port = str(provider.config.get_value(CONF_SERIAL_PORT))
        controller_stack = provider.get_setup_value(CONTROLLER_STACK_SETUP_KEY)
        if not isinstance(controller_stack, str) or not controller_stack:
            controller_stack = f"UART HCI ({self._serial_port})"
        self._attr_device_info.manufacturer = "Bluetooth SIG"
        self._attr_device_info.model = f"Host stack: Bumble; Controller stack: {controller_stack}"
        self._attr_type = PlayerType.PLAYER
        self._attr_supported_features = {
            PlayerFeature.PLAY_MEDIA,
            PlayerFeature.PAUSE,
            PlayerFeature.POWER,
            PlayerFeature.VOLUME_SET,
        }
        self._attr_powered = True
        self._attr_available = True
        self._attr_poll_interval = CONTROLLER_POLL_INTERVAL
        self._attr_volume_level = max(
            0,
            min(
                100,
                int(
                    provider.mass.config.get_raw_player_config_value(
                        player_id, CONF_STORED_VOLUME, 100
                    )
                ),
            ),
        )
        stored_broadcast_id = provider.mass.config.get_raw_player_config_value(
            player_id, CONF_STORED_BROADCAST_ID
        )
        if isinstance(stored_broadcast_id, int) and not isinstance(stored_broadcast_id, bool):
            broadcast_id = stored_broadcast_id
        elif isinstance(stored_broadcast_id, str):
            try:
                broadcast_id = int(stored_broadcast_id)
            except ValueError:
                broadcast_id = 0
        else:
            broadcast_id = 0
        if not 0 < broadcast_id < 0x1000000:
            broadcast_id = secrets.randbelow(0xFFFFFF) + 1
            provider.mass.config.set_raw_player_config_value(
                player_id, CONF_STORED_BROADCAST_ID, broadcast_id
            )
        self._broadcaster = AuracastBroadcaster(
            BroadcastConfig(
                name=self._attr_name,
                transport_spec=(
                    f"serial:{self._serial_port},"
                    f"{provider.config.get_value(CONF_BAUDRATE, DEFAULT_BAUDRATE)}"
                ),
                manufacturer_data=parse_manufacturer_data(
                    cast(
                        "str | None",
                        provider.config.get_value(CONF_MANUFACTURER_DATA, ""),
                    )
                ),
                broadcast_id=broadcast_id,
            )
        )
        self._stream_task: asyncio.Task[None] | None = None
        self._silence_task: asyncio.Task[None] | None = None
        self._broadcast_faulted = False
        self._lifecycle_lock = asyncio.Lock()
        self._resume_event = asyncio.Event()
        self._resume_event.set()
        self._streamed_seconds = 0.0

    @property
    def needs_poll(self) -> bool:
        """Return whether the player polls to monitor the controller connection."""
        return bool(self._attr_powered)

    async def start_broadcast(self) -> None:
        """Start an empty broadcast and keep it active while the player is idle."""
        async with self._lifecycle_lock:
            try:
                await self._start_broadcast()
            except BROADCAST_ERRORS as err:
                self._attr_available = False
                self.logger.error("Unable to start the idle Auracast broadcast: %s", err)
                self.update_state()
                return
            self._ensure_idle_silence()
            self.update_state()

    async def poll(self) -> None:
        """Monitor the configured serial port and recover the broadcast after reconnect."""
        if not self._attr_powered:
            return
        if await asyncio.to_thread(os.path.exists, self._serial_port):
            if not self._attr_available or not self._broadcaster.running:
                await self.start_broadcast()
            return
        async with self._lifecycle_lock:
            if self._attr_available or self._broadcaster.running:
                self.logger.debug(
                    "Auracast controller %s is not present; stopping broadcast", self._serial_port
                )
                await self._release_broadcast()
                self._broadcast_faulted = False
                self._attr_available = False
                self._set_idle()

    async def play_media(self, media: PlayerMedia) -> None:
        """Start streaming decoded PCM to the Auracast broadcaster."""
        async with self._lifecycle_lock:
            await self._stop_stream()
            await self._stop_silence()
            try:
                await self._start_broadcast()
            except BROADCAST_ERRORS as err:
                self._attr_available = False
                self._attr_playback_state = PlaybackState.IDLE
                self._attr_current_media = None
                self.update_state()
                raise PlayerUnavailableError(
                    f"Unable to open the Auracast controller on {self._serial_port}: {err}"
                ) from err
            self._attr_powered = True
            if self._broadcaster.controller_stack:
                self._attr_device_info.model = (
                    f"Host stack: Bumble; Controller stack: {self._broadcaster.controller_stack}"
                )
            self._attr_current_media = media
            self._attr_playback_state = PlaybackState.PLAYING
            self._streamed_seconds = 0.0
            self._attr_elapsed_time = 0.0
            self._attr_elapsed_time_last_updated = time.time()
            self._resume_event.set()
            task = asyncio.create_task(
                self._stream_audio(media), name=f"auracast_stream_{self.player_id}"
            )
            task.add_done_callback(self._on_stream_task_done)
            self._stream_task = task
            self.update_state()

    async def play(self) -> None:
        """Resume a paused broadcast stream."""
        if self._stream_task and not self._stream_task.done():
            self._resume_event.set()
            self._attr_playback_state = PlaybackState.PLAYING
            self._attr_elapsed_time_last_updated = time.time()
            self.update_state()

    async def pause(self) -> None:
        """Pause the source stream while keeping the broadcast active."""
        if self._stream_task and not self._stream_task.done():
            self._resume_event.clear()
            self._attr_playback_state = PlaybackState.PAUSED
            self._attr_elapsed_time = max(
                0.0, self._streamed_seconds - self._broadcaster.queued_seconds
            )
            self._attr_elapsed_time_last_updated = time.time()
            self.update_state()

    async def volume_set(self, volume_level: int) -> None:
        """
        Set software playback volume before LC3 encoding.

        :param volume_level: Volume level from 0 to 100.
        """
        self._attr_volume_level = max(0, min(100, volume_level))
        self.mass.config.set_raw_player_config_value(
            self.player_id, CONF_STORED_VOLUME, self._attr_volume_level
        )
        self.update_state()

    async def stop(self) -> None:
        """Stop streaming and release the controller transport."""
        async with self._lifecycle_lock:
            await self._release_broadcast()
            self._set_idle()

    async def power(self, powered: bool) -> None:
        """Power the broadcast player on or stop and power it off."""
        if powered:
            self._attr_powered = True
            await self.start_broadcast()
        else:
            await self.stop()
            self._attr_powered = False
        self.update_state()

    async def on_unload(self) -> None:
        """Stop playback and release controller resources when removed."""
        await self.stop()

    async def _stream_audio(self, media: PlayerMedia) -> None:
        """Forward PCM for the media to the broadcaster until the stream ends."""
        source = self.mass.streams.get_stream(media, PCM_FORMAT, self.player_id)
        pcm_stream = self._apply_output_filters(source, media)
        pending = bytearray()
        try:
            async with aclosing(source), aclosing(pcm_stream):
                async for chunk in pcm_stream:
                    pending.extend(chunk)
                    aligned_size = len(pending) - len(pending) % PCM_FRAME_BYTES
                    aligned = bytes(pending[:aligned_size])
                    del pending[:aligned_size]
                    for pcm_slice in iter_pcm_slices(aligned, PCM_FORMAT, FRAME_DURATION_MS):
                        await self._wait_while_paused()
                        volume_level = self._attr_volume_level
                        await self._broadcaster.send_pcm(
                            _apply_volume(pcm_slice, 100 if volume_level is None else volume_level)
                        )
                        self._streamed_seconds += len(pcm_slice) / PCM_BYTES_PER_SECOND
                await self._broadcaster.drain()
        except MusicAssistantError as err:
            self.logger.error("Auracast audio stream failed: %s", err)
        except BROADCAST_ERRORS as err:
            self.logger.error("Auracast broadcast failed: %s", err)
            self._attr_available = False
            self._broadcast_faulted = True

    async def _wait_while_paused(self) -> None:
        """Keep the broadcast fed with silence for as long as playback is paused."""
        while not self._resume_event.is_set():
            await self._broadcaster.send_pcm(SILENCE)
            await asyncio.sleep(FRAME_DURATION_MS / 1000)

    def _apply_output_filters(
        self, source: AsyncGenerator[bytes], media: PlayerMedia
    ) -> AsyncGenerator[bytes]:
        """Apply the player's DSP/EQ filter chain to the PCM source."""
        output_plan = self.mass.streams.audio.get_player_output_plan(
            self.player_id,
            PCM_FORMAT,
            PCM_FORMAT,
            queue_id=media.source_id,
            session_id=get_media_session_id(media),
            queue_item_id=media.queue_item_id,
        )
        if not output_plan.filter_params:
            return source
        return get_ffmpeg_stream(
            audio_input=source,
            input_format=PCM_FORMAT,
            output_format=PCM_FORMAT,
            filter_params=output_plan.filter_params,
        )

    def _on_stream_task_done(self, task: asyncio.Task[None]) -> None:
        """Hand a stream that ended on its own over to the lifecycle owner for cleanup."""
        if not task.cancelled():
            self.mass.create_task(self._finish_stream(task))

    async def _finish_stream(self, task: asyncio.Task[None]) -> None:
        """Keep an idle broadcast alive after the stream ends on its own."""
        async with self._lifecycle_lock:
            if self._stream_task is not task:
                return
            self._stream_task = None
            if self._broadcast_faulted:
                await self._broadcaster.stop()
                self._set_idle()
                return
            self._set_idle()
            self._ensure_idle_silence()

    async def _start_broadcast(self) -> None:
        """Ensure the broadcaster is running and refresh controller details."""
        if self._broadcast_faulted:
            await self._broadcaster.stop()
            self._broadcast_faulted = False
        if not self._broadcaster.running:
            await self._broadcaster.start()
        self._attr_available = True
        self._attr_powered = True
        if self._broadcaster.controller_stack:
            self._attr_device_info.model = (
                f"Host stack: Bumble; Controller stack: {self._broadcaster.controller_stack}"
            )

    def _ensure_idle_silence(self) -> None:
        """Schedule the silent audio frames needed to keep an idle broadcast active."""
        if self._silence_task is None or self._silence_task.done():
            task = asyncio.create_task(
                self._send_idle_silence(), name=f"auracast_silence_{self.player_id}"
            )
            task.add_done_callback(self._on_silence_task_done)
            self._silence_task = task

    def _on_silence_task_done(self, task: asyncio.Task[None]) -> None:
        """Schedule controller cleanup if the idle keepalive failed."""
        if self._silence_task is task:
            self._silence_task = None
        if self._broadcast_faulted and not task.cancelled():
            self.mass.create_task(self._teardown_faulted_broadcast())

    async def _send_idle_silence(self) -> None:
        """Keep the Auracast broadcast active with silent LC3 frames between tracks."""
        try:
            while self._broadcaster.running:
                await self._broadcaster.send_pcm(SILENCE)
                await asyncio.sleep(FRAME_DURATION_MS / 1000)
        except asyncio.CancelledError:
            raise
        except BROADCAST_ERRORS as err:
            self.logger.error("Auracast idle broadcast failed: %s", err)
            self._attr_available = False
            self._broadcast_faulted = True
            self.update_state()

    async def _teardown_faulted_broadcast(self) -> None:
        """Close a controller transport after an idle broadcast failure."""
        async with self._lifecycle_lock:
            if self._broadcast_faulted:
                await self._broadcaster.stop()
                self._set_idle()

    async def _release_broadcast(self) -> None:
        """
        Cancel and join the stream task, then close the controller.

        The only place the stream task and the broadcaster are torn down; callers must hold
        the lifecycle lock so a new broadcast can never open a controller still being closed.
        """
        await self._stop_stream()
        await self._stop_silence()
        await self._broadcaster.stop()

    async def _stop_stream(self) -> None:
        """Cancel and join the current audio stream, if any."""
        self._resume_event.set()
        if self._stream_task is not None:
            task, self._stream_task = self._stream_task, None
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    async def _stop_silence(self) -> None:
        """Cancel and join the idle broadcast keepalive, if any."""
        if self._silence_task is not None:
            task, self._silence_task = self._silence_task, None
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    def _set_idle(self) -> None:
        """Report the player as idle with no media."""
        self._attr_current_media = None
        self._attr_playback_state = PlaybackState.IDLE
        self._attr_elapsed_time = None
        self._attr_elapsed_time_last_updated = None
        self.update_state()


def _apply_volume(pcm: bytes, volume_level: int) -> bytes:
    """Scale little-endian signed 16-bit PCM by a 0..100 volume level."""
    if volume_level >= 100:
        return pcm
    if volume_level <= 0:
        return bytes(len(pcm))
    samples = np.frombuffer(pcm, dtype="<i2")
    return np.rint(samples * (volume_level / 100)).astype("<i2").tobytes()
