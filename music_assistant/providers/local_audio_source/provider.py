"""Local Audio Source provider implementation."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING, cast
from uuid import uuid4

import numpy as np
from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption
from music_assistant_models.enums import (
    ConfigEntryType,
    ContentType,
    ImageType,
    MediaType,
    PlaybackState,
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
from music_assistant_models.unique_list import UniqueList

from music_assistant.constants import CONF_ENTRY_WARN_PREVIEW
from music_assistant.models.plugin import PluginProvider

from .constants import (
    AUDIO_SOURCE_ID,
    CHANNELS,
    CONF_AUTO_TRIGGER,
    CONF_FRIENDLY_NAME,
    CONF_ICON_PRESET,
    CONF_INPUT_DEVICE,
    CONF_TARGET_PLAYER_ID,
    CONF_THUMBNAIL_IMAGE,
    CONF_TRIGGER_THRESHOLD_DBFS,
    DEFAULT_TRIGGER_THRESHOLD_DBFS,
    ICON_PRESET_CUSTOM,
    ICON_PRESETS,
    PAUSE_DEBOUNCE_S,
    PLAYER_ID_AUTO,
    SAMPLE_RATE_HZ,
    SENSOR_CHUNK_MS,
    SENSOR_RETRY_S,
    SUPPORTED_FEATURES,
    TRIGGER_ATTACK_S,
    TRIGGER_PENDING_TIMEOUT_S,
    TRIGGER_RELEASE_S,
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


def _pcm_rms_dbfs(chunk: bytes) -> float:
    """
    Compute the RMS level of a 16-bit little-endian PCM chunk, in dBFS.

    0 dBFS = full scale (loudest possible sample); silence trends toward
    -inf, clamped here at -120 dBFS to keep threshold comparisons well
    defined for empty/near-empty chunks.
    """
    if not chunk:
        return -120.0
    samples = np.frombuffer(chunk, dtype=np.int16).astype(np.float64)
    rms = np.sqrt(np.mean(np.square(samples)))
    if rms <= 0:
        return -120.0
    return float(20 * np.log10(rms / 32768.0))


class LocalAudioSourceProvider(PluginProvider):
    """Realtime PulseAudio/PipeWire capture plugin, exposed as a Music Assistant AudioSource."""

    def __init__(
        self, mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
    ) -> None:
        """Initialize MusicProvider."""
        super().__init__(mass, manifest, config, SUPPORTED_FEATURES)

        self._pa_source: str = cast("str", self.get_setup_value(CONF_INPUT_DEVICE))
        self._friendly_name: str = cast(
            "str", self.get_setup_value(CONF_FRIENDLY_NAME, "Local Audio Source")
        )

        self._icon_preset: str = cast(
            "str", self.get_setup_value(CONF_ICON_PRESET, ICON_PRESET_CUSTOM)
        )
        self._thumbnail_image: str = cast(
            "str", self.get_setup_value(CONF_THUMBNAIL_IMAGE, "") or ""
        )
        self._auto_trigger: bool = bool(self.config.get_value(CONF_AUTO_TRIGGER))
        self._target_player_id: str = cast(
            "str", self.config.get_value(CONF_TARGET_PLAYER_ID) or PLAYER_ID_AUTO
        )
        self._trigger_threshold_dbfs: float = cast(
            "float",
            self.config.get_value(CONF_TRIGGER_THRESHOLD_DBFS) or DEFAULT_TRIGGER_THRESHOLD_DBFS,
        )

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
        # set by _trigger_playback() when the signal sensor auto-started a
        # queue on our own initiative; cleared on manual on_source_unselected
        # or when the sensor auto-stops it again. Only ever stop a queue via
        # the sensor if we're the ones who started it — a user-initiated
        # session (via Live Inputs) is left alone.
        self._auto_triggered_queue: str | None = None
        # timestamp (loop.time()) of the most recent _trigger_playback() call
        # that's still waiting for on_source_selected() to confirm the queue
        # actually started — see the pending-claim timeout in _sensor_loop.
        self._auto_trigger_pending_since: float = 0.0
        self._sensor_task: asyncio.Task[None] | None = None
        # Dedicated executor for all blocking libpulse-simple calls (pa_simple_new/
        # read/close), kept off MA's shared default executor so a slow/stuck
        # PulseAudio/PipeWire call from this provider can't starve unrelated work
        # elsewhere in the server. max_workers=4 (not 1): a stuck read must not be
        # able to block a queued close on the same single worker thread — see the
        # blocking-call caveat on PASimpleRecordStream itself.
        self._pa_executor = ThreadPoolExecutor(
            max_workers=4, thread_name_prefix=f"las-{self.instance_id}"
        )

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
            metadata=(
                MediaItemMetadata(images=UniqueList([image])) if image else MediaItemMetadata()
            ),
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
            allow_external_trigger=self._auto_trigger,
            # capture only starts once a player selects this source
            can_initiate=True,
        )

    async def get_config_entries(self) -> tuple[ConfigEntry, ...]:
        """Return runtime options for this provider."""
        player_options = [
            ConfigValueOption(x.player_id, title=x.display_name)
            for x in sorted(
                self.mass.players.all_players(False, False), key=lambda p: p.display_name.lower()
            )
        ]
        return (
            CONF_ENTRY_WARN_PREVIEW,
            ConfigEntry(
                key=CONF_AUTO_TRIGGER,
                type=ConfigEntryType.BOOLEAN,
                default_value=False,
                required=False,
            ),
            ConfigEntry(
                key=CONF_TARGET_PLAYER_ID,
                type=ConfigEntryType.STRING,
                options=[
                    ConfigValueOption(PLAYER_ID_AUTO),
                    *player_options,
                ],
                default_value=PLAYER_ID_AUTO,
                required=True,
                depends_on=CONF_AUTO_TRIGGER,
                depends_on_value=True,
            ),
            ConfigEntry(
                key=CONF_TRIGGER_THRESHOLD_DBFS,
                type=ConfigEntryType.FLOAT,
                default_value=DEFAULT_TRIGGER_THRESHOLD_DBFS,
                required=False,
                depends_on=CONF_AUTO_TRIGGER,
                depends_on_value=True,
            ),
        )

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

    async def handle_async_init(self) -> None:
        """Start the signal-detection sensor task, if auto-start is configured."""
        if not self._auto_trigger:
            return
        self._sensor_task = self.mass.create_task(
            self._sensor_loop(), task_id=f"local_audio_source_sensor_{self.instance_id}"
        )

    async def unload(self, is_removed: bool = False) -> None:
        """Handle close/cleanup of the provider."""
        self.logger.debug("Unloading plugin")
        if self._sensor_task and not self._sensor_task.done():
            self._sensor_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._sensor_task
        async with self._capture_lock:
            await self._stop_capture_stream()
        # wait=False: don't block provider unload on any still-running
        # blocking libpulse call (see PASimpleRecordStream's no-cancellation
        # caveat) — the stream(s) were already asked to close just above.
        self._pa_executor.shutdown(wait=False)

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
                if not chunk:
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
        # If a different queue than the one we auto-started is claiming the
        # source (manual selection elsewhere, or exclusivity handing it to a
        # new queue), our previous auto-trigger claim is stale — drop it so
        # the sensor doesn't later try to stop a queue that isn't ours
        # anymore. When _trigger_playback() itself is what led here, it
        # already set _auto_triggered_queue to this exact queue_id before
        # play_media() ran, so this check is a no-op in that case.
        if self._auto_triggered_queue and self._auto_triggered_queue != queue_id:
            self._auto_triggered_queue = None
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
        if self._auto_triggered_queue == queue_id:
            self._auto_triggered_queue = None

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
            return await loop.run_in_executor(
                self._pa_executor, self._capture_stream.read, chunk_size
            )
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

    async def _sensor_loop(self) -> None:
        """
        Watch the configured source's signal level and auto play/stop a target player.

        Runs for the lifetime of the provider whenever auto-start-on-signal is
        enabled. Uses its own PASimpleRecordStream, entirely separate from the
        one get_audio_stream() opens for actual playback — PulseAudio/PipeWire
        natively fan a source out to multiple simultaneous readers, so the
        sensor and an active playback stream can both read the same source at
        once without conflict.
        """
        loop = asyncio.get_running_loop()
        chunk_bytes = max(
            256, int(self._sample_rate * self._channels * 2 * (SENSOR_CHUNK_MS / 1000))
        )
        stream: PASimpleRecordStream | None = None
        loud_since: float | None = None
        quiet_since: float | None = None
        try:
            while True:
                if not stream:
                    try:
                        stream = await loop.run_in_executor(
                            self._pa_executor,
                            lambda: PASimpleRecordStream(
                                source_name=self._pa_source,
                                app_name=f"music-assistant-{self.instance_id}-sensor",
                                rate=self._sample_rate,
                                channels=self._channels,
                            ),
                        )
                    except OSError as err:
                        self.logger.warning(
                            "Signal sensor couldn't open %r, retrying in %.0fs: %s",
                            self._pa_source,
                            SENSOR_RETRY_S,
                            err,
                        )
                        await asyncio.sleep(SENSOR_RETRY_S)
                        continue

                try:
                    chunk = await loop.run_in_executor(self._pa_executor, stream.read, chunk_bytes)
                except OSError as err:
                    self.logger.warning("Signal sensor read failed, reopening: %s", err)
                    with suppress(Exception):
                        await loop.run_in_executor(self._pa_executor, stream.close)
                    stream = None
                    await asyncio.sleep(1)
                    continue

                now = loop.time()
                # A pending auto-trigger that never got confirmed by
                # on_source_selected() (target player unreachable, playback
                # failed silently, etc) would otherwise wedge the sensor
                # forever behind the "not self._auto_triggered_queue" guard
                # below — clear it after a timeout so a retry becomes
                # possible again.
                if (
                    self._auto_triggered_queue
                    and not self._in_use_by_queue
                    and now - self._auto_trigger_pending_since > TRIGGER_PENDING_TIMEOUT_S
                ):
                    self.logger.warning(
                        "Auto-triggered playback on %s never started within %.0fs; "
                        "clearing the claim so signal detection can retry",
                        self._auto_triggered_queue,
                        TRIGGER_PENDING_TIMEOUT_S,
                    )
                    self._auto_triggered_queue = None

                if _pcm_rms_dbfs(chunk) > self._trigger_threshold_dbfs:
                    quiet_since = None
                    loud_since = loud_since or now
                    # Also require no pending auto-trigger claim, not just no
                    # active queue: _trigger_playback() sets
                    # _auto_triggered_queue synchronously before play_media()
                    # is even scheduled, but on_source_selected() (which sets
                    # _in_use_by_queue) only fires once that stream actually
                    # connects — moments later, asynchronously. Without this,
                    # every sensor iteration in that window would see
                    # "not _in_use_by_queue" as still true and fire another
                    # play_media() for the same rising edge.
                    if (
                        not self._in_use_by_queue
                        and not self._auto_triggered_queue
                        and now - loud_since >= TRIGGER_ATTACK_S
                    ):
                        self._trigger_playback()
                else:
                    loud_since = None
                    quiet_since = quiet_since or now
                    if self._auto_triggered_queue and now - quiet_since >= TRIGGER_RELEASE_S:
                        self._auto_stop_playback()
        finally:
            if stream:
                with suppress(Exception):
                    await loop.run_in_executor(self._pa_executor, stream.close)

    def _player_display_name(self, player_id: str) -> str:
        """Resolve a player_id to its display name for logging, falling back to the raw id."""
        if player := self.mass.players.get_player(player_id):
            # str(...): player.display_name types as Any due to a
            # @cached_property/@final interaction in core models/player.py;
            # coerce explicitly rather than carrying that into our own typing.
            return str(player.display_name)
        return player_id

    def _get_target_player_id(self) -> str | None:
        """
        Determine the target player ID for auto-triggered playback.

        - PLAYER_ID_AUTO: prefer a player that's currently playing something
          else, else fall back to the first available player.
        - A specific configured player_id: used as-is if it still exists.

        :return: The player ID to use, or None if no player is available.
        """
        if self._target_player_id != PLAYER_ID_AUTO:
            if self.mass.players.get_player(self._target_player_id):
                return self._target_player_id
            self.logger.warning(
                "Configured auto-start target player '%s' no longer exists",
                self._target_player_id,
            )
            return None

        all_players = list(self.mass.players.all_players(False, False))
        for player in all_players:
            if player.state.playback_state == PlaybackState.PLAYING:
                self.logger.debug("Auto-selecting playing player: %s", player.display_name)
                return player.player_id
        if all_players:
            first_player = all_players[0]
            self.logger.debug(
                "Auto-selecting first available player: %s", first_player.display_name
            )
            return first_player.player_id
        return None

    def _trigger_playback(self) -> None:
        """Auto-start playback of this AudioSource on the resolved target player."""
        target = self._get_target_player_id()
        if not target:
            self.logger.warning(
                "Signal detected on %s but no target player is available; not starting playback.",
                self._friendly_name,
            )
            return
        self.logger.info(
            "Signal detected on %s, starting playback on %s",
            self._friendly_name,
            self._player_display_name(target),
        )
        # Set before play_media() runs so the on_source_selected callback it
        # triggers (asynchronously, once the stream actually connects) sees
        # this queue_id already recorded as ours — see the comment in
        # on_source_selected. Also start the pending-claim clock here (used
        # by the sensor loop's stale-claim timeout) since this is the moment
        # the claim was made, not whenever the task happens to run.
        self._auto_triggered_queue = target
        self._auto_trigger_pending_since = asyncio.get_running_loop().time()
        task = self.mass.create_task(
            self.mass.player_queues.play_media(target, str(self._audio_source.uri))
        )

        def _on_play_media_done(t: asyncio.Task[None]) -> None:
            """Log a failed auto-start and free the claim so the sensor can retry."""
            if t.cancelled():
                return
            if (exc := t.exception()) is not None:
                self.logger.error("Auto-start playback on %s failed: %s", target, exc)
                if self._auto_triggered_queue == target:
                    self._auto_triggered_queue = None

        task.add_done_callback(_on_play_media_done)

    def _auto_stop_playback(self) -> None:
        """Auto-stop playback we previously started ourselves, now that it's gone quiet."""
        queue_id = self._auto_triggered_queue
        self._auto_triggered_queue = None
        if not queue_id:
            return
        # Only actually stop it if it's still playing THIS source: if the
        # user has since picked something else on that queue, our claim was
        # already stale (cleared above) and issuing stop() would yank away
        # playback they started themselves.
        queue = self.mass.player_queues.get(queue_id)
        still_ours = bool(
            queue and queue.current_item and queue.current_item.uri == str(self._audio_source.uri)
        )
        if not still_ours:
            self.logger.debug(
                "Skipping auto-stop on %s: no longer playing %s", queue_id, self._friendly_name
            )
            return
        self.logger.info(
            "Signal on %s went quiet, stopping playback on %s",
            self._friendly_name,
            self._player_display_name(queue_id),
        )
        task = self.mass.create_task(self.mass.player_queues.stop(queue_id))

        def _on_stop_done(t: asyncio.Task[None]) -> None:
            """Log a failed auto-stop; nothing else to recover here."""
            if t.cancelled():
                return
            if (exc := t.exception()) is not None:
                self.logger.error("Auto-stop playback on %s failed: %s", queue_id, exc)

        task.add_done_callback(_on_stop_done)

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
                self._pa_executor,
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
                await loop.run_in_executor(self._pa_executor, stream.close)
