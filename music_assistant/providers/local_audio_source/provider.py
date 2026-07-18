"""Local Audio Source provider implementation."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING, cast
from uuid import uuid4

from music_assistant_models.enums import (
    ContentType,
    ImageType,
    MediaType,
    SourceControl,
    StreamType,
)
from music_assistant_models.errors import AudioError, MediaNotFoundError
from music_assistant_models.media_items import (
    AudioFormat,
    AudioSource,
    MediaItemImage,
    MediaItemMetadata,
    ProviderMapping,
)
from music_assistant_models.streamdetails import StreamDetails, StreamMetadata

from music_assistant.models.plugin import PluginProvider

from .constants import (
    AUDIO_SOURCE_ID,
    CHANNELS,
    CONF_FRIENDLY_NAME,
    CONF_ICON_PRESET,
    CONF_INPUT_DEVICE,
    CONF_THUMBNAIL_IMAGE,
    ICON_PRESET_CUSTOM,
    ICON_PRESETS,
    PAUSE_DEBOUNCE_S,
    SAMPLE_RATE_HZ,
    SUPPORTED_FEATURES,
)
from .pa_simple import PASimpleRecordStream

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant

# directory holding the bundled preset icon svgs, keyed by ICON_PRESETS
_IMAGES_DIR = Path(__file__).parent / "images"

# capture chunk duration — matches the streams controller's own
# AUDIO_SOURCE_CHUNK_SECONDS fast-path granularity for consistent latency
_CHUNK_MS = 20


class LocalAudioSourceProvider(PluginProvider):
    """Realtime PulseAudio/PipeWire capture plugin, exposed as a Music Assistant AudioSource."""

    def __init__(
        self, mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
    ) -> None:
        """Initialize MusicProvider."""
        super().__init__(mass, manifest, config, SUPPORTED_FEATURES)

        # resolve config
        self._pa_source: str = cast("str", self.config.get_value(CONF_INPUT_DEVICE))
        self._friendly_name: str = cast("str", self.config.get_value(CONF_FRIENDLY_NAME))
        self._icon_preset: str = cast(
            "str", self.config.get_value(CONF_ICON_PRESET) or ICON_PRESET_CUSTOM
        )
        self._thumbnail_image: str = cast("str", self.config.get_value(CONF_THUMBNAIL_IMAGE) or "")

        # fixed audio params
        self._sample_rate: int = SAMPLE_RATE_HZ
        self._channels: int = CHANNELS

        # runtime state
        self._capture_stream: PASimpleRecordStream | None = None
        self._capture_lock = asyncio.Lock()
        self._paused = False
        self._active_stream_id: str = ""
        # tracks which queue currently owns the exclusive AudioSource. Set in
        # on_source_selected (NOT in get_stream_details — that path also runs
        # from queue preload, where claiming would block a later cross-queue
        # handoff).
        self._in_use_by_queue: str | None = None
        # _active_session_id is the controller-provided token for the current
        # stream request — used to reject stale on_source_unselected callbacks
        # after a same-queue reconnect supersedes the previous request.
        self._active_session_id: str | None = None

        self._audio_format = AudioFormat(
            content_type=ContentType.PCM_S16LE,
            codec_type=ContentType.PCM_S16LE,
            sample_rate=self._sample_rate,
            bit_depth=16,
            channels=self._channels,
        )

        image = self._build_image()

        self._audio_source = AudioSource(
            item_id=AUDIO_SOURCE_ID,
            provider=self.instance_id,
            name=self._friendly_name,
            metadata=MediaItemMetadata(images=[image]) if image else MediaItemMetadata(),
            provider_mappings={
                ProviderMapping(
                    item_id=AUDIO_SOURCE_ID,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                    audio_format=self._audio_format,
                )
            },
            can_play_pause=True,
            can_seek=False,
            can_next_previous=False,
            exclusive=True,
            allow_external_trigger=False,
            # capture only starts once a player selects this source
            can_initiate=True,
        )

    def _build_image(self) -> MediaItemImage | None:
        """Build the AudioSource thumbnail from the configured preset or custom URL."""
        if self._icon_preset != ICON_PRESET_CUSTOM:
            if self._icon_preset not in ICON_PRESETS:
                self.logger.warning("Unknown icon preset: %s", self._icon_preset)
                return None
            return MediaItemImage(
                type=ImageType.THUMB,
                path=f"{self._icon_preset}.svg",
                provider=self.instance_id,
                remotely_accessible=False,
            )
        if self._thumbnail_image.startswith(("http://", "https://")):
            return MediaItemImage(
                type=ImageType.THUMB,
                path=self._thumbnail_image,
                provider=self.instance_id,
                remotely_accessible=True,
            )
        if self._thumbnail_image:
            self.logger.warning(
                "Only URLs are supported for thumbnail images. Ignoring: %s",
                self._thumbnail_image,
            )
        return None

    async def resolve_image(self, path: str) -> str | bytes:
        """Resolve a bundled preset icon path to its on-disk SVG file."""
        # only ever called with paths we generated ourselves in _build_image,
        # but validate against the known preset filenames regardless
        if path in {f"{key}.svg" for key in ICON_PRESETS}:
            return str(_IMAGES_DIR / path)
        return path

    @property
    def instance_name_postfix(self) -> str | None:
        """Return a (default) instance name postfix for this provider instance."""
        return self._friendly_name

    async def unload(self, is_removed: bool = False) -> None:
        """Handle close/cleanup of the provider."""
        self.logger.debug("Unloading plugin")
        async with self._capture_lock:
            await self._stop_capture_stream()

    async def get_audio_sources(self) -> list[AudioSource]:
        """Return the single AudioSource this plugin exposes."""
        return [self._audio_source]

    async def get_stream_details(self, source_id: str, queue_id: str) -> StreamDetails:
        """
        Return StreamDetails for streaming the captured PCM audio to a queue.

        Side-effect-free: ownership is claimed in on_source_selected (which the
        streams controller fires before this method on the actual stream
        request). Keeping this idempotent means preload paths like
        player_queues._load_item can fetch streamdetails without claiming the
        source and blocking a subsequent cross-queue handoff.
        """
        if source_id != AUDIO_SOURCE_ID:
            raise MediaNotFoundError(f"Unknown AudioSource: {source_id}")
        return StreamDetails(
            provider=self.instance_id,
            item_id=source_id,
            audio_format=self._audio_format,
            media_type=MediaType.AUDIO_SOURCE,
            stream_type=StreamType.CUSTOM,
            stream_metadata=StreamMetadata(
                title=self._friendly_name,
                artist=self._pa_source,
            ),
        )

    async def get_audio_stream(
        self, streamdetails: StreamDetails, seek_position: int = 0
    ) -> AsyncGenerator[bytes]:
        """Yield raw PCM chunks captured from the configured PulseAudio/PipeWire source."""
        _stream_id = str(uuid4())
        self._active_stream_id = _stream_id
        self._paused = False
        consumer_queue = self._in_use_by_queue
        # Snapshot the active session id so a same-queue reconnect (which
        # refreshes _active_session_id but not _in_use_by_queue) supersedes
        # this stream: the loop exits and the finally release skips so it
        # doesn't clobber the new session's claim.
        captured_session_id = self._active_session_id

        bytes_per_sec = self._sample_rate * self._channels * 2  # 16-bit PCM
        period_s = _CHUNK_MS / 1000
        chunk_size = max(256, int(bytes_per_sec * period_s))

        _stream_details = f"ID: {_stream_id}//Queue: {consumer_queue}//Source: {self._pa_source}"
        _stream_acquired = False

        self.logger.debug("Ready to capture local audio stream: %s", _stream_details)

        try:
            while True:
                if self._should_stop_stream(
                    _stream_id, consumer_queue, captured_session_id, _stream_details
                ):
                    break

                if self._paused:
                    async with self._capture_lock:
                        await self._stop_capture_stream()
                    await asyncio.sleep(PAUSE_DEBOUNCE_S)
                    continue

                chunk = await self._capture_one_chunk(chunk_size, _stream_acquired)
                if chunk is None:
                    continue

                if not _stream_acquired:
                    _stream_acquired = True
                    self.logger.debug("Acquired local audio capture stream: %s", _stream_details)

                yield chunk
        finally:
            if (
                self._in_use_by_queue == consumer_queue
                and self._active_session_id == captured_session_id
            ):
                self._in_use_by_queue = None
            async with self._capture_lock:
                await self._stop_capture_stream()
            self.logger.debug("Stopped local audio capture: %s", _stream_details)

    async def _capture_one_chunk(self, chunk_size: int, stream_acquired: bool) -> bytes | None:
        """
        Ensure the capture stream is open and read one chunk from it.

        Returns None if the caller should just loop again (transient hiccup
        while (re)opening the stream). Raises AudioError if the configured
        source can't be opened at all, matching the fail-fast contract other
        AudioSource plugins follow.

        Once open, the actual read blocks at the source's own real-time
        pace and is intentionally NOT wrapped in an asyncio timeout: unlike
        AsyncProcess.read() over a pipe, pa_simple_read() is a synchronous
        C call run via run_in_executor — an asyncio-side timeout can only
        abandon the awaiting future, it can't interrupt the blocked C call,
        so a timeout here would just leak an executor thread rather than
        actually recovering. pa_simple_new() already fails fast in
        _start_capture_stream() if the source name is invalid or
        unreachable, which covers the case a read-level timeout was meant
        to catch.
        """
        async with self._capture_lock:
            if not self._capture_stream:
                self._capture_stream = await self._start_capture_stream()

        if not self._capture_stream:
            if not stream_acquired:
                self._raise_no_audio_error("Failed to open capture stream")
            await asyncio.sleep(0.5)
            return None

        loop = asyncio.get_running_loop()
        try:
            return await loop.run_in_executor(None, self._capture_stream.read, chunk_size)
        except OSError as err:
            self.logger.warning("Capture read failed, reopening stream: %s", err)
            async with self._capture_lock:
                await self._stop_capture_stream()
            await asyncio.sleep(0.05)
            return None

    def _raise_no_audio_error(self, reason: str) -> None:
        """Raise a fail-fast AudioError when the configured source can't be opened."""
        raise AudioError(
            f"{reason} on source {self._pa_source!r}",
            translation_key="no_packets",
            translation_owner=self.translation_owner,
            translation_args=[self._pa_source, self._friendly_name],
        )

    def _should_stop_stream(
        self,
        stream_id: str,
        consumer_queue: str | None,
        captured_session_id: str | None,
        stream_details: str,
    ) -> bool:
        """Check whether the current get_audio_stream loop should stop and log why."""
        if self._in_use_by_queue != consumer_queue:
            self.logger.debug(
                "Stopping local audio capture: %s - Reason: plugin is no longer in use by queue %s",
                stream_details,
                consumer_queue,
            )
            return True
        if self._active_session_id != captured_session_id:
            self.logger.debug(
                "Stopping local audio capture: %s - Reason: same-queue reconnect superseded "
                "this session",
                stream_details,
            )
            return True
        if self._active_stream_id != stream_id:
            self.logger.debug(
                "Stopping local audio capture: %s - Reason: stream_id changed, this is a "
                "stale stream reader",
                stream_details,
            )
            return True
        return False

    async def on_source_control(
        self,
        source_id: str,
        action: SourceControl,
        value: int | None = None,
    ) -> None:
        """Handle a playback control command for the active AudioSource."""
        if source_id != AUDIO_SOURCE_ID:
            return
        if action == SourceControl.PLAY:
            self._paused = False
        elif action == SourceControl.PAUSE:
            self._paused = True

    async def on_source_selected(
        self, source_id: str, player_id: str, queue_id: str, stream_session_id: str
    ) -> None:
        """Claim the source for this queue and let any prior stream wind down."""
        if source_id != AUDIO_SOURCE_ID:
            return
        # Claim ownership for this queue. The lock lives here (not in
        # get_stream_details) so preload paths can fetch streamdetails without
        # accidentally blocking a subsequent cross-queue handoff at the actual
        # stream request. There is no separate stop-previous-player step: this
        # source only has a single passive PulseAudio/PipeWire capture, so the
        # previous queue's get_audio_stream loop notices the queue change on
        # its own and exits cleanly.
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

    async def _start_capture_stream(self) -> PASimpleRecordStream | None:
        """Open a new PulseAudio/PipeWire capture stream via libpulse-simple."""
        self.logger.debug(
            "Opening capture stream for %s (source=%s sr=%d ch=%d)",
            self._friendly_name,
            self._pa_source,
            self._sample_rate,
            self._channels,
        )
        pa_source = self._pa_source
        app_name = f"music-assistant-{self.instance_id}"
        sample_rate = self._sample_rate
        channels = self._channels
        loop = asyncio.get_running_loop()
        try:
            return await loop.run_in_executor(
                None,
                lambda: PASimpleRecordStream(
                    source_name=pa_source,
                    app_name=app_name,
                    rate=sample_rate,
                    channels=channels,
                ),
            )
        except OSError as err:
            self.logger.error("Failed to open capture stream: %s", err)
            return None

    async def _stop_capture_stream(self) -> None:
        """Close the running capture stream, if any. Caller holds _capture_lock."""
        if self._capture_stream:
            stream, self._capture_stream = self._capture_stream, None
            loop = asyncio.get_running_loop()
            with suppress(Exception):
                await loop.run_in_executor(None, stream.close)
