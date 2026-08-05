"""VBAN receiver provider implementation."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING, Any, cast
from uuid import uuid4

from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption
from music_assistant_models.enums import ConfigEntryType, ContentType, MediaType, StreamType
from music_assistant_models.errors import AudioError, MediaNotFoundError, SetupFailedError
from music_assistant_models.media_items import AudioFormat, AudioSource, ProviderMapping
from music_assistant_models.streamdetails import StreamDetails, StreamMetadata

from music_assistant.constants import (
    CONF_BIND_IP,
    CONF_BIND_PORT,
    CONF_ENTRY_WARN_PREVIEW,
    VERBOSE_LOG_LEVEL,
)
from music_assistant.models.plugin import PluginProvider

from .constants import (
    CONF_AUDIO_CHANNELS,
    CONF_LOG_VBAN_STREAM_STATS,
    CONF_PCM_AUDIO_FORMAT,
    CONF_PCM_SAMPLE_RATE,
    CONF_SENDER_HOST,
    CONF_VBAN_QUEUE_SIZE,
    CONF_VBAN_QUEUE_STRATEGY,
    CONF_VBAN_STREAM_NAME,
    DEFAULT_AUDIO_CHANNELS,
    DEFAULT_PCM_AUDIO_FORMAT,
    DEFAULT_PCM_SAMPLE_RATE,
    DEFAULT_UDP_PORT,
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


# stable id for the single AudioSource this provider exposes;
# combined with the provider instance_id this forms the persistent uri
AUDIO_SOURCE_ID = "main"


class VBANReceiverProvider(PluginProvider):
    """Implementation of a VBAN protocol receiver plugin."""

    def __init__(
        self, mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
    ) -> None:
        """Initialize MusicProvider."""
        super().__init__(mass, manifest, config, SUPPORTED_FEATURES)
        # Setup values fall back to legacy option values for existing instances.
        self._bind_port: int = cast("int", self.get_setup_value(CONF_BIND_PORT) or DEFAULT_UDP_PORT)
        self._bind_ip: str = cast("str", self.get_setup_value(CONF_BIND_IP) or "0.0.0.0")
        self._sender_host: str = cast("str", self.get_setup_value(CONF_SENDER_HOST) or "127.0.0.1")
        self._vban_stream_name: str = cast(
            "str", self.get_setup_value(CONF_VBAN_STREAM_NAME) or "Network AUX"
        )
        self._pcm_audio_format: str = cast(
            "str", self.get_setup_value(CONF_PCM_AUDIO_FORMAT) or DEFAULT_PCM_AUDIO_FORMAT
        )
        self._pcm_sample_rate: int = cast(
            "int", self.get_setup_value(CONF_PCM_SAMPLE_RATE) or DEFAULT_PCM_SAMPLE_RATE
        )
        self._audio_channels: int = cast(
            "int", self.get_setup_value(CONF_AUDIO_CHANNELS) or DEFAULT_AUDIO_CHANNELS
        )
        self._vban_queue_strategy: BackPressureStrategy = VBAN_QUEUE_STRATEGIES[
            cast(
                "str",
                self.config.get_value(CONF_VBAN_QUEUE_STRATEGY)
                or next(iter(VBAN_QUEUE_STRATEGIES)),
            )
        ]
        self._vban_queue_size: int = cast(
            "int",
            self.config.get_value(CONF_VBAN_QUEUE_SIZE) or AsyncVBANClientMod.default_queue_size,
        )
        self._log_stats: bool = cast("bool", self.config.get_value(CONF_LOG_VBAN_STREAM_STATS))

        self._vban_receiver: AsyncVBANClientMod | None = None
        self._vban_stream: VBANIncomingStream | None = None
        self._udp_socket_fut: asyncio.Future[Any] | None = None
        self._stats_reporter: VBANStatsReporter | None = None
        self._active_stream_id: str = ""
        self._in_use_by_queue: str | None = None
        # _active_session_id is the controller-provided token for the current
        # stream request — used to reject stale on_source_unselected callbacks
        # after a same-queue reconnect supersedes the previous request.
        self._active_session_id: str | None = None

        self._audio_format = AudioFormat(
            content_type=ContentType(self._pcm_audio_format.lower()),
            codec_type=ContentType(self._pcm_audio_format.lower()),
            sample_rate=self._pcm_sample_rate,
            bit_depth=get_supported_pcm_formats()[self._pcm_audio_format],
            channels=self._audio_channels,
        )
        self._audio_source = AudioSource(
            item_id=AUDIO_SOURCE_ID,
            provider=self.instance_id,
            name=f"{self.manifest.name}: {self._vban_stream_name}",
            provider_mappings={
                ProviderMapping(
                    item_id=AUDIO_SOURCE_ID,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                    audio_format=self._audio_format,
                )
            },
            can_play_pause=False,
            can_seek=False,
            can_next_previous=False,
            exclusive=True,
            allow_external_trigger=False,
            # MA opens the UDP listener on demand; get_audio_stream raises if
            # the configured sender never sends
            can_initiate=True,
        )

    async def get_config_entries(self) -> tuple[ConfigEntry, ...]:
        """Return runtime options for this provider."""
        return (
            CONF_ENTRY_WARN_PREVIEW,
            ConfigEntry(
                key=CONF_VBAN_QUEUE_STRATEGY,
                type=ConfigEntryType.STRING,
                default_value=next(iter(VBAN_QUEUE_STRATEGIES)),
                options=[ConfigValueOption(x, title=x) for x in VBAN_QUEUE_STRATEGIES],
                advanced=True,
                required=True,
            ),
            ConfigEntry(
                key=CONF_VBAN_QUEUE_SIZE,
                type=ConfigEntryType.INTEGER,
                default_value=AsyncVBANClientMod.default_queue_size,
                advanced=True,
                required=True,
            ),
            ConfigEntry(
                key=CONF_LOG_VBAN_STREAM_STATS,
                type=ConfigEntryType.BOOLEAN,
                default_value=False,
                advanced=True,
                required=True,
            ),
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
                pcm_sample_size=self._audio_format.pcm_sample_size
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

        if self._in_use_by_queue:
            # Allow the running stream to stop cleanly
            self._in_use_by_queue = None
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

    async def get_audio_sources(self) -> list[AudioSource]:
        """Return the single AudioSource this VBAN receiver exposes."""
        return [self._audio_source]

    async def get_stream_details(self, item_id: str, media_type: MediaType) -> StreamDetails:
        """
        Return StreamDetails for streaming the VBAN PCM audio to a queue.

        Side-effect-free: ownership is claimed in on_source_selected (which the
        streams controller fires before this method on the actual stream
        request). Keeping this idempotent means preload paths like
        player_queues._load_item can fetch streamdetails without claiming the
        source and blocking a subsequent cross-queue handoff.
        """
        if item_id != AUDIO_SOURCE_ID:
            raise MediaNotFoundError(f"Unknown AudioSource: {item_id}")
        return StreamDetails(
            provider=self.instance_id,
            item_id=item_id,
            audio_format=self._audio_format,
            media_type=MediaType.AUDIO_SOURCE,
            stream_type=StreamType.CUSTOM,
            stream_metadata=StreamMetadata(
                title=self._vban_stream_name,
                artist=self._sender_host,
            ),
        )

    async def get_audio_stream(  # noqa: PLR0915
        self, streamdetails: StreamDetails, seek_position: int = 0
    ) -> AsyncGenerator[bytes]:
        """Yield raw PCM chunks from the VBANIncomingStream queue."""
        assert self._vban_stream  # for type checking
        assert self._udp_socket_fut  # for type checking
        _stream_id = str(uuid4())
        self._active_stream_id = _stream_id
        consumer_queue = self._in_use_by_queue
        # Snapshot the active session id so a same-queue reconnect (which
        # refreshes _active_session_id but not _in_use_by_queue) supersedes
        # this stream: the loop exits and the finally release skips so it
        # doesn't clobber the new session's claim.
        captured_session_id = self._active_session_id
        _stream_details = (
            f"ID: {_stream_id}//Queue: {consumer_queue}//"
            f"Stream: {self._vban_stream_name}//"
            f"Config: {self._audio_format.output_format_str}"
        )
        _stream_acquired = False

        # Drain any leftovers in the queue from previous use
        while self._vban_stream.get_packet_nowait():
            pass

        if self._stats_reporter:
            self._stats_reporter.start(stream_id=_stream_id, stream_details=_stream_details)

        self.logger.debug("Ready to receive VBAN PCM audio stream: %s", _stream_details)

        try:
            while True:
                if self._in_use_by_queue != consumer_queue:
                    self.logger.debug(
                        "Stopping VBAN PCM audio stream receiver: %s - Reason: plugin is no "
                        "longer in use by queue %s",
                        _stream_details,
                        consumer_queue,
                    )
                    break
                if self._active_session_id != captured_session_id:
                    self.logger.debug(
                        "Stopping VBAN PCM audio stream receiver: %s - Reason: same-queue "
                        "reconnect superseded this session",
                        _stream_details,
                    )
                    break
                if self._active_stream_id != _stream_id:
                    self.logger.debug(
                        "Stopping VBAN PCM audio stream receiver: %s - Reason: stream_id has "
                        "changed from %s to %s meaning %s is a stale stream reader which was "
                        "not cleanly closed",
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
                    # cold-start: fail fast if the configured sender never sends
                    if not _stream_acquired:
                        raise AudioError(
                            f"VBAN sender {self._sender_host!r} did not send any packets "
                            f"on stream {self._vban_stream_name!r}",
                            translation_key="no_packets",
                            translation_owner=self.translation_owner,
                            translation_args=[self._sender_host, self._vban_stream_name],
                        ) from None
                    continue
                except asyncio.QueueShutDown:
                    self.logger.error(
                        "Found VBANIncomingStream queue shut down when attempting to get VBAN "
                        "packet for audio stream: %s",
                        _stream_details,
                    )
                    break
        finally:
            self._cancel_stats_reporter(_stream_id)
            # Guard release on BOTH queue id AND session id so a stale generator
            # teardown after a same-queue reconnect doesn't clear the new
            # session's claim.
            if (
                self._in_use_by_queue == consumer_queue
                and self._active_session_id == captured_session_id
            ):
                self._in_use_by_queue = None
            self.logger.debug("Stopped VBAN PCM audio stream receiver: %s", _stream_details)

    async def on_source_selected(
        self, source_id: str, player_id: str, queue_id: str, stream_session_id: str
    ) -> None:
        """Claim the source for this queue and let any prior stream wind down."""
        if source_id != AUDIO_SOURCE_ID:
            return
        # Claim ownership for this queue. The lock lives here (not in
        # get_stream_details) so preload paths can fetch streamdetails without
        # accidentally blocking a subsequent cross-queue handoff at the actual
        # stream request. There is no cmd_stop on a previous player like the
        # Spotify Connect / AirPlay / Yandex receivers do: VBAN is a purely
        # passive UDP receiver with no concept of an "active player" — the
        # previous queue's get_audio_stream loop notices the queue change on
        # its 1s timeout and exits cleanly on its own.
        self._in_use_by_queue = queue_id
        # Record this request's session id so a later on_source_unselected can
        # tell whether it is the live teardown or a stale callback from a
        # superseded same-queue request.
        self._active_session_id = stream_session_id

    async def on_source_unselected(
        self, source_id: str, queue_id: str, stream_session_id: str
    ) -> None:
        """Release the queue-scoped exclusive claim when MA tears down the stream."""
        if source_id != AUDIO_SOURCE_ID:
            return
        # Reject stale callbacks: only release if this is still the active
        # session. A queue_id check alone is not sufficient — same-queue
        # reconnects (player drops + reopens the same stream URL before the
        # original request's finally fires) would otherwise let the old
        # request's late callback clear the live claim of the new stream.
        if self._active_session_id != stream_session_id:
            return
        self._active_session_id = None
        if self._in_use_by_queue == queue_id:
            self._in_use_by_queue = None

    def _cancel_stats_reporter(self, instance_id: str | None = None) -> None:
        """Cancel a running stats reporter."""
        if self._stats_reporter:
            self.logger.debug("Cancelling stats reporter")
            self._stats_reporter.cancel(instance_id)
