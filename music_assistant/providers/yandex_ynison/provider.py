"""Yandex Ynison plugin provider for Music Assistant."""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import AsyncGenerator, Callable
from contextlib import suppress
from enum import StrEnum
from typing import TYPE_CHECKING, Any, cast

from music_assistant_models.enums import (
    ContentType,
    EventType,
    MediaType,
    PlaybackState,
    ProviderFeature,
    ProviderType,
    QueueOption,
    StreamType,
)
from music_assistant_models.errors import (
    LoginFailed,
    PlayerCommandFailed,
    UnsupportedFeaturedException,
)
from music_assistant_models.streamdetails import StreamDetails, StreamMetadata
from ya_passport_auth import SecretStr

from music_assistant.helpers.ffmpeg import get_ffmpeg_stream
from music_assistant.helpers.throttle_retry import ThrottlerManager
from music_assistant.models.plugin import PluginProvider, PluginSource

from .auth import refresh_music_token
from .constants import (
    CONF_ALLOW_PLAYER_SWITCH,
    CONF_DEVICE_ID,
    CONF_HANDOFF_HEARTBEAT_INTERVAL,
    CONF_MASS_PLAYER_ID,
    CONF_OUTPUT_BIT_DEPTH,
    CONF_OUTPUT_SAMPLE_RATE,
    CONF_PLAYBACK_MODE,
    CONF_PUBLISH_NAME,
    CONF_TOKEN,
    CONF_X_TOKEN,
    CONF_YM_INSTANCE,
    DEFAULT_DISPLAY_NAME,
    HANDOFF_HEARTBEAT_DEFAULT,
    HANDOFF_HEARTBEAT_MAX,
    HANDOFF_HEARTBEAT_MIN,
    OUTPUT_AUTO,
    PLAYBACK_MODE_HANDOFF,
    PLAYBACK_MODE_STREAM,
    PLAYER_ID_AUTO,
    YANDEX_MUSIC_CONF_QUALITY,
    YANDEX_MUSIC_CONF_TOKEN,
    YANDEX_MUSIC_CONF_X_TOKEN,
    YANDEX_MUSIC_LOSSLESS_QUALITIES,
    YM_INSTANCE_OWN,
)
from .protocols import YandexMusicProviderLike
from .streaming import (
    PCM_LOSSLESS_PARAMS,
    PCM_LOSSY_PARAMS,
    PROBE_ARGS,
    make_pcm_format,
    pacing_args,
)
from .ynison_client import (
    YnisonClient,
    YnisonDeviceInfo,
    YnisonState,
    generate_device_id,
    make_version_block,
)

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.event import MassEvent
    from music_assistant_models.media_items import AudioFormat
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant

# How often (seconds) to sync progress to MA UI and Ynison.
# Lowered from 5.0s — the Yandex.Music app shows playback position from
# the active device, so faster updates make play/pause/seek feel snappier
# in the app. Ynison does not rate-limit `update_playing_status`.
_PROGRESS_SYNC_INTERVAL = 2.0

# Retry settings for transient Yandex API failures
_API_MAX_RETRIES = 3
_API_INITIAL_BACKOFF = 2.0
_API_MAX_BACKOFF = 30.0

# Cache TTL for stream details (seconds)
_STREAM_DETAILS_CACHE_TTL = 300  # 5 minutes

# Pre-fetch budget for the format adaptation hint. _get_stream_details_with_retry
# does up to 3 attempts with exponential backoff (2s + 4s = ~6s in the worst
# case) — that latency is unacceptable inline before select_source(). If the
# fetch can't satisfy the budget, fall back to the previously known format
# rather than blocking playback activation.
_PREFETCH_FORMAT_TIMEOUT = 2.5

# Window during which we suppress seek-detection after a track change, seek,
# or play_media(REPLACE) — Ynison echoes our update back, and the stale
# progress in those echoes would otherwise look like a fresh user seek.
# 3s comfortably covers the WS round-trip + MA stream startup; longer windows
# delay legitimate user seeks issued shortly after a track change.
_ECHO_GRACE_PERIOD = 3.0

# Drift-seek suppression: how long after we issue play_media or a seek
# command we ignore drift between Ynison-reported progress and MA queue
# elapsed time. Doubles as the "activation window" used by heartbeat /
# _on_ma_player_event to force `paused=False` while MA is still spinning
# the stream up (otherwise the IDLE-during-startup gap would propagate
# `paused=True` to Ynison and the app would flip to "paused" prematurely).
# 10s comfortably covers play_media await + post-await Chromecast/DLNA/
# web startup latency end-to-end.
_DRIFT_SUPPRESS_PERIOD = 10.0

# Re-issue debounce: how long after a play_media (initial or IDLE-resume)
# we refuse to fire another play_media(REPLACE). 8s comfortably covers
# real Chromecast/DLNA/web-player startup latency (typically 4-6s) so a
# stream of `paused=False` echoes from Ynison while MA is still spinning
# the stream up doesn't trigger duplicate REPLACEs that race the first.
# Three seconds turned out to be too short — heartbeat at T+3.5s reported
# `paused=True` (queue still IDLE), Ynison-app showed pause, user re-tapped
# play, second REPLACE fired, which raced the first. See PR #48 live test.
_REISSUE_DEBOUNCE_PERIOD = 8.0

# Idempotency cache TTL for outbound peer-commands (1 s). A second
# `_apply_*` call carrying the same `(action, track_id)` key inside this
# window is silently dropped — protects against duplicate MA events
# arriving in rapid succession.
_COMMAND_IDEMPOTENCY_TTL = 1.0

# play_media exception backoff sequence. After failures we wait the
# corresponding number of seconds before retrying; we stop after the
# sequence is exhausted (3 attempts).
_PLAY_MEDIA_BACKOFF_SECONDS: tuple[float, ...] = (1.0, 2.0, 5.0)


class HandoffPhase(StrEnum):
    """Internal handoff state machine.

    Mirrors the lifecycle of a single track from the plugin's point of view,
    independent of MA's `PlaybackState`. Decisions in `_dispatch_state` use
    the pair `(actual_queue_state, _expected_phase)` to pick the right action.

    Transitions (ours, driven only by `_apply_*` methods):
      IDLE → ACTIVATING : play_media issued for a new track
      ACTIVATING → PLAYING : MA queue reports PLAYING for our track
      PLAYING → PAUSED : user pause
      PAUSED → PLAYING : user resume
      PLAYING → ENDING : elapsed crossed `duration - 2 s`
      ENDING → IDLE : completion signalled to Ynison and acknowledged
      any → IDLE : `_clear_active_player()`
    """

    IDLE = "idle"
    ACTIVATING = "activating"
    PLAYING = "playing"
    PAUSED = "paused"
    ENDING = "ending"


# Accepted non-auto values for output format overrides; mirrors the options
# offered in CONF_OUTPUT_SAMPLE_RATE / CONF_OUTPUT_BIT_DEPTH config entries.
# Used defensively to reject stale/tampered values without raising.
_VALID_SAMPLE_RATES: frozenset[str] = frozenset({"44100", "48000", "96000"})
_VALID_BIT_DEPTHS: frozenset[str] = frozenset({"16", "24"})


def _parse_handoff_heartbeat(raw: Any) -> float:
    """Coerce config value (str/int/None) to a clamped heartbeat interval (s).

    Accepts string options ("3", "5", "7", "10") from the dropdown as well as
    legacy int/float values from older configs. Falls back to the default on
    any unparsable input — the heartbeat is a safety net, not a critical
    setting, and we don't want it crashing provider load.
    """
    if raw is None:
        return HANDOFF_HEARTBEAT_DEFAULT
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return HANDOFF_HEARTBEAT_DEFAULT
    return max(HANDOFF_HEARTBEAT_MIN, min(HANDOFF_HEARTBEAT_MAX, value))


