"""VBAN receiver provider implementation."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING, Any, cast
from uuid import uuid4

from music_assistant_models.enums import ContentType, StreamType
from music_assistant_models.errors import SetupFailedError
from music_assistant_models.media_items import AudioFormat
from music_assistant_models.streamdetails import StreamMetadata

from music_assistant.constants import CONF_BIND_IP, CONF_BIND_PORT, VERBOSE_LOG_LEVEL
from music_assistant.models.plugin import PluginProvider, PluginSource

from .constants import (
    CONF_AUDIO_CHANNELS,
    CONF_LOG_VBAN_STREAM_STATS,
    CONF_PCM_AUDIO_FORMAT,
    CONF_PCM_SAMPLE_RATE,
    CONF_SENDER_HOST,
    CONF_VBAN_QUEUE_SIZE,
    CONF_VBAN_QUEUE_STRATEGY,
    CONF_VBAN_STREAM_NAME,
    SUPPORTED_FEATURES,
    VBAN_QUEUE_STRATEGIES,
)
from .helpers import get_supported_pcm_formats
from .stats import VBANStatsReporter
from .vban import AsyncVBANClientMod

if TYPE_CHECKING:
    from aiovban.asyncio.streams import VBANIncomingStream
    from aiovban.asyncio.util import BackPressureStrategy
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant


class VBANReceiverProvider(PluginProvider):
    """Implementation of a VBAN protocol receiver plugin."""

    def __init__(
        self, mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
    ) -> None:
        """Initialize MusicProvider."""
        super().__init__(mass, manifest, config, SUPPORTED_FEATURES)
        self._bind_port: int = cast("int", self.config.get_value(CONF_BIND_PORT))
        self._bind_ip: str = cast("str", self.config.get_value(CONF_BIND_IP))
        self._sender_host: str = cast("str", self.config.get_value(CONF_SENDER_HOST))
        self._vban_stream_name: str = cast("str", self.config.get_value(CONF_VBAN_STREAM_NAME))
        self._pcm_audio_format: str = cast("str", self.config.get_value(CONF_PCM_AUDIO_FORMAT))
        self._pcm_sample_rate: int = cast("int", self.config.get_value(CONF_PCM_SAMPLE_RATE))
        self._audio_channels: int = cast("int", self.config.get_value(CONF_AUDIO_CHANNELS))
        self._vban_queue_strategy: BackPressureStrategy = VBAN_QUEUE_STRATEGIES[
            cast("str", self.config.get_value(CONF_VBAN_QUEUE_STRATEGY))
        ]
        self._vban_queue_size: int = cast("int", self.config.get_value(CONF_VBAN_QUEUE_SIZE))
        self._log_stats: bool = cast("bool", self.config.get_value(CONF_LOG_VBAN_STREAM_STATS))

        self._vban_receiver: AsyncVBANClientMod | None = None
        self._vban_stream: VBANIncomingStream | None = None
        self._udp_socket_fut: asyncio.Future[Any] | None = None
        self._stats_reporter: VBANStatsReporter | None = None
        self._active_stream_id: str = ""

        self._source_details = PluginSource(
            id=self.instance_id,
            name=f"{self.manifest.name}: {self._vban_stream_name}",
            passive=False,
            can_play_pause=False,
            can_seek=False,
            can_next_previous=False,
            audio_format=AudioFormat(
                content_type=ContentType(self._pcm_audio_format.lower()),
                codec_type=ContentType(self._pcm_audio_format.lower()),
                sample_rate=self._pcm_sample_rate,
                bit_depth=get_supported_pcm_formats()[self._pcm_audio_format],
                channels=self._audio_channels,
            ),
            metadata=StreamMetadata(
                title=self._vban_stream_name,
                artist=self._sender_host,
            ),
            stream_type=StreamType.CUSTOM,
        )

    @property
    def instance_name_postfix(self) -> str | None:
        """Return a (default) instance name postfix for this provider instance."""
        return self._vban_stream_name

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        # Set-up aiovban logging - DEBUG level is noisy
        if self.logger.isEnabledFor(VERBOSE_LOG_LEVEL):
            logging.getLogger("aiovban").setLevel(logging.DEBUG)
        else:
            logging.getLogger("aiovban").setLevel(logging.INFO)

        if self._log_stats and (
            self.logger.isEnabledFor(logging.DEBUG) or self.logger.isEnabledFor(VERBOSE_LOG_LEVEL)
        ):
            self._stats_reporter = VBANStatsReporter(
                pcm_sample_size=self._source_details.audio_format.pcm_sample_size
            )

        self._vban_receiver = AsyncVBANClientMod(default_queue_size=self._vban_queue_size)
        try:
            self._vban_stream = (
                await self._vban_receiver.register_device(self._sender_host)
            ).receive_stream(
                self._vban_stream_name, back_pressure_strategy=self._vban_queue_strategy
            )

            self._udp_socket_fut = await self._vban_receiver.listen(
                address=self._bind_ip, port=self._bind_port, controller=self
            )
        except (OSError, ValueError) as err:
            raise SetupFailedError(f"Failed to start VBAN receiver plugin: {err}") from err

    async def unload(self, is_removed: bool = False) -> None:
        """Handle close/cleanup of the provider."""
        self.logger.debug("Unloading plugin")

        self._cancel_stats_reporter()

        if self.active_player:
            # Allow the running stream to stop cleanly
            self._source_details.in_use_by = None
            await asyncio.sleep(1)

        if self._vban_receiver:
            self.logger.debug("Closing UDP transport")
            self._vban_receiver.close()
            if self._udp_socket_fut:
                try:
                    await self._udp_socket_fut
                except Exception as err:
                    self.logger.debug("Error while closing UDP transport: %s", err)
                else:
                    self.logger.debug("Closed UDP transport")
            self._vban_receiver = None

        self._vban_stream = None

    def _cancel_stats_reporter(self, instance_id: str | None = None) -> None:
        """Cancel a running stats reporter."""
        if self._stats_reporter:
            self.logger.debug("Cancelling stats reporter")
            self._stats_reporter.cancel(instance_id)

    def get_source(self) -> PluginSource:
        """Get (audio)source details for this plugin."""
        return self._source_details

    @property
    def active_player(self) -> bool:
        """Report the active player status."""
        return bool(self._source_details.in_use_by)

    async def get_audio_stream(self, player_id: str) -> AsyncGenerator[bytes, None]:
        """Yield raw PCM chunks from the VBANIncomingStream queue."""
        assert self._vban_stream  # for type checking
        assert self._udp_socket_fut  # for type checking
        _stream_id = str(uuid4())
        self._active_stream_id = _stream_id
        _stream_details = f"ID: {_stream_id}//Player: {player_id}//Stream: {self._vban_stream_name}//Config: {self._source_details.audio_format.output_format_str}"
        _stream_acquired = False

        # Drain any leftovers in the queue from previous use
        while self._vban_stream.get_packet_nowait():
            pass

        if self._stats_reporter:
            self._stats_reporter.start(stream_id=_stream_id, stream_details=_stream_details)

        self.logger.debug("Ready to receive VBAN PCM audio stream: %s", _stream_details)

        try:
            while True:
                if self._source_details.in_use_by != player_id:
                    self.logger.debug(
                        "Stopping VBAN PCM audio stream receiver: %s - Reason: plugin is no longer in use by Player %s",
                        _stream_details,
                        player_id,
                    )
                    break
                if self._active_stream_id != _stream_id:
                    self.logger.debug(
                        "Stopping VBAN PCM audio stream receiver: %s - Reason: stream_id has changed from %s to %s meaning %s is a stale stream reader which was not cleanly closed",
                        _stream_details,
                        _stream_id,
                        self._active_stream_id,
                        _stream_id,
                    )
                    break
                if self._udp_socket_fut.done():
                    self.logger.debug(
                        "Stopping VBAN PCM audio stream receiver: %s - Reason: UDP socket closed",
                        _stream_details,
                    )
                    break

                try:
                    async with asyncio.timeout(1):
                        packet = await self._vban_stream.get_packet()
                        # Check if the stream_id has changed underneath us while waiting
                        if self._active_stream_id != _stream_id:
                            break
                    if not _stream_acquired:
                        _stream_acquired = True
                        self.logger.debug("Acquired VBAN PCM audio stream: %s", _stream_details)
                    if self._stats_reporter:
                        self._stats_reporter.update(
                            instance_id=_stream_id, vban_bytes_len=len(packet.body.data)
                        )
                    yield packet.body.data
                except TimeoutError:
                    continue
                except asyncio.QueueShutDown:
                    self.logger.error(
                        "Found VBANIncomingStream queue shut down when attempting to get VBAN packet for audio stream: %s",
                        _stream_details,
                    )
                    break
        finally:
            self._cancel_stats_reporter(_stream_id)
            self.logger.debug("Stopped VBAN PCM audio stream receiver: %s", _stream_details)