class YandexYnisonProvider(PluginProvider):
    """Implementation of the Yandex Music Connect (Ynison) Plugin."""

    @property
    def instance_name_postfix(self) -> str | None:
        """Return display name as instance postfix for multi-instance setups."""
        name = self._display_name
        return name if name != DEFAULT_DISPLAY_NAME else None

    @property
    def _is_handoff(self) -> bool:
        """True when this instance is configured for handoff playback mode."""
        return self._playback_mode == PLAYBACK_MODE_HANDOFF

    def __init__(
        self,
        mass: MusicAssistant,
        manifest: ProviderManifest,
        config: ProviderConfig,
        supported_features: set[ProviderFeature],
    ) -> None:
        """Initialize the Ynison plugin provider."""
        super().__init__(mass, manifest, config, supported_features)

        # Config values
        self._default_player_id: str = (
            cast("str", self.config.get_value(CONF_MASS_PLAYER_ID)) or PLAYER_ID_AUTO
        )
        allow_switch_value = self.config.get_value(CONF_ALLOW_PLAYER_SWITCH)
        self._allow_player_switch: bool = (
            cast("bool", allow_switch_value) if allow_switch_value is not None else True
        )
        self._cfg_sample_rate: str = (
            cast("str", self.config.get_value(CONF_OUTPUT_SAMPLE_RATE)) or OUTPUT_AUTO
        )
        self._cfg_bit_depth: str = (
            cast("str", self.config.get_value(CONF_OUTPUT_BIT_DEPTH)) or OUTPUT_AUTO
        )
        self._display_name: str = (
            cast("str", self.config.get_value(CONF_PUBLISH_NAME)) or DEFAULT_DISPLAY_NAME
        )
        self._playback_mode: str = (
            cast("str", self.config.get_value(CONF_PLAYBACK_MODE)) or PLAYBACK_MODE_STREAM
        )
        self._handoff_heartbeat_interval: float = _parse_handoff_heartbeat(
            self.config.get_value(CONF_HANDOFF_HEARTBEAT_INTERVAL)
        )

        # Token source — None = own (manually entered CONF_TOKEN);
        # otherwise the instance_id of a linked yandex_music provider to borrow from.
        ym_instance_value = cast("str | None", self.config.get_value(CONF_YM_INSTANCE))
        self._ym_instance_id: str | None = (
            ym_instance_value
            if ym_instance_value and ym_instance_value != YM_INSTANCE_OWN
            else None
        )

        # Device ID — persist in config so re-registration uses the same ID
        device_id = cast("str | None", self.config.get_value(CONF_DEVICE_ID))
        if not device_id:
            device_id = generate_device_id()
            self._update_config_value(CONF_DEVICE_ID, device_id)
        self._device_id: str = device_id

        # Runtime state
        self._active_player_id: str | None = None
        self._ynison: YnisonClient | None = None
        self._runner_task: asyncio.Task[None] | None = None
        self._on_unload_callbacks: list[Callable[..., None]] = []
        self._yandex_provider: YandexMusicProviderLike | None = None
        self._current_streaming_track_id: str | None = None
        self._track_changed_event = asyncio.Event()
        self._stream_stop_event = asyncio.Event()
        self._seek_position_ms: int = 0
        self._seek_grace_until: float = 0.0
        self._last_player_update_time: float = 0.0
        self._actual_duration_ms: int = 0
        self._prefetched_list: list[dict[str, Any]] | None = None
        self._prefetch_task: asyncio.Task[Any] | None = None
        self._normalized_params: dict[str, Any] = PCM_LOSSY_PARAMS
        self._normalized_format: AudioFormat = make_pcm_format(PCM_LOSSY_PARAMS)

        # Handoff bookkeeping (v2.0 FSM):
        # `_expected_phase` is the plugin's authoritative view of where the
        # current track stands. Driven only by `_apply_*` methods, never by
        # incoming MA queue states. `_dispatch_state` matches MA's actual
        # queue.state against `_expected_phase` to pick the correct action.
        self._expected_phase: HandoffPhase = HandoffPhase.IDLE
        self._expected_track_id: str | None = None
        # Throttle watermark for outbound progress (heartbeat + MA event).
        self._handoff_last_progress_sync_mono: float = 0.0
        # Track id we've already signalled "completion" for — gates against
        # double end-of-track reports inside the same track.
        self._handoff_completion_signaled_for: str | None = None
        # Drift-seek suppression window — set after play_media/seek to
        # ignore stale Ynison echoes while MA spins up the stream.
        self._drift_suppress_until: float = 0.0
        # Re-issue debounce — set after IDLE-resume play_media to refuse
        # another play_media until MA has had time to actually start.
        self._re_issue_debounce_until: float = 0.0
        # Last MA queue.state we observed; used to detect transitions for
        # force-progress (state changes bypass the 2s throttle).
        self._handoff_last_seen_state: PlaybackState | None = None
        # Last position MA queue reported while PLAYING — preferred over
        # Ynison's progress_ms echo on IDLE-resume after a fast toggle.
        self._handoff_last_playing_elapsed_ms: int = 0
        # In-flight play_media task; cancelled when a new track arrives
        # before the previous activation has settled.
        self._play_media_task: asyncio.Task[None] | None = None
        # Idempotency cache: (action, track_id) → expiry monotonic time.
        self._command_idempotency: dict[tuple[str, str | None], float] = {}
        # Independent heartbeat task — guards against Ynison re-balancing the
        # active device when MA queue events are sparse (DLNA/UPnP).
        self._handoff_heartbeat_task: asyncio.Task[None] | None = None

        # Rate limiter for Yandex API calls (max 2 req/s)
        self._api_throttler = ThrottlerManager(rate_limit=2, period=1.0)

        # Progress tracking — byte counter is the single source of truth
        # during active streaming; Ynison echoes are detected via
        # YnisonState.last_update_is_echo and ignored.
        self._streaming_progress_ms: int = 0

        # PluginSource
        self._source_details = PluginSource(
            id=self.instance_id,
            name=self.name,
            passive=not self._allow_player_switch,
            can_play_pause=False,
            can_seek=False,
            can_next_previous=False,
            audio_format=self._normalized_format,
            metadata=StreamMetadata(
                title=f"Yandex Music Connect | {self._display_name}",
            ),
            stream_type=StreamType.CUSTOM,
        )
        self._source_details.on_select = self._on_source_selected

    # ------------------------------------------------------------------
    # Provider lifecycle
    # ------------------------------------------------------------------

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        if self._ym_instance_id is not None:
            self.logger.info(
                "Borrowing credentials from yandex_music instance '%s'",
                self._ym_instance_id,
            )
        else:
            self.logger.info("Using manually configured Yandex Music token (no auto-refresh)")
        token = await self._resolve_token()

        device_info = YnisonDeviceInfo(
            device_id=self._device_id,
            title=self._display_name,
        )

        self._ynison = YnisonClient(
            token=token,
            device_info=device_info,
            on_state_update=self._handle_ynison_state,
            logger=self.logger,
            on_auth_failure=self._refresh_ynison_token,
        )

        self._runner_task = self.mass.create_task(self._ynison.connect())

        # Subscribe to provider events to detect linked yandex_music provider
        self._on_unload_callbacks.append(
            self.mass.subscribe(
                self._on_provider_event,
                EventType.PROVIDERS_UPDATED,
            )
        )
        # Initial check for matching provider
        self.mass.create_task(self._check_yandex_provider_match())

        # Handoff mode: subscribe to MA events so we can mirror progress and
        # playback state back to Ynison without owning the audio source.
        if self._is_handoff:
            self._on_unload_callbacks.append(
                self.mass.subscribe(
                    self._on_ma_player_event,
                    (EventType.QUEUE_TIME_UPDATED, EventType.PLAYER_UPDATED),
                )
            )
            # NOTE: MEDIA_ITEM_PLAYED was tried as a primary natural-end
            # signal but turned out unreliable: MA fires it whenever its
            # internal "fully played" heuristic decides a track is done
            # (>= 90% playback time), and that heuristic confuses the
            # seek-on-activation flow into firing within ~30s of starting
            # a 4-minute track. Stick with the queue state-transition
            # fallback in `_on_ma_player_event` (PLAYING → PAUSED/IDLE +
            # `_is_at_natural_end_of_track`) which uses our own snapshot
            # of the last-known PLAYING elapsed time.
            self.logger.info(
                "Playback mode: HANDOFF — MA player_queue will own audio "
                "(experimental, expect looser app sync)"
            )
        else:
            self.logger.info("Playback mode: STREAM (default)")

    async def unload(self, is_removed: bool = False) -> None:
        """Handle close/cleanup of the provider."""
        if self._prefetch_task and not self._prefetch_task.done():
            self._prefetch_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._prefetch_task

        # Cancel any in-flight handoff play_media task — otherwise it
        # keeps running against a partially-torn-down `mass` context
        # (e.g. config-driven reload or account switch).
        if self._play_media_task and not self._play_media_task.done():
            self._play_media_task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await self._play_media_task

        # Cancel handoff heartbeat task if still alive.
        heartbeat_task = self._handoff_heartbeat_task
        self._handoff_heartbeat_task = None
        if heartbeat_task is not None and not heartbeat_task.done():
            heartbeat_task.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat_task

        if self._ynison:
            await self._ynison.disconnect()

        if self._runner_task and not self._runner_task.done():
            self._runner_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._runner_task

        for callback in self._on_unload_callbacks:
            with suppress(KeyError):
                callback()

    def get_source(self) -> PluginSource:
        """Get (audio)source details for this plugin."""
        return self._source_details

    async def get_audio_stream(self, player_id: str) -> AsyncGenerator[bytes, None]:  # noqa: PLR0915
        """Return continuous audio stream following Ynison track changes.

        Streams the current track, then waits for track changes and streams
        the next track automatically. Runs until the source is deselected.

        The PCM format is frozen at session start to match what the outer
        ffmpeg captured from ``_source_details.audio_format``.  If
        ``_update_normalized_format()`` fires mid-session (e.g. a provider
        reload), the new format takes effect only on the *next* session —
        preventing bit-depth/sample-rate mismatches that cause noise.
        """
        self._stream_stop_event.clear()

        # Freeze format for this streaming session so every inner ffmpeg
        # produces data matching the outer ffmpeg's captured input_format.
        session_params: dict[str, Any] = dict(self._normalized_params)
        session_fmt: AudioFormat = make_pcm_format(session_params)

        while not self._stream_stop_event.is_set() and self._source_details.in_use_by == player_id:
            if not self._ynison or not self._ynison.state.current_track_id:
                # Wait for a track to appear
                self._track_changed_event.clear()
                try:
                    await asyncio.wait_for(self._track_changed_event.wait(), timeout=30.0)
                except TimeoutError:
                    continue
                continue

            # Clear event before reading state so any subsequent update
            # re-sets the event instead of being silently cleared.
            self._track_changed_event.clear()
            track_id = self._ynison.state.current_track_id
            self._current_streaming_track_id = track_id

            # Don't start streaming if Ynison reports paused — wait for resume.
            # Poll every 1s because a same-track resume won't trigger
            # _track_changed_event (it only fires on track change / seek).
            if self._ynison.state.is_paused:
                pause_deadline = time.monotonic() + 30.0
                while (
                    not self._stream_stop_event.is_set()
                    and self._source_details.in_use_by == player_id
                    and self._ynison
                    and self._ynison.state.current_track_id == track_id
                    and self._ynison.state.is_paused
                    and time.monotonic() < pause_deadline
                ):
                    remaining = pause_deadline - time.monotonic()
                    with suppress(TimeoutError):
                        await asyncio.wait_for(
                            self._track_changed_event.wait(),
                            timeout=min(1.0, remaining),
                        )
                    self._track_changed_event.clear()
                continue

            if not self._yandex_provider:
                self.logger.warning(
                    "No linked Yandex Music provider — cannot stream track %s", track_id
                )
                self._current_streaming_track_id = None
                self._stream_stop_event.set()
                if self._source_details.in_use_by == player_id:
                    self._source_details.in_use_by = None
                    await self.mass.players.cmd_stop(player_id)
                return

            # Stream the current track
            seek_ms = self._seek_position_ms
            self._seek_position_ms = 0
            bytes_yielded = 0
            self._streaming_progress_ms = seek_ms
            last_progress_sync = time.monotonic()

            track_fmt = make_pcm_format(session_params)
            async for chunk in self._stream_track(
                track_id, seek_ms=seek_ms, session_params=session_params
            ):
                yield chunk
                bytes_yielded += len(chunk)
                now_mono = time.monotonic()
                if now_mono - last_progress_sync >= _PROGRESS_SYNC_INTERVAL:
                    last_progress_sync = now_mono
                    await self._sync_progress(seek_ms, bytes_yielded, player_id, session_fmt)
                if (
                    self._track_changed_event.is_set()
                    or self._stream_stop_event.is_set()
                    or self._source_details.in_use_by != player_id
                ):
                    break

            # Align to PCM frame boundary — prevents misalignment in MA's
            # downstream ffmpeg when a track stream is interrupted mid-chunk.
            # We pad with zeros (can't un-yield bytes already sent downstream).
            frame_size = (track_fmt.bit_depth // 8) * track_fmt.channels
            if frame_size > 0:
                excess = bytes_yielded % frame_size
                if excess:
                    yield b"\x00" * (frame_size - excess)

            # Don't clear _current_streaming_track_id yet — keep it set
            # during advance/wait so Ynison echo of the same track doesn't
            # trigger a false track-change detection in _activate_playback.

            if self._stream_stop_event.is_set():
                self._current_streaming_track_id = None
                break

            # Track finished naturally — signal completion to Ynison.
            # Yandex controls the queue; we just wait for the next track.
            if not self._track_changed_event.is_set() and self._ynison:
                self.logger.info("Track %s finished, advancing to next", track_id)
                await self._signal_track_completion()
                if not await self._wait_for_track_change(track_id):
                    self._stream_stop_event.set()
                    self._current_streaming_track_id = None
                    break

            # Clear before next iteration — the new track ID will be set at
            # the top of the loop from the latest Ynison state.
            self._current_streaming_track_id = None

    async def _wait_for_track_change(self, old_track_id: str, timeout: float = 30.0) -> bool:
        """Wait for Ynison to report a different track, ignoring echoes.

        After _signal_track_completion sends update_playing_status, Ynison
        echoes back the same track with updated progress.  Only return True
        once current_track_id actually differs from old_track_id.
        """
        deadline = time.monotonic() + timeout
        while not self._stream_stop_event.is_set():
            # Check state BEFORE clearing the event.  Ynison may have already
            # advanced between _signal_track_completion() returning and this
            # method running; clearing first would drop the set() that went
            # with the state update, leaving us to wait until timeout.
            # Check is race-free: no await between the read and clear() below.
            # None means empty/unreadable queue — treat as "not advanced."
            if self._ynison:
                current = self._ynison.state.current_track_id
                if current is not None and current != old_track_id:
                    return True
            self._track_changed_event.clear()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                await asyncio.wait_for(self._track_changed_event.wait(), timeout=remaining)
            except TimeoutError:
                break
        self.logger.info("No new track from Ynison after completion, stopping stream")
        return False

    async def _stream_track(
        self,
        track_id: str,
        seek_ms: int = 0,
        session_params: dict[str, Any] | None = None,
    ) -> AsyncGenerator[bytes, None]:
        """Stream a single track, normalizing to fixed PCM via per-track ffmpeg.

        Every track is decoded through its own ffmpeg process to produce a
        fixed PCM output (s16le or s24le based on YM quality setting). This
        ensures MA's single ffmpeg process never encounters mid-stream format
        changes (codec, bit depth, sample rate).

        *session_params* — frozen format dict from the enclosing
        ``get_audio_stream()`` session.  Falls back to the current
        ``_normalized_params`` when called outside a session.
        """
        try:
            stream_details = await self._get_stream_details_with_retry(track_id)
        except Exception:
            self.logger.exception("Failed to get stream details for track %s", track_id)
            self._stream_stop_event.set()
            return

        # Re-capture the provider after the above await: _yandex_provider may
        # have flipped to None while we were fetching stream details.  Using
        # the attribute directly below would race with
        # _check_yandex_provider_match.
        provider = self._yandex_provider
        if provider is None:
            self.logger.warning(
                "Linked Yandex Music provider unloaded mid-stream — stopping track %s",
                track_id,
            )
            self._stream_stop_event.set()
            return

        await self._update_metadata_from_stream(stream_details, seek_ms)

        extra_input_args = PROBE_ARGS + pacing_args()
        if seek_ms > 0:
            extra_input_args += ["-ss", f"{seek_ms / 1000.0:.3f}"]

        # Use session format when available, otherwise current normalized params
        params = session_params if session_params is not None else self._normalized_params
        out_fmt = make_pcm_format(params)
        self.logger.info(
            "Streaming track %s → %s: input=%s seek=%dms",
            track_id,
            out_fmt.content_type.value,
            stream_details.audio_format,
            seek_ms,
        )
        async for chunk in get_ffmpeg_stream(
            audio_input=provider.get_audio_stream(stream_details),
            input_format=stream_details.audio_format,
            output_format=out_fmt,
            extra_input_args=extra_input_args,
        ):
            yield chunk

    async def _get_stream_details_with_retry(
        self,
        track_id: str,
        media_type: MediaType = MediaType.TRACK,
    ) -> StreamDetails:
        """Fetch stream details with caching, throttling, and retry."""
        # Capture the linked yandex_music provider into a local ref at entry.
        # self._yandex_provider can flip to None mid-await when the linked
        # MusicProvider is unloaded (see _check_yandex_provider_match, which
        # runs as a background task on provider-loaded/unloaded events).
        # Dereferencing the attribute after an await would raise
        # AttributeError and hard-stop the audio generator.
        provider = self._yandex_provider
        if provider is None:
            raise LoginFailed(
                "Linked Yandex Music provider is not loaded — cannot fetch stream details"
            )

        cache_key = f"ynison_sd_{track_id}"
        cached = await self.mass.cache.get(
            cache_key,
            provider=self.instance_id,
            base_class=StreamDetails,
        )
        if cached is not None:
            self.logger.debug("Stream details cache hit for %s", track_id)
            return cast("StreamDetails", cached)

        backoff = _API_INITIAL_BACKOFF
        last_err: Exception | None = None
        for attempt in range(_API_MAX_RETRIES):
            async with self._api_throttler.acquire() as delay:
                if delay > 0:
                    self.logger.debug("get_stream_details throttled %.1fs", delay)
            try:
                sd = await provider.get_stream_details(track_id, media_type)
                # StreamDetails.data has serialize="omit", so to_dict()
                # strips it. Manually include it so cached entries keep
                # the URL / decryption key needed by get_audio_stream().
                cache_value = sd.to_dict()
                cache_value["data"] = sd.data
                # Respect the provider's expiration (e.g. yandex_music sets
                # 50 s because CDN URLs expire after ~60 s).  Fall back to
                # our default TTL when the provider does not override.
                cache_ttl = min(_STREAM_DETAILS_CACHE_TTL, sd.expiration)
                if cache_ttl > 0:
                    await self.mass.cache.set(
                        cache_key,
                        cache_value,
                        expiration=cache_ttl,
                        provider=self.instance_id,
                    )
                return sd
            except asyncio.CancelledError:
                raise
            except Exception as err:
                last_err = err
                if attempt < _API_MAX_RETRIES - 1:
                    jitter = backoff * random.uniform(0.75, 1.25)
                    self.logger.warning(
                        "get_stream_details attempt %d/%d failed: %s, retrying in %.1fs",
                        attempt + 1,
                        _API_MAX_RETRIES,
                        err,
                        jitter,
                    )
                    await asyncio.sleep(jitter)
                    backoff = min(backoff * 2, _API_MAX_BACKOFF)
        msg = f"get_stream_details failed after {_API_MAX_RETRIES} attempts for {track_id}"
        raise RuntimeError(msg) from last_err

    async def _invalidate_stream_cache(self, track_id: str) -> None:
        """Evict cached stream details for a track so the next fetch is fresh."""
        cache_key = f"ynison_sd_{track_id}"
        await self.mass.cache.delete(cache_key, provider=self.instance_id)
        self.logger.debug("Invalidated stream cache for %s", track_id)

    # ------------------------------------------------------------------
    # Token handling
    # ------------------------------------------------------------------

    def _read_ym_tokens(self) -> tuple[str | None, str | None]:
        """Read token/x_token from the linked yandex_music provider's config.

        Borrow-mode only — callers must check ``self._ym_instance_id is not None``
        before calling. Raises LoginFailed with a distinct message when the
        linked YM provider is not currently loaded — separate from the
        "loaded but unauthenticated" case so operators can tell the two apart.
        """
        assert self._ym_instance_id is not None, "Caller must check borrow mode before calling"
        ym_provider = self.mass.get_provider(self._ym_instance_id)
        if ym_provider is None:
            raise LoginFailed(
                f"Linked Yandex Music instance '{self._ym_instance_id}' is not loaded. "
                "Check that the Yandex Music provider is enabled and configured."
            )
        # Guard against a stale/manually-edited instance id pointing at a non-YM
        # provider — otherwise reading unrelated config keys yields a misleading
        # "no credentials" error further down.
        if ym_provider.domain != "yandex_music" or ym_provider.type != ProviderType.MUSIC:
            raise LoginFailed(
                f"Linked provider instance '{self._ym_instance_id}' is not a Yandex Music "
                f"music provider (domain={ym_provider.domain!r}, type={ym_provider.type!r}). "
                "Re-select the Yandex Music source in this plugin's configuration."
            )
        token = cast("str | None", ym_provider.config.get_value(YANDEX_MUSIC_CONF_TOKEN))
        x_token = cast("str | None", ym_provider.config.get_value(YANDEX_MUSIC_CONF_X_TOKEN))
        return (token, x_token)

    async def _resolve_token(self) -> SecretStr:
        """Resolve the Yandex Music OAuth token for the Ynison connection.

        In borrow mode: read from the linked yandex_music provider's config.
        If only x_token is present (YM hasn't refreshed yet), do a one-shot
        in-memory refresh without writing back — YM owns token persistence.

        In own mode: return CONF_TOKEN if set; otherwise, when CONF_X_TOKEN
        is present (QR-with-Remember-session path), refresh in-memory.
        """
        if self._ym_instance_id is not None:
            token, x_token = self._read_ym_tokens()
            if token:
                return SecretStr(token)
            if x_token:
                self.logger.debug("YM token not yet refreshed — refreshing in-memory")
                return await refresh_music_token(SecretStr(x_token))
            raise LoginFailed(f"Yandex Music instance '{self._ym_instance_id}' has no credentials")

        token = cast("str | None", self.config.get_value(CONF_TOKEN))
        if token:
            return SecretStr(token)
        x_token = cast("str | None", self.config.get_value(CONF_X_TOKEN))
        if x_token:
            self.logger.debug("Own-mode token not present — refreshing from stored x_token")
            return await refresh_music_token(SecretStr(x_token))
        raise LoginFailed("No Yandex Music token configured")

    async def _refresh_ynison_token(self) -> SecretStr:
        """Refresh the OAuth token for Ynison reconnection.

        Called by YnisonClient on auth failure (401/403) during reconnect.

        In borrow mode: re-read the linked YM instance's x_token and refresh
        in-memory only (no config writes — YM owns token persistence).

        In own mode: refresh from stored CONF_X_TOKEN when present (QR with
        "Remember session" enabled). When absent (manual token paste only),
        surface LoginFailed so the user knows to paste a new token.
        """
        if self._ym_instance_id is not None:
            _, x_token = self._read_ym_tokens()
            if not x_token:
                raise LoginFailed("Cannot refresh: linked Yandex Music instance has no x_token")
            self.logger.info("Refreshing Yandex Music token for Ynison reconnect (borrow mode)")
            return await refresh_music_token(SecretStr(x_token))

        x_token = cast("str | None", self.config.get_value(CONF_X_TOKEN))
        if x_token:
            self.logger.info("Refreshing Yandex Music token for Ynison reconnect (own mode)")
            return await refresh_music_token(SecretStr(x_token))

        raise LoginFailed(
            "Token expired and no stored x_token to refresh from. Re-authenticate "
            "via QR or paste a fresh Yandex Music token."
        )

    # ------------------------------------------------------------------
    # Ynison state handling
    # ------------------------------------------------------------------

    async def _handle_ynison_state(self, state: YnisonState) -> None:
        """Handle state update from Ynison."""
        is_our_device = state.active_device_id == self._device_id

        # Detailed queue logging for diagnostics
        queue = state.player_state.get("player_queue", {})
        playable_list = queue.get("playable_list", [])
        current_index = queue.get("current_playable_index", -1)
        entity_type = queue.get("entity_type", "")
        entity_id = queue.get("entity_id", "")
        track_id = state.current_track_id
        self.logger.debug(
            "Ynison state: active_device=%s (ours=%s) track=%s "
            "index=%d/%d entity=%s type=%s paused=%s progress=%dms",
            state.active_device_id,
            is_our_device,
            track_id,
            current_index,
            len(playable_list),
            entity_id[:40] if entity_id else "<none>",
            entity_type,
            state.is_paused,
            state.progress_ms,
        )

        # Post-reconnect settle window (v2.0). The first state after a WS
        # reconnect can be a stale snapshot from before the disconnect —
        # acting on it would re-fire whatever was last broadcast (typically
        # paused=True from our own heartbeat). Skip with a debug log; the
        # next genuine state-update inside ~2s will be processed normally.
        if self._ynison and self._ynison.in_post_reconnect_settle:
            self.logger.debug(
                "Skipping state inside post-reconnect settle window (track=%s paused=%s)",
                track_id,
                state.is_paused,
            )
            return

        if is_our_device and not state.is_paused:
            # Pre-fetch next batch when playing second-to-last track
            self._maybe_prefetch(current_index, playable_list, entity_id, entity_type)
            await self._activate_playback(state)
        elif is_our_device and state.is_paused:
            if self._is_handoff:
                # Pause only the player we already activated. Falling back to
                # _get_target_player_id() here would auto-select an unrelated
                # playing player after startup/cleanup and pause the wrong
                # MA queue (Copilot review).
                if self._active_player_id:
                    await self._handoff_pause(self._active_player_id)
            else:
                # Our device but paused — stop player, keep association
                await self._pause_playback()
        elif self._source_details.in_use_by or (self._is_handoff and self._active_player_id):
            # Active device switched away — fully release player.
            # In handoff mode `_source_details.in_use_by` is always None
            # (no AUDIO_SOURCE), so we also check `_active_player_id` —
            # otherwise the heartbeat keeps pushing stale progress to
            # Ynison long after another device took over.
            self._clear_active_player()

    async def _activate_playback(self, state: YnisonState) -> None:  # noqa: PLR0915
        """Activate playback on the target MA player."""
        target_player_id = self._get_target_player_id()
        if not target_player_id:
            self.logger.warning("Ynison active on our device but no MA player available")
            return

        if self._is_handoff:
            await self._handoff_activate(state, target_player_id)
            return

        # Detect resume after pause: stream was stopped but player still associated
        needs_reselect = self._stream_stop_event.is_set()
        self._stream_stop_event.clear()

        # Select source on the target player if not already active or resuming.
        # Guard on _active_player_id (set immediately) rather than in_use_by
        # (set by the server callback after DLNA negotiation completes) to
        # prevent queuing redundant select_source calls during the ~5s gap.
        if self._active_player_id != target_player_id or needs_reselect:
            # Pre-fetch real format of the upcoming track BEFORE select_source
            # so the outer ffmpeg starts with PluginSource.audio_format that
            # matches what we will actually emit. Skipped only when the same
            # player resumes the same track (format definitely unchanged); a
            # resume-reselect onto a *different* track must still prefetch.
            new_track = state.current_track_id
            should_prefetch = new_track is not None and (
                self._active_player_id != target_player_id
                or new_track != self._current_streaming_track_id
            )
            self._active_player_id = target_player_id
            if should_prefetch and new_track is not None:
                await self._prefetch_format_for_track(new_track)
            self.mass.create_task(
                self.mass.players.select_source(target_player_id, self.instance_id)
            )

        # Signal track change if track_id changed
        significant_change = False
        new_track = state.current_track_id
        if new_track and new_track != self._current_streaming_track_id:
            self.logger.info("Track changed: %s -> %s", self._current_streaming_track_id, new_track)
            self._current_streaming_track_id = new_track
            self._seek_position_ms = state.progress_ms
            self._track_changed_event.set()
            significant_change = True
            # Grace period: ignore seek detection for a few seconds after
            # track change — Ynison echoes can report stale progress that
            # looks like a large drift.
            self._seek_grace_until = time.monotonic() + _ECHO_GRACE_PERIOD
        elif new_track and new_track == self._current_streaming_track_id:
            # Same-track resume after pause: explicitly seek to the Ynison position
            # so the new stream starts at the right offset.
            if needs_reselect:
                self._seek_position_ms = state.progress_ms
                self._track_changed_event.set()
                self._seek_grace_until = time.monotonic() + _ECHO_GRACE_PERIOD
                significant_change = True
            else:
                # Detect seek: compare Ynison progress against our stream position.
                # Ignore Ynison echoes (updates authored by our own device_id) to
                # prevent feedback loops where our own progress triggers false seeks.
                now = time.monotonic()
                if now < self._seek_grace_until:
                    pass  # Skip during grace period after track change or seek
                elif state.last_update_is_echo:
                    pass  # Echo of our own update — ignore
                else:
                    our_ms = self._streaming_progress_ms
                    if our_ms >= 0:
                        verdict = self._classify_drift(state.progress_ms, our_ms)
                        if verdict == "seek":
                            self.logger.info(
                                "Seek detected on track %s: "
                                "expected ~%dms, Ynison at %dms (drift %dms)",
                                new_track,
                                our_ms,
                                state.progress_ms,
                                abs(state.progress_ms - our_ms),
                            )
                            self._seek_position_ms = state.progress_ms
                            self._track_changed_event.set()
                            self._seek_grace_until = now + _ECHO_GRACE_PERIOD
                            significant_change = True
                        elif verdict == "queue_rebuild":
                            self.logger.debug(
                                "Stream: drift to 0 ignored on %s (Ynison=%dms, "
                                "stream=%dms) — queue-rebuild echo, not a user seek",
                                new_track,
                                state.progress_ms,
                                our_ms,
                            )

        # Update metadata from state
        self._update_metadata(state)

        # Always trigger player update on significant changes;
        # throttle regular updates to avoid UI churn (every 2 seconds).
        # Use force_update on seek/track change so the server broadcasts a full
        # PLAYER_UPDATED event instead of a lightweight elapsed-time-only one
        # that the frontend may not handle for PluginSource players.
        now_mono = time.monotonic()
        if significant_change or needs_reselect or now_mono - self._last_player_update_time >= 2.0:
            self.mass.players.trigger_player_update(
                target_player_id, force_update=significant_change
            )
            self._last_player_update_time = now_mono

    def _update_metadata(self, state: YnisonState) -> None:
        """Update PluginSource metadata from Ynison state."""
        if self._source_details.metadata is None:
            self._source_details.metadata = StreamMetadata(
                title=f"Yandex Music Connect | {self._display_name}",
            )

        meta = self._source_details.metadata

        # Update duration (prefer actual from stream_details) and elapsed time
        best_duration = self._best_duration_ms()
        if best_duration:
            meta.duration = best_duration // 1000
        # Only update elapsed from Ynison when NOT actively streaming —
        # during streaming, _sync_progress provides byte-accurate progress.
        if state.progress_ms is not None and not self._source_details.in_use_by:
            meta.elapsed_time = state.progress_ms // 1000
            meta.elapsed_time_last_updated = time.time()

        # Extract track info from player state if available
        queue = state.player_state.get("player_queue", {})
        playable_list = queue.get("playable_list", [])
        index = queue.get("current_playable_index", 0)
        if playable_list and 0 <= index < len(playable_list):
            playable = playable_list[index]
            title = playable.get("title")
            if title:
                meta.title = title
            cover = playable.get("cover_url_optional")
            if cover and not cover.startswith("http"):
                cover = f"https://{cover}"
            if cover:
                # Replace %% placeholder with size
                cover = cover.replace("%%", "400x400")
            meta.image_url = cover

    async def _update_metadata_from_stream(
        self, stream_details: StreamDetails, seek_ms: int = 0
    ) -> None:
        """Update PluginSource metadata from stream details (authoritative for duration)."""
        if self._source_details.metadata is None:
            self._source_details.metadata = StreamMetadata(
                title=f"Yandex Music Connect | {self._display_name}",
            )
        meta = self._source_details.metadata
        if stream_details.duration:
            meta.duration = stream_details.duration
            self._actual_duration_ms = stream_details.duration * 1000
            # Push the real duration to Ynison so the YM app shows
            # the correct value (we send duration_ms=0 on advance to
            # prevent stale propagation, so this corrects it).
            if self._ynison:
                await self._send_progress_to_ynison(
                    progress_ms=seek_ms,
                    duration_ms=self._actual_duration_ms,
                    paused=self._ynison.state.is_paused,
                )
        meta.elapsed_time = seek_ms // 1000 if seek_ms else 0
        meta.elapsed_time_last_updated = time.time()
        if self._source_details.in_use_by:
            self.mass.players.trigger_player_update(
                self._source_details.in_use_by, force_update=True
            )

    async def _send_progress_to_ynison(
        self, progress_ms: int, duration_ms: int, paused: bool
    ) -> None:
        """Send progress to Ynison.

        Progress is clamped to duration because Ynison rejects updates where
        progress > duration (error 400030001) and disconnects the WebSocket.
        The byte counter can slightly overshoot duration at end-of-stream.

        Echo detection is done upstream via YnisonState.last_update_is_echo,
        which is set when Ynison rebroadcasts an update we authored.
        """
        if duration_ms <= 0:
            # Ynison rejects progress > duration; skip until duration is known.
            return
        if not self._ynison or not self._ynison.connected:
            return
        progress_ms = min(progress_ms, duration_ms)
        await self._ynison.update_playing_status(
            progress_ms=progress_ms,
            duration_ms=duration_ms,
            paused=paused,
        )

    def _bytes_to_ms(self, byte_count: int, fmt: AudioFormat | None = None) -> int:
        """Convert PCM byte count to milliseconds using the given format."""
        bps = (fmt or self._normalized_format).pcm_sample_size
        if bps == 0:
            return 0
        return (byte_count * 1000) // bps

    async def _sync_progress(
        self,
        seek_ms: int,
        bytes_yielded: int,
        player_id: str | None,
        fmt: AudioFormat | None = None,
    ) -> None:
        """Push real playback progress to MA metadata and Ynison."""
        elapsed_ms = seek_ms + self._bytes_to_ms(bytes_yielded, fmt)
        self._streaming_progress_ms = elapsed_ms
        # Update MA metadata
        meta = self._source_details.metadata
        if meta:
            meta.elapsed_time = elapsed_ms // 1000
            meta.elapsed_time_last_updated = time.time()
        if player_id:
            self.mass.players.trigger_player_update(player_id)
        # Update Ynison so the Yandex app shows correct position
        await self._send_progress_to_ynison(
            progress_ms=elapsed_ms,
            duration_ms=self._best_duration_ms(),
            paused=False,
        )

    async def _pause_playback(self) -> None:
        """Handle pause — stop streaming but keep player association for resume."""
        paused_progress_ms = self._streaming_progress_ms
        self._stream_stop_event.set()
        # Preserve the last known position for same-track resume.
        self._streaming_progress_ms = paused_progress_ms
        player_id = self._source_details.in_use_by
        if player_id:
            try:
                await self.mass.players.cmd_stop(player_id)
            except Exception:
                self.logger.debug("Failed to stop player %s on pause", player_id)
            if self._source_details.in_use_by == player_id:
                self._source_details.in_use_by = None
                self.mass.players.trigger_player_update(player_id)

    # ------------------------------------------------------------------
    # Player selection
    # ------------------------------------------------------------------

    def _get_target_player_id(self) -> str | None:
        """Determine the target player ID for playback."""
        # If there's an active player, validate it still exists
        if self._active_player_id:
            if self.mass.players.get_player(self._active_player_id):
                return self._active_player_id
            self._active_player_id = None

        # Auto selection
        if self._default_player_id == PLAYER_ID_AUTO:
            all_players = list(self.mass.players.all_players(False, False))
            # Prefer currently playing player
            for player in all_players:
                if player.state.playback_state == PlaybackState.PLAYING:
                    self.logger.debug("Auto-selecting playing player: %s", player.display_name)
                    return str(player.player_id)
            # Fallback to first available
            if all_players:
                return str(all_players[0].player_id)
            return None

        # Specific configured player
        if self.mass.players.get_player(self._default_player_id):
            return self._default_player_id

        self.logger.warning(
            "Configured default player '%s' no longer exists",
            self._default_player_id,
        )
        return None

    async def _on_source_selected(self) -> None:
        """Handle callback when this source is selected on a player."""
        new_player_id = self._source_details.in_use_by
        if not new_player_id:
            return

        # Check if manual player switching is allowed
        if not self._allow_player_switch:
            current_target = self._get_target_player_id()
            if new_player_id != current_target:
                self.logger.debug(
                    "Player switching disabled, rejecting selection on %s",
                    new_player_id,
                )
                self._source_details.in_use_by = current_target
                # Revert the rejected player's active_source back to its MA queue
                # (the controller already set it to our plugin before calling on_select)
                try:
                    await self.mass.players.select_source(new_player_id, new_player_id)
                except Exception:
                    self.logger.debug("Could not revert active_source for %s", new_player_id)
                if current_target:
                    self.mass.players.trigger_player_update(current_target)
                msg = (
                    "Player switching is disabled; source must remain on "
                    f"{current_target or 'the configured target player'}"
                )
                raise RuntimeError(msg)

        # Stop previous player if switching
        if self._active_player_id and self._active_player_id != new_player_id:
            self.logger.info(
                "Source selected on %s, stopping %s",
                new_player_id,
                self._active_player_id,
            )
            try:
                await self.mass.players.cmd_stop(self._active_player_id)
            except Exception as err:
                self.logger.debug(
                    "Failed to stop previous player %s: %s",
                    self._active_player_id,
                    err,
                )

        self._active_player_id = new_player_id
        self.logger.debug("Active player set to: %s", new_player_id)

    def _clear_active_player(self) -> None:
        """Clear the active player and reset plugin state."""
        prev_player_id = self._active_player_id
        was_in_use = self._source_details.in_use_by == prev_player_id
        self._active_player_id = None
        self._source_details.in_use_by = None
        self._stream_stop_event.set()
        self._streaming_progress_ms = 0
        self._prefetched_list = None
        if self._prefetch_task and not self._prefetch_task.done():
            self._prefetch_task.cancel()
        # Stop the handoff heartbeat — there is no active player to report on.
        self._cancel_handoff_heartbeat()
        # Reset handoff bookkeeping so the next activation starts clean.
        # The progress watermark is the throttle key for both
        # _on_ma_player_event and _handoff_heartbeat_loop — leaving a stale
        # value here would suppress the first update of a fresh session
        # (Copilot review).
        self._expected_phase = HandoffPhase.IDLE
        self._expected_track_id = None
        self._handoff_completion_signaled_for = None
        self._drift_suppress_until = 0.0
        self._re_issue_debounce_until = 0.0
        self._handoff_last_seen_state = None
        self._handoff_last_progress_sync_mono = 0.0
        self._handoff_last_playing_elapsed_ms = 0
        self._command_idempotency.clear()

        if prev_player_id:
            self.logger.debug(
                "Playback ended on player %s, clearing active player",
                prev_player_id,
            )
            if was_in_use:
                self.mass.create_task(self.mass.players.cmd_stop(prev_player_id))
            self.mass.players.trigger_player_update(prev_player_id)

    # ------------------------------------------------------------------
    # Yandex Music provider matching
    # ------------------------------------------------------------------

    def _on_provider_event(self, event: MassEvent) -> None:
        """Handle provider added/removed events."""
        self.mass.create_task(self._check_yandex_provider_match())

    async def _check_yandex_provider_match(self) -> None:
        """Check if a Yandex Music provider is available for audio streaming.

        In borrow mode (self._ym_instance_id set), match strictly by instance_id
        so that audio and credentials come from the same account. In own mode,
        accept any yandex_music music-provider (prior behavior).
        """
        for provider in self.mass.get_providers():
            if provider.domain != "yandex_music" or provider.type != ProviderType.MUSIC:
                continue
            if self._ym_instance_id is not None and provider.instance_id != self._ym_instance_id:
                continue
            self.logger.debug("Found Yandex Music provider — enabling playback control")
            self._yandex_provider = cast("YandexMusicProviderLike", provider)
            self._update_normalized_format()
            self._update_source_capabilities()
            return

        if self._yandex_provider is not None:
            self.logger.debug(
                "Yandex Music provider no longer available — disabling playback control"
            )
            self._yandex_provider = None
            self._update_source_capabilities()

    def _update_normalized_format(self, hint: AudioFormat | None = None) -> None:
        """Set PCM normalization profile based on config, YM quality and optional hint.

        Priority: explicit config values > hint from real track stream_details
        > auto-detection from YM quality.

        Auto-detection reads the quality tier from the linked yandex_music
        provider's config (`provider.config.get_value("quality")`), since
        yandex_music does not expose a typed accessor method.
        Auto-detection: superb/lossless → 24bit/44.1kHz, else → 16bit/44.1kHz.

        When *hint* is provided (e.g. real ``stream_details.audio_format`` of
        the upcoming track) and config is at OUTPUT_AUTO, the hint's
        sample_rate/bit_depth override the auto-detected base — letting Hi-Res
        tracks (96 kHz / 24-bit) flow without resampling.

        Creates fresh AudioFormat instances each time to prevent mutation by
        MA's FFMpeg._log_reader_task (which sets input_format.codec_type
        in-place on the object passed as input_format to the outer ffmpeg).
        """
        # Start with auto-detected base from YM quality config
        # (yandex_music does not expose get_quality(); read from its ProviderConfig instead)
        quality = ""
        if self._yandex_provider is not None:
            provider_config = getattr(self._yandex_provider, "config", None)
            if provider_config is not None and hasattr(provider_config, "get_value"):
                config_quality = provider_config.get_value(YANDEX_MUSIC_CONF_QUALITY)
                if isinstance(config_quality, str):
                    quality = config_quality
        is_lossless = quality in YANDEX_MUSIC_LOSSLESS_QUALITIES
        base = PCM_LOSSLESS_PARAMS if is_lossless else PCM_LOSSY_PARAMS

        sample_rate = base["sample_rate"]
        bit_depth = base["bit_depth"]

        # Hint from real stream_details — only honoured in auto mode.
        # Validate against accepted values to reject implausible reports.
        if hint is not None:
            if self._cfg_sample_rate == OUTPUT_AUTO and hint.sample_rate in {44100, 48000, 96000}:
                sample_rate = hint.sample_rate
            if self._cfg_bit_depth == OUTPUT_AUTO and hint.bit_depth in {16, 24}:
                bit_depth = hint.bit_depth

        # Apply config overrides. MA's ConfigEntry options constrain the UI to
        # known-good strings, but a stale persisted value or hand-edited config
        # could still surface something unparsable or off-list — fall back to
        # the auto-detected base with a warning instead of crashing the load.
        if self._cfg_sample_rate != OUTPUT_AUTO:
            if self._cfg_sample_rate in _VALID_SAMPLE_RATES:
                sample_rate = int(self._cfg_sample_rate)
            else:
                self.logger.warning(
                    "Invalid %s=%r; falling back to auto-detected %d Hz",
                    CONF_OUTPUT_SAMPLE_RATE,
                    self._cfg_sample_rate,
                    sample_rate,
                )
        if self._cfg_bit_depth != OUTPUT_AUTO:
            if self._cfg_bit_depth in _VALID_BIT_DEPTHS:
                bit_depth = int(self._cfg_bit_depth)
            else:
                self.logger.warning(
                    "Invalid %s=%r; falling back to auto-detected %d-bit",
                    CONF_OUTPUT_BIT_DEPTH,
                    self._cfg_bit_depth,
                    bit_depth,
                )

        content_type = ContentType.PCM_S24LE if bit_depth == 24 else ContentType.PCM_S16LE
        new_params: dict[str, Any] = {
            "content_type": content_type,
            "sample_rate": sample_rate,
            "bit_depth": bit_depth,
            "channels": 2,
        }

        # Warn if format changes while a player is actively streaming — the
        # active session keeps using its frozen snapshot; the new format takes
        # effect on the next session.
        old = self._normalized_params
        if self._source_details.in_use_by and (
            old.get("content_type") != content_type
            or old.get("sample_rate") != sample_rate
            or old.get("bit_depth") != bit_depth
        ):
            self.logger.warning(
                "Normalization format changed while streaming — new format "
                "(%s/%dHz/%dbit) will apply on next session",
                content_type.value,
                sample_rate,
                bit_depth,
            )

        self._normalized_params = new_params
        # Fresh copy for each caller so no shared mutable state
        self._normalized_format = make_pcm_format(self._normalized_params)
        self._source_details.audio_format = make_pcm_format(self._normalized_params)
        self.logger.debug(
            "Normalization format: %s/%dHz/%dbit",
            self._normalized_format.content_type.value,
            self._normalized_format.sample_rate,
            self._normalized_format.bit_depth,
        )

    async def _prefetch_format_for_track(self, track_id: str) -> None:
        """Pre-fetch stream details for *track_id* and adapt PCM format.

        MA reads ``PluginSource.audio_format`` BEFORE calling our
        ``get_audio_stream()`` (see ``streams/controller.py`` —
        ``get_plugin_source_stream`` captures ``input_format`` from the
        source's ``audio_format`` and starts the outer ffmpeg with it).
        Therefore the format must already match the upcoming track when
        ``select_source()`` fires — adapting later is too late.

        This method is best-effort: any failure leaves the current format
        in place and only logs a warning, so playback never breaks because
        of a bad pre-fetch.

        Bounded by ``_PREFETCH_FORMAT_TIMEOUT`` so a transient API hiccup
        cannot stall ``select_source()`` for the full retry budget — a slow
        pre-fetch is treated like a failed one (fall back to current format,
        let the in-stream `_get_stream_details_with_retry` handle retries).
        """
        if not self._yandex_provider:
            return
        try:
            stream_details = await asyncio.wait_for(
                self._get_stream_details_with_retry(track_id),
                timeout=_PREFETCH_FORMAT_TIMEOUT,
            )
        except TimeoutError:
            self.logger.info(
                "Pre-fetch of stream details for %s exceeded %.1fs — "
                "keeping current format; in-stream fetch will retry",
                track_id,
                _PREFETCH_FORMAT_TIMEOUT,
            )
            return
        except Exception:
            self.logger.warning(
                "Pre-fetch of stream details failed for %s — keeping current format",
                track_id,
                exc_info=True,
            )
            return
        old_sr = self._normalized_params.get("sample_rate")
        old_bd = self._normalized_params.get("bit_depth")
        self._update_normalized_format(hint=stream_details.audio_format)
        new_sr = self._normalized_params.get("sample_rate")
        new_bd = self._normalized_params.get("bit_depth")
        if (old_sr, old_bd) != (new_sr, new_bd):
            self.logger.info(
                "Pre-fetch adapted format for %s: %dHz/%dbit -> %dHz/%dbit (source=%s)",
                track_id,
                old_sr or 0,
                old_bd or 0,
                new_sr or 0,
                new_bd or 0,
                stream_details.audio_format,
            )

    def _update_source_capabilities(self) -> None:
        """Update source capabilities based on linked provider availability."""
        has_provider = self._yandex_provider is not None
        self._source_details.can_play_pause = has_provider
        self._source_details.can_seek = has_provider
        self._source_details.can_next_previous = has_provider

        if has_provider:
            self._source_details.on_play = self._on_play
            self._source_details.on_pause = self._on_pause
            self._source_details.on_next = self._on_next
            self._source_details.on_previous = self._on_previous
            self._source_details.on_seek = self._on_seek
        else:
            self._source_details.on_play = None
            self._source_details.on_pause = None
            self._source_details.on_next = None
            self._source_details.on_previous = None
            self._source_details.on_seek = None

        if self._source_details.in_use_by:
            self.mass.players.trigger_player_update(self._source_details.in_use_by)

    # ------------------------------------------------------------------
    # Handoff mode (experimental)
    # ------------------------------------------------------------------

    def _idempotent(self, action: str, key: str | None) -> bool:
        """Return True if `(action, key)` was not seen within the TTL window.

        Suppresses duplicate command invocations during rapid Ynison echoes —
        e.g. two identical pause notifications inside 1s should issue
        `mass.players.cmd_pause` only once. The cache is keyed by the
        `(action, key)` tuple and entries older than `_COMMAND_IDEMPOTENCY_TTL`
        are evicted lazily.
        """
        now = time.monotonic()
        # Lazy GC: drop stale entries to keep the dict bounded.
        for stale_key in [
            k for k, ts in self._command_idempotency.items() if now - ts > _COMMAND_IDEMPOTENCY_TTL
        ]:
            self._command_idempotency.pop(stale_key, None)
        composite = (action, key)
        last = self._command_idempotency.get(composite)
        if last is not None and now - last < _COMMAND_IDEMPOTENCY_TTL:
            return False
        self._command_idempotency[composite] = now
        return True

    @staticmethod
    def _classify_drift(
        ynison_ms: int,
        our_ms: int,
        threshold_ms: int = 3000,
    ) -> str:
        """Classify drift between Ynison-reported and our local position.

        Returns one of:
        - ``"ignore"`` — drift below threshold; no seek needed.
        - ``"queue_rebuild"`` — Ynison reports near-zero while we're
          past 5s; treat as a RADIO queue-rebuild echo, not a real
          user seek (otherwise we'd yank playback back to start
          mid-track on every queue replenishment).
        - ``"seek"`` — genuine drift; honor it.

        Used by both handoff (`_apply_same_track_sync`) and stream
        mode (`_handle_ynison_state` drift block) to keep the
        rebuild-vs-seek heuristic consistent.
        """
        drift = abs(ynison_ms - our_ms)
        if drift <= threshold_ms:
            return "ignore"
        if ynison_ms < 1000 and our_ms > 5000:
            return "queue_rebuild"
        return "seek"

    @staticmethod
    def _pick_resume_position(
        *,
        local_snapshot_ms: int,
        ynison_progress_ms: int,
    ) -> tuple[int, str]:
        """Pick the best resume position from local snapshot + Ynison.

        Returns ``(resume_ms, source_label)``. Local snapshots can
        be stale after multiple REPLACE cycles (handoff) or after a
        network blip (stream). Ynison's progress is set by the user's
        app on pause and is authoritative for "where the user wants
        to resume". Take the max so:
        - A fast pause/resume toggle (Ynison echo lags by ~1s but
          local snapshot is current) lands at the local value.
        - A long pause where local accumulator was reset by REPLACE
          cycles (snapshot=1s, Ynison=43s) lands at Ynison's 43s.

        Source label is for logging; values: ``"local_snapshot"`` or
        ``"ynison"``.
        """
        if local_snapshot_ms >= ynison_progress_ms and local_snapshot_ms > 0:
            return local_snapshot_ms, "local_snapshot"
        return ynison_progress_ms, "ynison"

    async def _cancel_pending_play_media(self) -> None:
        """Cancel a still-running play_media task, if any.

        On rapid track-change in the Yandex app the previous `play_media` may
        still be resolving stream details when the next one fires. Cancelling
        the predecessor avoids a half-finished load racing with the new one
        and confusing MA's queue runner.
        """
        task = self._play_media_task
        if task is None or task.done():
            return
        task.cancel()
        with suppress(asyncio.CancelledError, Exception):
            await task

    def _build_handoff_uri(self, track_id: str) -> str:
        """Build a Yandex Music URI for handoff `play_media`.

        Use the linked provider's instance_id when known so MA picks the right
        yandex_music account (matters when borrow + own coexist). Fall back to
        the bare domain — MA's `parse_uri` accepts both forms.
        """
        prov_id = (
            self._yandex_provider.instance_id
            if self._yandex_provider is not None
            and getattr(self._yandex_provider, "instance_id", None)
            else "yandex_music"
        )
        return f"{prov_id}://track/{track_id}"

    async def _handoff_activate(self, state: YnisonState, target_player_id: str) -> None:
        """Centralized FSM dispatcher for Ynison handoff state events.

        Classifies the incoming Ynison state diff and routes to the
        appropriate `_apply_*` action method. Three top-level cases:

        1. Track id changed → `_apply_track_change` (REPLACE / dedup /
           cmd_play resume).
        2. Same track, queue IDLE on the same URI → `_apply_idle_resume`
           (re-issue play_media + seek for resume after pause-watchdog).
        3. Same track, queue alive → `_apply_same_track_sync` (drift
           detection + queue-PAUSED → cmd_play mirror).

        The plugin trades native MA pipeline (no inner ffmpeg, no PCM
        resampling) for looser sync — Spotify Connect's authors hit the
        same trade-off (see commented `CONF_HANDOFF_MODE` in
        spotify_connect/__init__.py).
        """
        new_track = state.current_track_id
        if not new_track:
            return

        # Replay reset (P6): fresh state at progress < 1s on the same
        # track means the user reset playback. Clear the completion
        # marker so the next end-of-track signals correctly.
        if state.progress_ms < 1000 and new_track == self._expected_track_id:
            self._handoff_completion_signaled_for = None

        expected_uri = self._build_handoff_uri(new_track)
        is_track_change = new_track != self._expected_track_id

        if is_track_change:
            # Pre-fetch primes MA's stream-detail cache. Cosmetic in
            # handoff (MA fetches its own details), keeps logs/duration
            # consistent during the transition.
            if self._yandex_provider:
                await self._prefetch_format_for_track(new_track)
            await self._apply_track_change(state, target_player_id, new_track, expected_uri)
            return

        queue = self.mass.player_queues.get(target_player_id)

        # Resume-via-REPLACE: a single-track REPLACE queue lands in IDLE
        # *or* PAUSED on pause depending on the underlying player.
        # cmd_play on PAUSED works when the HTTP stream is still live
        # (local FIFO/Snapcast etc.), but local web/Chromecast players
        # close the connection after a few seconds of pause and cmd_play
        # then has nothing to resume from — audio stays silent. Always
        # re-issue play_media on same-URI resume to spin the stream
        # back up reliably. The cmd_pause/seek/cmd_play dance inside
        # `_apply_idle_resume` avoids the audible 0-then-jump glitch.
        if (
            queue is not None
            and queue.state in (PlaybackState.IDLE, PlaybackState.PAUSED)
            and queue.current_item is not None
            and getattr(queue.current_item, "uri", None) == expected_uri
            and not state.is_paused
            # Guard: skip IDLE-resume in two phases:
            # - PAUSED: WE just committed `_handoff_pause`; a stale
            #   Ynison `paused=False` echo would otherwise REPLACE on
            #   a deliberately-paused player.
            # - ENDING: we just signalled track completion to Ynison
            #   (heartbeat or `_on_ma_player_event` natural-end path);
            #   queue is IDLE on the OLD track URI but Ynison is in
            #   the middle of broadcasting the new track id. Re-issuing
            #   REPLACE on the old URI here would race the upcoming
            #   `_apply_track_change` and the seek would be to
            #   duration-of-old-track, restarting the old track at its
            #   end and immediately re-firing natural-end / restarting
            #   the cycle.
            and self._expected_phase not in (HandoffPhase.PAUSED, HandoffPhase.ENDING)
        ):
            await self._apply_idle_resume(state, target_player_id, expected_uri)
            return

        await self._apply_same_track_sync(state, target_player_id, queue, new_track)

    async def _apply_track_change(  # noqa: PLR0915 — sub-case branches inline
        self,
        state: YnisonState,
        target_player_id: str,
        new_track: str,
        expected_uri: str,
    ) -> None:
        """Handle a new-track Ynison event in handoff mode.

        Three sub-cases based on MA queue state for the expected URI:
        - PLAYING: dedup, just update bookkeeping;
        - PAUSED: cmd_play resume (no REPLACE, keeps queue PAUSED);
        - else: cancel any pending play_media + issue REPLACE.
        """
        queue = self.mass.player_queues.get(target_player_id)
        prev_track_id = self._expected_track_id
        self._active_player_id = target_player_id
        # Drop the elapsed snapshot — it belongs to the previous track.
        self._handoff_last_playing_elapsed_ms = 0

        # Heartbeat is started only on a clean activation; otherwise a
        # heartbeat after a failed play_media would keep streaming the
        # previous/idle queue progress to Ynison and delay rebalancing.
        activated = False

        same_uri_playing = (
            queue is not None
            and queue.current_item is not None
            and getattr(queue.current_item, "uri", None) == expected_uri
            and queue.state == PlaybackState.PLAYING
        )
        same_uri_paused = (
            queue is not None
            and queue.current_item is not None
            and getattr(queue.current_item, "uri", None) == expected_uri
            and queue.state == PlaybackState.PAUSED
        )

        if same_uri_playing:
            # Dedup (P5): MA already plays the URI; just update state.
            self.logger.debug(
                "Handoff: queue already playing %s — skipping play_media", expected_uri
            )
            self._expected_track_id = new_track
            self._handoff_completion_signaled_for = None
            activated = True
        elif same_uri_paused:
            # Same URI but paused — cmd_play resume (no REPLACE that
            # would restart the stream and trigger the watchdog → IDLE
            # drop on long pauses).
            self.logger.info("Handoff: resuming paused queue on %s", expected_uri)
            # Open a short drift-suppress window — cmd_play takes 100-500ms
            # to land, during which `_apply_same_track_sync` could see a
            # stale Ynison progress echo and fire a spurious seek.
            self._drift_suppress_until = time.monotonic() + _DRIFT_SUPPRESS_PERIOD
            self._expected_phase = HandoffPhase.ACTIVATING
            try:
                await self.mass.players.cmd_play(target_player_id)
            except Exception:
                self.logger.exception("Handoff resume cmd_play failed on %s", target_player_id)
            else:
                self._expected_track_id = new_track
                self._handoff_completion_signaled_for = None
                activated = True
        else:
            # Idempotency: same track-change command within 1s is a
            # duplicate (Ynison echoed our state back). Skip duplicate
            # play_media to avoid an unnecessary stream restart.
            if not self._idempotent("play_media", new_track):
                return
            await self._cancel_pending_play_media()
            self.logger.info(
                "Handoff: track changed %s -> %s, calling player_queues.play_media",
                prev_track_id,
                new_track,
            )
            # Open the activation window BEFORE the await — MA fires
            # PLAYER_UPDATED events while play_media is still resolving,
            # and our `_on_ma_player_event` handler must see
            # `_drift_suppress_until` already set so it forces
            # paused=False instead of leaking a stale paused=True. Same
            # for `_re_issue_debounce_until` — covers the window where a
            # paused=False echo could otherwise re-fire IDLE-resume
            # against an in-flight stream. Saved for rollback on failure.
            prev_drift = self._drift_suppress_until
            prev_debounce = self._re_issue_debounce_until
            prev_phase = self._expected_phase
            now = time.monotonic()
            self._drift_suppress_until = now + _DRIFT_SUPPRESS_PERIOD
            self._re_issue_debounce_until = now + _REISSUE_DEBOUNCE_PERIOD
            self._expected_phase = HandoffPhase.ACTIVATING
            try:
                self._play_media_task = asyncio.create_task(
                    self.mass.player_queues.play_media(
                        target_player_id, expected_uri, option=QueueOption.REPLACE
                    )
                )
                await self._play_media_task
            except asyncio.CancelledError:
                # Superseded by a fresher track change — leave the
                # _expected_track_id as-is for the new invocation, but
                # roll back the phase/windows so a series of cascaded
                # cancellations doesn't leave `_expected_phase = ACTIVATING`
                # and friends pointing at an in-flight task that no
                # longer exists. The successor invocation will set them
                # again right before its own await.
                self._drift_suppress_until = prev_drift
                self._re_issue_debounce_until = prev_debounce
                self._expected_phase = prev_phase
                raise
            except Exception:
                # play_media failed — don't commit _expected_track_id
                # (otherwise the same-track branch would never retry
                # play_media). Roll back the optimistic window/phase
                # state so they don't lie to subsequent calls.
                self._drift_suppress_until = prev_drift
                self._re_issue_debounce_until = prev_debounce
                self._expected_phase = prev_phase
                self.logger.exception(
                    "Handoff play_media failed for %s on %s",
                    expected_uri,
                    target_player_id,
                )
            else:
                self._expected_track_id = new_track
                self._handoff_completion_signaled_for = None
                activated = True
                # Honor Ynison's reported position when activating —
                # the user may have transferred a mid-track playback
                # from the Yandex app (track at 60s, switch device →
                # MA must continue from 60s, not restart at 0).
                # Same pause-seek-play sequence as _apply_idle_resume
                # to avoid the audible 0-then-jump glitch.
                start_ms = state.progress_ms
                if start_ms >= 1000:
                    self.logger.info(
                        "Handoff: continuing track from Ynison-reported position (seek=%dms)",
                        start_ms,
                    )
                    with suppress(Exception):
                        await self.mass.players.cmd_pause(target_player_id)
                    with suppress(Exception):
                        await self.mass.player_queues.seek(target_player_id, start_ms // 1000)
                    with suppress(Exception):
                        await self.mass.players.cmd_play(target_player_id)

        if activated:
            self._ensure_handoff_heartbeat()

    async def _apply_idle_resume(
        self, state: YnisonState, target_player_id: str, expected_uri: str
    ) -> None:
        """Re-issue play_media + seek when queue dropped to IDLE.

        Pausing a single-track REPLACE queue eventually drops MA's queue
        runner to IDLE. When Ynison says "playing this track" again,
        we have to spin the stream back up via REPLACE. The seek+
        cmd_pause/cmd_play dance avoids the audible 0-then-jump glitch.
        """
        # Debounce: a recent play_media is still resolving; don't fire
        # another REPLACE just because Ynison's paused=False keeps
        # echoing on every WS round-trip — the player would never settle.
        if time.monotonic() < self._re_issue_debounce_until:
            return

        # Pick resume position via shared helper — see `_pick_resume_position`.
        ma_snapshot = self._handoff_last_playing_elapsed_ms
        ynison_pos = state.progress_ms
        resume_ms, source = self._pick_resume_position(
            local_snapshot_ms=ma_snapshot,
            ynison_progress_ms=ynison_pos,
        )
        self.logger.info(
            "Handoff: queue IDLE on same URI — re-issuing play_media to resume "
            "(seek=%dms, source=%s, ma_snapshot=%dms, ynison=%dms)",
            resume_ms,
            source,
            ma_snapshot,
            ynison_pos,
        )
        # Open windows BEFORE the await — see `_apply_track_change` for
        # the rationale. MA's PLAYER_UPDATED events during play_media
        # must see them set so heartbeat doesn't leak paused=True.
        # Saved for rollback on exception so a failed resume doesn't
        # leave the provider stuck in an ACTIVATING/"not paused" reporting
        # window and silently suppress the next attempt for the duration
        # of `_REISSUE_DEBOUNCE_PERIOD`.
        prev_drift = self._drift_suppress_until
        prev_debounce = self._re_issue_debounce_until
        prev_phase = self._expected_phase
        now = time.monotonic()
        self._drift_suppress_until = now + _DRIFT_SUPPRESS_PERIOD
        self._re_issue_debounce_until = now + _REISSUE_DEBOUNCE_PERIOD
        self._expected_phase = HandoffPhase.ACTIVATING
        # Cancel any still-running play_media task before issuing the new
        # REPLACE — the cancel-on-track-change invariant from CLAUDE.md
        # applies here too. The 8s `_re_issue_debounce_until` guard makes
        # this rare in practice, but a slow `play_media` (>8s) followed
        # by an IDLE-resume could otherwise race two REPLACEs against
        # the same MA queue.
        await self._cancel_pending_play_media()
        try:
            await self.mass.player_queues.play_media(
                target_player_id, expected_uri, option=QueueOption.REPLACE
            )
        except Exception:
            # play_media itself failed — the stream never started, so
            # roll back the optimistic window/phase state. Otherwise
            # the windows would suppress a legitimate retry from the
            # next Ynison tick.
            self._drift_suppress_until = prev_drift
            self._re_issue_debounce_until = prev_debounce
            self._expected_phase = prev_phase
            self.logger.exception("Handoff IDLE-resume play_media failed on %s", target_player_id)
            return
        if resume_ms >= 1000:
            # play_media(REPLACE) succeeded and starts decoding at 0.
            # The pause/seek/play sequence trades the audible 0-then-jump
            # for brief silence. Each leg is best-effort: a transient
            # failure on cmd_pause / seek / cmd_play does NOT undo the
            # rolling stream — the windows must stay armed so the next
            # Ynison tick doesn't fire a duplicate REPLACE racing it.
            with suppress(Exception):
                await self.mass.players.cmd_pause(target_player_id)
            with suppress(Exception):
                await self.mass.player_queues.seek(target_player_id, resume_ms // 1000)
            with suppress(Exception):
                await self.mass.players.cmd_play(target_player_id)

    async def _apply_same_track_sync(
        self,
        state: YnisonState,
        target_player_id: str,
        queue: Any,
        new_track: str,
    ) -> None:
        """Run drift-seek + queue-PAUSED → cmd_play resume mirror.

        Runs on every same-track Ynison update where the IDLE-resume
        branch did not fire. Two responsibilities:
        - Drift seek when Ynison and MA disagree on position by >3s
          (with queue-rebuild guard for the progress=0 echo).
        - Resume MA queue if it transitioned to PAUSED while Ynison
          says playing (rare but possible if MA UI paused while Ynison
          remained PLAYING).
        """
        try:
            our_pos_ms = int(queue.corrected_elapsed_time * 1000) if queue is not None else 0
        except Exception:
            our_pos_ms = 0

        # Drift-seek suppression: ignore drift inside the activation
        # window EXCEPT when the queue is already PLAYING with elapsed
        # > 1s — a real user seek within the window must still pass.
        in_grace = time.monotonic() < self._drift_suppress_until
        queue_progressed = (
            queue is not None
            and queue.state == PlaybackState.PLAYING
            and queue.corrected_elapsed_time > 1.0
        )
        if in_grace and not queue_progressed:
            return

        # Drift detection: skip echoes — they'd reflect our own heartbeat
        # bouncing back, not a real seek. IDLE-/PAUSED-resume branches
        # above run regardless of echo; the skip is local to drift only.
        # Also skip if MA's queue has no current_item: the track ended,
        # `_signal_track_completion` may have sent progress=duration to
        # Ynison, and we'd otherwise try to seek the empty queue back to
        # that "phantom" position.
        if (
            not state.last_update_is_echo
            and queue is not None
            and getattr(queue, "current_item", None) is not None
        ):
            verdict = self._classify_drift(state.progress_ms, our_pos_ms)
            if verdict == "seek":
                self.logger.info(
                    "Handoff: seek detected on %s (Ynison=%dms, MA=%dms)",
                    new_track,
                    state.progress_ms,
                    our_pos_ms,
                )
                try:
                    await self.mass.player_queues.seek(target_player_id, state.progress_ms // 1000)
                except Exception:
                    self.logger.exception("Handoff seek failed on %s", target_player_id)
            elif verdict == "queue_rebuild":
                self.logger.debug(
                    "Handoff: drift to 0 ignored on %s (Ynison=%dms, MA=%dms) "
                    "— queue-rebuild echo, not a user seek",
                    new_track,
                    state.progress_ms,
                    our_pos_ms,
                )

        # MA queue paused while Ynison says playing → cmd_play resume.
        # cmd_play (not queue.play) keeps the queue in PAUSED → PLAYING
        # transition fast, avoids watchdog-driven IDLE drop.
        try:
            if queue is not None and queue.state == PlaybackState.PAUSED:
                await self.mass.players.cmd_play(target_player_id)
        except Exception:
            self.logger.debug("Handoff resume sync skipped", exc_info=True)

    async def _handoff_pause(self, target_player_id: str) -> None:
        """Pause MA queue when Ynison reports paused (handoff mode).

        Uses `mass.players.cmd_pause` rather than `mass.player_queues.pause`
        deliberately. The queue-level pause schedules a `_watch_pause`
        watchdog that calls `stop()` after 30s, dropping queue.state to
        IDLE — and a single-track REPLACE queue actually trips this
        watchdog within seconds, not 30. Once the queue is IDLE, every
        resume requires a fresh `play_media(REPLACE)` (3-5s of silence)
        instead of an instant `cmd_play`. Calling cmd_pause directly
        keeps queue.state == PAUSED for as long as the user wants and
        lets resume go through the fast path.
        """
        # Suppress duplicate pause within the idempotency TTL — Ynison may
        # echo the same `paused=True` state multiple times back-to-back.
        if not self._idempotent("pause", target_player_id):
            return
        # Set expected_phase BEFORE the await — MA fires PLAYER_UPDATED
        # events while cmd_pause is still resolving, and our event handler
        # must already see expected_phase=PAUSED so it reports paused=True
        # to Ynison instead of leaking the stale "still PLAYING" state
        # (transient ~2s gap between cmd_pause start and queue.state
        # transitioning to PAUSED).
        prev_phase = self._expected_phase
        self._expected_phase = HandoffPhase.PAUSED
        # Also close the drift_suppress window if it was open (a
        # resume that the user is now cancelling) — otherwise the
        # Yandex-app button shows "playing" until the window expires.
        self._drift_suppress_until = 0.0
        # Echo paused=True to Ynison right now (don't wait the up-to-5s
        # heartbeat tick) so the Yandex-app pause button reflects state
        # immediately. Best-effort — heartbeat will retry if this fails.
        queue = self.mass.player_queues.get(target_player_id)
        elapsed_ms = int(queue.corrected_elapsed_time * 1000) if queue else 0
        with suppress(Exception):
            await self._send_progress_to_ynison(
                progress_ms=elapsed_ms,
                duration_ms=self._best_duration_ms(),
                paused=True,
            )
        # Mark progress sync watermark to skip the next heartbeat tick.
        # Without this, the heartbeat ~5s after our explicit echo would
        # fire and — in the worst case where heartbeat scheduling lands
        # in the middle of pause processing — race against the user's
        # paused=True with a stale paused=False, briefly flipping the
        # Yandex-app button back to "play".
        self._handoff_last_progress_sync_mono = time.monotonic()
        try:
            await self.mass.players.cmd_pause(target_player_id)
        except Exception:
            self.logger.debug("Handoff pause failed on %s", target_player_id, exc_info=True)
            # Roll back the optimistic phase change so future `is_paused`
            # evaluations don't lie.
            self._expected_phase = prev_phase
        # Replay corner case (P6): if Ynison parked us at progress=0 and
        # then asked for pause, future end-of-track signalling should fire.
        if self._ynison and self._ynison.state.progress_ms < 1000:
            self._handoff_completion_signaled_for = None

    def _ensure_handoff_heartbeat(self) -> None:
        """Start the handoff heartbeat task if not already running."""
        if self._handoff_heartbeat_task is not None and not self._handoff_heartbeat_task.done():
            return
        self._handoff_heartbeat_task = self.mass.create_task(self._handoff_heartbeat_loop())

    def _cancel_handoff_heartbeat(self) -> None:
        """Cancel the handoff heartbeat task (idempotent)."""
        task = self._handoff_heartbeat_task
        self._handoff_heartbeat_task = None
        if task is not None and not task.done():
            task.cancel()

    async def _handoff_heartbeat_loop(self) -> None:
        """Push progress to Ynison on a fixed cadence in handoff mode.

        Ynison may re-balance the active device away from us if we go silent
        (error 300100001). MA's QUEUE_TIME_UPDATED events arrive sparsely on
        DLNA/UPnP renderers — so we need an independent timer that keeps the
        Ynison server informed even when the player itself is quiet.

        The loop reuses ``_handoff_last_progress_sync_mono`` as a watermark:
        when a real MA event has just sent progress, the heartbeat skips its
        next tick to avoid double-sending.
        """
        try:
            while True:
                await asyncio.sleep(self._handoff_heartbeat_interval)
                if not self._is_handoff:
                    return
                if not self._ynison or not self._ynison.connected:
                    continue
                target_player_id = self._active_player_id
                if not target_player_id:
                    continue
                queue = self.mass.player_queues.get(target_player_id)
                if queue is None:
                    self.logger.debug(
                        "Handoff heartbeat: queue %s vanished — clearing active player",
                        target_player_id,
                    )
                    self._clear_active_player()
                    return
                # Skip if a real MA event just pushed progress within the
                # heartbeat window — avoid duplicate update_playing_status.
                now_mono = time.monotonic()
                if (
                    now_mono - self._handoff_last_progress_sync_mono
                    < self._handoff_heartbeat_interval / 2
                ):
                    continue
                self._handoff_last_progress_sync_mono = now_mono
                elapsed_ms = int(queue.corrected_elapsed_time * 1000)
                duration_ms = self._best_duration_ms()
                if duration_ms <= 0 and queue.current_item is not None:
                    duration_ms = (queue.current_item.duration or 0) * 1000
                # Treat anything other than PLAYING as paused — single-track
                # MA queues land in IDLE on pause, not PAUSED, and reporting
                # paused=False in that case made the Yandex Music app think
                # we kept playing while the player was actually silent.
                #
                # Exception: while we're still inside the activation/resume
                # window (`_drift_suppress_until > now`) the queue is briefly
                # IDLE because MA hasn't started the stream yet — reporting
                # paused=True in that gap makes Ynison-app show "paused" and
                # the user re-taps play, firing duplicate REPLACE that races
                # the first. Override to paused=False during that window so
                # the app stays consistent with our intent (we're starting).
                # Resolve paused: PAUSED > ACTIVATING > activation window > queue.state.
                # PAUSED first so a user pause inside the activation window
                # is reflected immediately. ACTIVATING second so a slow
                # play_media (e.g. ~15s on Hi-Res FLAC + slow CDN, post-
                # auto-advance) doesn't time out the activation window
                # and start reporting paused=True while the stream is
                # still starting — Ynison-app would otherwise flip to
                # "paused" mid-load and the user's next tap races the
                # in-flight stream.
                if self._expected_phase == HandoffPhase.PAUSED:
                    is_paused = True
                elif (
                    self._expected_phase == HandoffPhase.ACTIVATING
                    or time.monotonic() < self._drift_suppress_until
                ):
                    is_paused = False
                else:
                    is_paused = queue.state != PlaybackState.PLAYING
                self.logger.debug(
                    "Handoff heartbeat: tick player=%s elapsed=%dms paused=%s",
                    target_player_id,
                    elapsed_ms,
                    is_paused,
                )
                with suppress(Exception):
                    await self._send_progress_to_ynison(
                        progress_ms=elapsed_ms,
                        duration_ms=duration_ms,
                        paused=is_paused,
                    )
                # Heartbeat-side natural-end detection. MA's event bus
                # sometimes drops the PLAYING→PAUSED/IDLE transition
                # event for handoff (we observed cases where stream
                # finished cleanly but `_on_ma_player_event` never ran
                # for the state change). Heartbeat tick fires every
                # ≤5s reliably, so doing the same check here fills the
                # gap. Idempotent via `_handoff_completion_signaled_for`.
                # IDLE only — accepting PAUSED here would create a tiny
                # race where a user pause near end-of-track lands before
                # `_handoff_pause` commits `_expected_phase = PAUSED`,
                # firing a false-positive completion. The
                # `_on_ma_player_event` path uses `_expected_phase` as
                # the user-pause filter; the heartbeat uses queue.state
                # directly because it can fire BEFORE the phase commit.
                if (
                    self._expected_phase == HandoffPhase.PLAYING
                    and queue.state == PlaybackState.IDLE
                    and self._expected_track_id
                    and self._handoff_completion_signaled_for != self._expected_track_id
                    and self._is_at_natural_end_of_track(queue)
                ):
                    self._handoff_completion_signaled_for = self._expected_track_id
                    self._expected_phase = HandoffPhase.ENDING
                    self.logger.info(
                        "Handoff heartbeat: detected natural-end on %s "
                        "(queue.state=%s, elapsed=%dms) — signalling "
                        "completion to Ynison",
                        self._expected_track_id,
                        queue.state.value if hasattr(queue.state, "value") else queue.state,
                        elapsed_ms,
                    )
                    self.mass.create_task(self._signal_track_completion())
        except asyncio.CancelledError:
            pass

    def _on_ma_player_event(self, event: MassEvent) -> None:
        """Mirror MA queue progress and stop-events back to Ynison (handoff)."""
        if not self._is_handoff:
            return
        if not self._ynison or not self._ynison.connected:
            return
        target_player_id = self._active_player_id
        if not target_player_id or event.object_id != target_player_id:
            return

        queue = self.mass.player_queues.get(target_player_id)
        if queue is None:
            return

        # P10: detect playback_state transitions and force a Ynison update
        # immediately, bypassing the throttle. State changes are rare events
        # (play/pause/idle), so this never floods the WS.
        current_state = queue.state
        prev_state = self._handoff_last_seen_state
        state_changed = current_state != prev_state
        self._handoff_last_seen_state = current_state

        # Throttle progress-only updates — at most one every _PROGRESS_SYNC_INTERVAL.
        now_mono = time.monotonic()
        if (
            not state_changed
            and now_mono - self._handoff_last_progress_sync_mono < _PROGRESS_SYNC_INTERVAL
        ):
            return
        self._handoff_last_progress_sync_mono = now_mono
        elapsed_ms = int(queue.corrected_elapsed_time * 1000)
        duration_ms = self._best_duration_ms()
        if duration_ms <= 0 and queue.current_item is not None:
            duration_ms = (queue.current_item.duration or 0) * 1000

        # Snapshot the elapsed position whenever the queue is genuinely PLAYING.
        # On IDLE-resume we'd rather seek back to this remembered offset than
        # trust Ynison's `progress_ms` echo, which lags behind a fast pause/play
        # toggle and would otherwise restart the track at 0.
        if queue.state == PlaybackState.PLAYING and elapsed_ms > 0:
            self._handoff_last_playing_elapsed_ms = elapsed_ms
            # FSM: confirmed transition to PLAYING — clear ACTIVATING.
            if self._expected_phase in (HandoffPhase.ACTIVATING, HandoffPhase.PAUSED):
                self._expected_phase = HandoffPhase.PLAYING

        # Resolve paused: PAUSED > ACTIVATING > activation window > queue.state.
        # See heartbeat resolver for the rationale.
        if self._expected_phase == HandoffPhase.PAUSED:
            is_paused = True
        elif (
            self._expected_phase == HandoffPhase.ACTIVATING
            or time.monotonic() < self._drift_suppress_until
        ):
            is_paused = False
        else:
            is_paused = queue.state != PlaybackState.PLAYING
        self.mass.create_task(
            self._send_progress_to_ynison(
                progress_ms=elapsed_ms,
                duration_ms=duration_ms,
                paused=is_paused,
            )
        )

        # End-of-track signal — primary path is the dedicated
        # `_on_ma_media_item_played` handler (MEDIA_ITEM_PLAYED event).
        # Belt-and-braces fallback below: detect queue state transition
        # PLAYING → {PAUSED, IDLE} near track duration. MEDIA_ITEM_PLAYED
        # is fired by MA only when the queue transitions from one media
        # item to the next OR when MA's `_handle_playback_progress_report`
        # decides the item is "fully played" (>= 90% playback time);
        # in some single-track REPLACE scenarios (e.g. seek-to-near-end
        # then short remaining playback) the threshold can fail and we'd
        # never get the event. The state-transition check fills that gap
        # — gated on `_expected_phase == PLAYING` to skip user pauses
        # mid-track (which set PAUSED via `_handoff_pause`). The marker
        # `_handoff_completion_signaled_for` is shared with the primary
        # path so they don't double-fire.
        # Strictly IDLE — accepting PAUSED here would create a race with
        # MA-UI-initiated pauses. When the user pauses via MA's web UI
        # (not the Yandex app), MA's PLAYER_UPDATED fires synchronously
        # before Ynison's `paused=True` round-trips back through the WS;
        # `_handoff_pause` hasn't yet committed `_expected_phase = PAUSED`,
        # so the phase guard above is still True. If the pause lands
        # near track end, this would falsely trigger completion. PAUSED
        # → IDLE follow-up will catch the natural end via the second
        # transition once MA's queue runner clears.
        if (
            state_changed
            and prev_state == PlaybackState.PLAYING
            and current_state == PlaybackState.IDLE
            and self._expected_phase == HandoffPhase.PLAYING
            and self._expected_track_id
            and self._handoff_completion_signaled_for != self._expected_track_id
            and self._is_at_natural_end_of_track(queue)
        ):
            self._handoff_completion_signaled_for = self._expected_track_id
            self._expected_phase = HandoffPhase.ENDING
            self.logger.info(
                "Handoff: queue PLAYING -> IDLE on %s near duration — "
                "signalling natural-end completion to Ynison",
                self._expected_track_id,
            )
            self.mass.create_task(self._signal_track_completion())

    def _is_at_natural_end_of_track(self, queue: Any) -> bool:
        """Return True iff the queue's elapsed time is close to track duration.

        Used to distinguish a queue that went IDLE because the track played
        out (advance needed) from one that went IDLE due to pause/stop on a
        single-track queue (no advance needed). Conservative: when both
        primary and fallback signals are unavailable we return False —
        better to leave the user paused than to cascade through the RADIO
        tail.

        Two checks, in order:
        1. queue.current_item still set + corrected_elapsed_time near
           current_item.duration. Canonical case while MA's queue state
           machine has the item loaded.
        2. queue cleared (current_item=None): use the last-known PLAYING
           snapshot (`_handoff_last_playing_elapsed_ms`) against Ynison's
           reported duration (`_best_duration_ms`). Catches the case
           where MA's "End of queue reached" cleared current_item before
           our event handler ran — without it, the natural-end signal
           never fires and Ynison sits silent waiting for a next-track
           command we never send.
        """
        current_item = getattr(queue, "current_item", None)
        if current_item is not None:
            duration = getattr(current_item, "duration", None) or 0
            if duration > 0:
                elapsed = getattr(queue, "corrected_elapsed_time", 0.0) or 0.0
                # 5s margin covers fade-out / silence at end-of-track plus
                # reporting jitter from the player. Track shorter than 5s
                # is treated as "always near end" — pause-then-cascade on
                # sub-5s tracks is indistinguishable from end-of-track.
                return elapsed >= max(0.0, duration - 5.0)
        # Fallback: queue cleared before we got the chance to inspect it.
        duration_ms = self._best_duration_ms()
        if duration_ms > 0:
            snapshot_ms = self._handoff_last_playing_elapsed_ms
            return snapshot_ms >= max(0, duration_ms - 5000)
        return False

    # ------------------------------------------------------------------
    # Playback control callbacks
    # ------------------------------------------------------------------

    def _best_duration_ms(self) -> int:
        """Return the best known duration: actual from stream, or Ynison state as fallback."""
        if self._actual_duration_ms > 0:
            return self._actual_duration_ms
        if self._ynison:
            return self._ynison.state.duration_ms
        return 0

    async def _on_play(self) -> None:
        """Handle play command — send resume to Ynison."""
        if not self._ynison:
            raise UnsupportedFeaturedException("Ynison client not initialized")
        if not self._ynison.connected:
            raise PlayerCommandFailed("Ynison WebSocket disconnected")
        # Idempotency: MA may fire the same play callback twice (e.g.
        # racing UI and remote-control taps). Collapse duplicates so
        # the WS doesn't see two `paused=False` updates within 1s.
        if not self._idempotent("on_play", None):
            return
        state = self._ynison.state
        await self._send_progress_to_ynison(
            progress_ms=state.progress_ms,
            duration_ms=self._best_duration_ms(),
            paused=False,
        )

    async def _on_pause(self) -> None:
        """Handle pause command — send pause to Ynison."""
        if not self._ynison:
            raise UnsupportedFeaturedException("Ynison client not initialized")
        if not self._ynison.connected:
            raise PlayerCommandFailed("Ynison WebSocket disconnected")
        # Idempotency: same as `_on_play` — drop a duplicate pause
        # within the TTL window so we don't echo paused=True twice.
        if not self._idempotent("on_pause", None):
            return
        state = self._ynison.state
        await self._send_progress_to_ynison(
            progress_ms=state.progress_ms,
            duration_ms=self._best_duration_ms(),
            paused=True,
        )

    # Entity types that use server-side "radio" queue replenishment.
    # Currently only RADIO (personal wave, genre stations).
    # Add "WAVE" here if/when Yandex supports it via the same
    # rotor_station_tracks API.
    _RADIO_ENTITY_TYPES = {"RADIO"}

    def _maybe_prefetch(
        self,
        current_index: int,
        playable_list: list[dict[str, Any]],
        entity_id: str,
        entity_type: str,
    ) -> None:
        """Kick off background prefetch when nearing the end of the queue."""
        if entity_type not in self._RADIO_ENTITY_TYPES:
            return
        if not self._yandex_provider or not playable_list:
            return
        # second-to-last or last — trigger prefetch near end of queue
        if current_index < len(playable_list) - 2:
            return
        # Already prefetched or prefetch in progress
        if self._prefetched_list is not None:
            return
        if self._prefetch_task and not self._prefetch_task.done():
            return

        self.logger.info(
            "Pre-fetching tracks (at index %d/%d, entity=%s)",
            current_index,
            len(playable_list),
            entity_id[:40] if entity_id else "<none>",
        )

        async def _do_prefetch() -> None:
            result = await self._replenish_radio_queue(entity_id, entity_type, playable_list)
            if result:
                self._prefetched_list = result
                # Push expanded queue to Ynison immediately so the YM app
                # sees upcoming tracks and enables the "next" button.
                await self._update_queue_list(result)

        self._prefetch_task = self.mass.create_task(_do_prefetch())

    async def _signal_track_completion(self) -> None:
        """Signal that the current track finished playing.

        Ynison is a state-sync protocol — the active device must advance
        current_playable_index itself.

        If the next index is within the playable list, we advance immediately.
        If we're at the end (typical for RADIO/wave with short queues),
        we fetch more tracks via the Yandex Music API, append them to the
        playable_list, and then advance.
        """
        if not self._ynison:
            return
        state = self._ynison.state
        duration = self._best_duration_ms()
        queue = state.player_state.get("player_queue", {})
        current_index = queue.get("current_playable_index", 0)
        playable_list = queue.get("playable_list", [])
        entity_type = queue.get("entity_type", "")
        entity_id = queue.get("entity_id", "")
        next_index = current_index + 1

        self.logger.info(
            "Track finished at index %d/%d (entity=%s type=%s), "
            "advancing to index %d (duration=%dms)",
            current_index,
            len(playable_list),
            entity_id[:40] if entity_id else "<none>",
            entity_type,
            next_index,
            duration,
        )
        self._actual_duration_ms = 0

        # 1. Report that playback reached the end.
        # Echo tracking is handled by _send_progress_to_ynison.
        await self._send_progress_to_ynison(
            progress_ms=duration, duration_ms=duration, paused=False
        )

        if next_index < len(playable_list):
            # 2a. Queue has room — advance immediately.
            # Clear stale prefetch data so _maybe_prefetch can trigger for
            # the new queue tail on subsequent state updates.
            self._prefetched_list = None
            await self._advance_queue_index(next_index)
        elif entity_type in self._RADIO_ENTITY_TYPES:
            # 2b. At end of RADIO queue — use prefetched data or fetch now
            expanded: list[dict[str, Any]] | None = None
            if self._prefetched_list:
                self.logger.info("Using pre-fetched queue (%d items)", len(self._prefetched_list))
                expanded = self._prefetched_list
                self._prefetched_list = None
            elif self._prefetch_task and not self._prefetch_task.done():
                self.logger.info("Waiting for in-flight prefetch...")
                await self._prefetch_task
                expanded = self._prefetched_list
                self._prefetched_list = None
            else:
                expanded = await self._replenish_radio_queue(entity_id, entity_type, playable_list)
            if expanded and next_index < len(expanded):
                await self._advance_queue_index(next_index, expanded_list=expanded)
            elif expanded:
                self.logger.warning(
                    "Expanded queue has %d items but next_index=%d — re-fetching",
                    len(expanded),
                    next_index,
                )
                fresh = await self._replenish_radio_queue(entity_id, entity_type, expanded)
                if fresh and next_index < len(fresh):
                    await self._advance_queue_index(next_index, expanded_list=fresh)
                else:
                    self.logger.warning("Still cannot advance after re-fetch")
            else:
                self.logger.warning(
                    "Could not replenish queue (entity=%s type=%s), cannot advance",
                    entity_id,
                    entity_type,
                )
        else:
            self.logger.info(
                "End of non-radio queue (entity=%s type=%s), playback complete",
                entity_id[:40] if entity_id else "<none>",
                entity_type,
            )

    async def _replenish_radio_queue(
        self,
        entity_id: str,
        entity_type: str,
        playable_list: list[dict[str, Any]],
    ) -> list[dict[str, Any]] | None:
        """Fetch more tracks from Yandex Music API and return expanded playable_list.

        The active device is responsible for replenishing RADIO/wave queues.
        Ynison only syncs state — it does NOT generate new tracks.
        """
        if not self._yandex_provider:
            self.logger.warning("No yandex_music provider available for radio replenishment")
            return None

        # Determine the last track ID for pagination
        last_track_id: str | None = None
        if playable_list:
            last_track_id = playable_list[-1].get("playable_id")

        self.logger.info(
            "Fetching more tracks for %s station %s (queue=%s)",
            entity_type,
            entity_id,
            last_track_id,
        )

        try:
            tracks, batch_id = await self._yandex_provider.get_rotor_station_tracks(
                entity_id, queue=last_track_id
            )
        except Exception:
            self.logger.exception("Failed to fetch radio tracks for %s", entity_id)
            return None

        if not tracks:
            self.logger.warning("No tracks returned for station %s", entity_id)
            return None

        # Determine the 'from' field from existing items
        from_field = ""
        if playable_list:
            from_field = playable_list[0].get("from", "")

        # Convert tracks to Ynison playable_list format
        new_items: list[dict[str, Any]] = []
        for track in tracks:
            album_id = ""
            if hasattr(track, "albums") and track.albums:
                album_id = str(track.albums[0].id) if track.albums[0].id else ""
            cover = ""
            if hasattr(track, "cover_uri") and track.cover_uri:
                cover = track.cover_uri
            new_items.append(
                {
                    "playable_id": str(track.id),
                    "album_id_optional": album_id,
                    "playable_type": "TRACK",
                    "from": from_field,
                    "title": track.title or "",
                    "cover_url_optional": cover,
                }
            )

        self.logger.info(
            "Fetched %d new tracks for station %s (batch=%s)",
            len(new_items),
            entity_id,
            batch_id,
        )

        return list(playable_list) + new_items

    async def _advance_queue_index(
        self,
        next_index: int,
        *,
        expanded_list: list[dict[str, Any]] | None = None,
    ) -> None:
        """Send update_player_state to advance the queue to next_index.

        If expanded_list is provided, it replaces the playable_list
        (used after radio queue replenishment).

        Waits up to 10 s for reconnection if Ynison is temporarily
        disconnected (e.g. after a transient error).
        """
        if not self._ynison:
            return
        if not self._ynison.connected:
            self.logger.info("Waiting for Ynison reconnection before advancing queue…")
            for _ in range(10):
                await asyncio.sleep(1)
                if not self._ynison or self._ynison.connected:
                    break
            if not self._ynison or not self._ynison.connected:
                self.logger.warning("Cannot advance queue — Ynison still disconnected")
                return
        state = self._ynison.state
        queue = state.player_state.get("player_queue", {})
        device_id = self._ynison.device_id
        new_state = dict(state.player_state)
        new_state["player_queue"] = dict(queue)
        new_state["player_queue"]["current_playable_index"] = next_index
        new_state["player_queue"]["version"] = make_version_block(device_id)
        if expanded_list is not None:
            new_state["player_queue"]["playable_list"] = expanded_list
        new_state["status"] = dict(new_state.get("status", {}))
        new_state["status"]["progress_ms"] = "0"
        new_state["status"]["duration_ms"] = "0"
        new_state["status"]["paused"] = False
        new_state["status"]["version"] = make_version_block(device_id)
        await self._ynison.update_player_state(player_state=new_state)

    async def _update_queue_list(self, expanded_list: list[dict[str, Any]]) -> None:
        """Push an expanded playable_list to Ynison without changing index or progress.

        Called right after prefetch completes so the YM app sees upcoming
        tracks and enables the "next" button.
        """
        if not self._ynison or not self._ynison.connected:
            return
        state = self._ynison.state
        queue = state.player_state.get("player_queue", {})
        device_id = self._ynison.device_id
        new_state = dict(state.player_state)
        new_state["player_queue"] = dict(queue)
        new_state["player_queue"]["playable_list"] = expanded_list
        new_state["player_queue"]["version"] = make_version_block(device_id)
        await self._ynison.update_player_state(player_state=new_state)

    async def _on_next(self) -> None:
        """Handle next track command — signal track end so Yandex advances."""
        if not self._ynison:
            raise UnsupportedFeaturedException("Ynison client not initialized")
        if not self._ynison.connected:
            raise PlayerCommandFailed("Ynison WebSocket disconnected")
        await self._signal_track_completion()

    async def _on_previous(self) -> None:
        """Handle previous track command — update queue index in Ynison."""
        if not self._ynison:
            raise UnsupportedFeaturedException("Ynison client not initialized")
        if not self._ynison.connected:
            raise PlayerCommandFailed("Ynison WebSocket disconnected")
        queue = self._ynison.state.player_state.get("player_queue", {})
        current_index = queue.get("current_playable_index", 0)
        if current_index > 0:
            self._actual_duration_ms = 0
            await self._advance_queue_index(current_index - 1)

    async def _on_seek(self, position: int) -> None:
        """Handle seek command — send position update to Ynison.

        :param position: Position in seconds from Music Assistant.
        """
        if not self._ynison:
            raise UnsupportedFeaturedException("Ynison client not initialized")
        if not self._ynison.connected:
            raise PlayerCommandFailed("Ynison WebSocket disconnected")
        seek_ms = position * 1000
        state = self._ynison.state
        await self._send_progress_to_ynison(
            progress_ms=seek_ms,
            duration_ms=self._best_duration_ms(),
            paused=state.is_paused,
        )
        # Also trigger local stream restart so seek takes effect
        # immediately without waiting for the Ynison echo.
        self._seek_position_ms = seek_ms
        self._seek_grace_until = time.monotonic() + _ECHO_GRACE_PERIOD
        self._track_changed_event.set()
