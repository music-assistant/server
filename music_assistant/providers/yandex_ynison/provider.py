"""Yandex Ynison plugin provider for Music Assistant."""

from __future__ import annotations

import asyncio
import hashlib
import random
import time
from collections.abc import AsyncGenerator, Callable
from contextlib import aclosing, suppress
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar, Literal, cast

from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption
from music_assistant_models.enums import (
    ConfigEntryType,
    ContentType,
    EventType,
    MediaType,
    PlaybackState,
    ProviderFeature,
    ProviderType,
    SourceControl,
    StreamType,
)
from music_assistant_models.errors import (
    InvalidDataError,
    LoginFailed,
    MediaNotFoundError,
    PlayerCommandFailed,
    UnsupportedFeaturedException,
)
from music_assistant_models.media_items import AudioSource, ProviderMapping
from music_assistant_models.streamdetails import StreamDetails, StreamMetadata
from ya_passport_auth import SecretStr
from ya_passport_auth.ma import BorrowedCredentialSource

from music_assistant.controllers.streams.constants import STREAM_SLOT_PLAYBACK_WAIT_TIMEOUT
from music_assistant.helpers.ffmpeg import get_ffmpeg_stream
from music_assistant.helpers.throttle_retry import BYPASS_THROTTLER, ThrottlerManager
from music_assistant.models.plugin import PluginProvider, SourceControlValue

from .auth import refresh_music_token
from .constants import (
    CONF_ALLOW_PLAYER_SWITCH,
    CONF_DEVICE_ID,
    CONF_MASS_PLAYER_ID,
    CONF_OUTPUT_BIT_DEPTH,
    CONF_OUTPUT_SAMPLE_RATE,
    CONF_PUBLISH_NAME,
    CONF_TOKEN,
    CONF_X_TOKEN,
    CONF_YM_INSTANCE,
    DEFAULT_DISPLAY_NAME,
    OUTPUT_AUTO,
    PLAYER_ID_AUTO,
    YANDEX_MUSIC_CONF_QUALITY,
    YANDEX_MUSIC_LOSSLESS_QUALITIES,
    YM_INSTANCE_OWN,
)
from .protocols import YandexMusicProviderLike
from .streaming import (
    PCM_LOSSLESS_PARAMS,
    PCM_LOSSY_PARAMS,
    PROBE_ARGS,
    make_pcm_format,
)
from .ynison_client import (
    YnisonClient,
    YnisonDeviceInfo,
    YnisonSendError,
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
_PROGRESS_SYNC_INTERVAL = 5.0

# Grace window after our own REPLACE/seek during which incoming Ynison
# progress updates are treated as our own echo (not a user seek).
_ECHO_GRACE_PERIOD = 3.0

# Bound on the synchronous pre-fetch in _prefetch_format_for_track. A slow
# pre-fetch is treated like a failed one — fall back to the current format
# and let the in-stream `_get_stream_details_with_retry` handle retries.
_PREFETCH_FORMAT_TIMEOUT = 2.5

# Idempotency cache TTL for outbound peer-commands.
_COMMAND_IDEMPOTENCY_TTL = 1.0

# stable id for the single AudioSource this provider exposes;
# combined with the provider instance_id this forms the persistent uri
AUDIO_SOURCE_ID = "main"

# Retry settings for transient Yandex API failures
_API_MAX_RETRIES = 3
_API_INITIAL_BACKOFF = 2.0
_API_MAX_BACKOFF = 30.0

# Cache TTL for stream details (seconds)
_STREAM_DETAILS_CACHE_TTL = 300  # 5 minutes

# In-memory music-token cache TTL (seconds). Yandex music tokens live ~60 min;
# 50 min leaves 10 min headroom before the server would reject them. Tied to
# the borrow-mode-with-only-x_token + 401-storm path described in spec 0004.
_MUSIC_TOKEN_TTL_S = 50 * 60

# Maximum number of distinct x_token entries kept in the own-mode music-token
# cache (borrow mode caches inside BorrowedCredentialSource). 4 keeps headroom
# for an x_token rotation with one refresh in flight.
_MUSIC_TOKEN_CACHE_MAX = 4

# Accepted non-auto values for output format overrides; mirrors the options
# offered in CONF_OUTPUT_SAMPLE_RATE / CONF_OUTPUT_BIT_DEPTH config entries.
# Used defensively to reject stale/tampered values without raising.
_VALID_SAMPLE_RATES: frozenset[str] = frozenset({"44100", "48000", "96000"})
_VALID_BIT_DEPTHS: frozenset[str] = frozenset({"16", "24"})


class _StreamOwnerMismatchError(InvalidDataError):
    """Raised when linked-provider stream details belong to another instance."""


@dataclass(frozen=True)
class _CachedToken:
    """
    Music token entry in the in-memory cache.

    `expires_monotonic` is compared against the provider's `_now()` seam.
    """

    token: SecretStr
    expires_monotonic: float


def _hash_x_token(x_token: str) -> str:
    """
    Return the SHA-256 hex digest of an x_token, used as cache key.

    The raw x_token is never stored in dict keys (defence-in-depth against
    accidental log / dump leakage of the cache structure).
    """
    return hashlib.sha256(x_token.encode("utf-8")).hexdigest()


class YandexYnisonProvider(PluginProvider):
    """Implementation of the Yandex Music Connect (Ynison) Plugin."""

    # PluginProvider base does not declare `is_streaming_provider`; MA's
    # audio-analysis path raises AttributeError for live sources without
    # an explicit opt-out. Analysing transient external-source tracks
    # buys nothing.
    is_streaming_provider: bool = False

    @property
    def instance_name_postfix(self) -> str | None:
        """Return display name as instance postfix for multi-instance setups."""
        name = self._display_name
        return name if name != DEFAULT_DISPLAY_NAME else None

    def __init__(
        self,
        mass: MusicAssistant,
        manifest: ProviderManifest,
        config: ProviderConfig,
        supported_features: set[ProviderFeature],
    ) -> None:
        """Initialize the Ynison plugin provider."""
        super().__init__(mass, manifest, config, supported_features)

        # Setup identity and playback options
        self._default_player_id: str = (
            cast("str", self.get_setup_value(CONF_MASS_PLAYER_ID)) or PLAYER_ID_AUTO
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
            cast("str", self.get_setup_value(CONF_PUBLISH_NAME)) or DEFAULT_DISPLAY_NAME
        )

        # Token source — None = own (manually entered CONF_TOKEN);
        # otherwise the instance_id of a linked yandex_music provider to borrow from.
        ym_instance_value = cast("str | None", self.get_setup_value(CONF_YM_INSTANCE))
        self._ym_instance_id: str | None = (
            ym_instance_value
            if ym_instance_value and ym_instance_value != YM_INSTANCE_OWN
            else None
        )
        # Borrow mode: read-only credential source over the linked
        # yandex_music instance (shared auth layer). The owner stays the
        # single writer/rotator of persisted credentials; minted music
        # tokens are cached in-memory inside the source (TTL + LRU +
        # coalesced refreshes per its spec).
        self._borrow_source: BorrowedCredentialSource | None = (
            BorrowedCredentialSource(self.mass, self._ym_instance_id)
            if self._ym_instance_id is not None
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

        # Rate limiter for Yandex API calls (max 2 req/s)
        self._api_throttler = ThrottlerManager(rate_limit=2, period=1.0)

        # Progress tracking — byte counter is the single source of truth
        # during active streaming; Ynison echoes are detected via
        # YnisonState.last_update_is_echo and ignored.
        self._streaming_progress_ms: int = 0

        # AudioSource MediaItem + per-stream state
        self._stream_metadata = StreamMetadata(
            title=f"Yandex Music Connect | {self._display_name}",
        )
        self._audio_source = AudioSource(
            item_id=AUDIO_SOURCE_ID,
            provider=self.instance_id,
            name=self.name,
            provider_mappings={
                ProviderMapping(
                    item_id=AUDIO_SOURCE_ID,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                    # Fresh AudioFormat copy: AudioFormat is mutable and MA's
                    # FFMpeg._log_reader_task sets `input_format.codec_type`
                    # in-place. Sharing `self._normalized_format` here would
                    # let that mutation leak into the ProviderMapping and into
                    # later StreamDetails snapshots.
                    audio_format=make_pcm_format(self._normalized_params),
                )
            },
            can_play_pause=False,
            can_seek=False,
            can_next_previous=False,
            exclusive=True,
            allow_external_trigger=True,
        )
        # _in_use_by_player tracks the queue currently consuming our stream
        self._in_use_by_player: str | None = None
        # _active_session_id is the controller-provided token for the current
        # stream request — used to reject stale on_source_unselected callbacks
        # after a same-queue reconnect supersedes the previous request.
        self._active_session_id: str | None = None

        # Idempotency cache for outbound peer-commands. Suppresses duplicate
        # (action, key) pairs inside `_COMMAND_IDEMPOTENCY_TTL` — protects
        # against echo-storms where the same Ynison broadcast lands on our
        # state-handler twice in quick succession.
        self._command_idempotency: dict[tuple[str, str | None], float] = {}

        # "Ynison paused us externally — expect a resume that needs
        # `play_media` re-issuance." Set in `_pause_playback`, read in
        # `_activate_playback`. Survives a stray `_stream_stop_event` clear
        # independent of the stop signal (which covers non-pause stop reasons).
        self._externally_paused: bool = False

        # In-memory music-token cache keyed by SHA-256(x_token). 50-min TTL,
        # 4-entry LRU. Coalesces concurrent refresh attempts via a single
        # asyncio.Lock so a reconnect storm makes at most one Passport call.
        # `_now` is a seam for tests to advance the clock.
        self._token_cache: dict[str, _CachedToken] = {}
        self._token_refresh_lock = asyncio.Lock()
        self._now: Callable[[], float] = time.monotonic

    async def get_config_entries(self) -> tuple[ConfigEntry, ...]:
        """
        Return Config entries to configure this provider.

        Account, player and device identity are collected by the interactive setup flow;
        only runtime playback options live here.
        """
        return (
            ConfigEntry(
                key=CONF_ALLOW_PLAYER_SWITCH,
                type=ConfigEntryType.BOOLEAN,
                default_value=True,
            ),
            ConfigEntry(
                key=CONF_OUTPUT_SAMPLE_RATE,
                type=ConfigEntryType.STRING,
                default_value=OUTPUT_AUTO,
                options=[
                    ConfigValueOption(OUTPUT_AUTO),
                    ConfigValueOption("44100"),
                    ConfigValueOption("48000"),
                    ConfigValueOption("96000"),
                ],
                advanced=True,
            ),
            ConfigEntry(
                key=CONF_OUTPUT_BIT_DEPTH,
                type=ConfigEntryType.STRING,
                default_value=OUTPUT_AUTO,
                options=[
                    ConfigValueOption(OUTPUT_AUTO),
                    ConfigValueOption("16"),
                    ConfigValueOption("24"),
                ],
                advanced=True,
            ),
            ConfigEntry(
                key=CONF_DEVICE_ID,
                type=ConfigEntryType.STRING,
                hidden=True,
                required=False,
            ),
        )

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

    async def unload(self, is_removed: bool = False) -> None:
        """Handle close/cleanup of the provider."""
        if self._prefetch_task and not self._prefetch_task.done():
            self._prefetch_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._prefetch_task

        if self._ynison:
            await self._ynison.disconnect()

        if self._runner_task and not self._runner_task.done():
            self._runner_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._runner_task

        for callback in self._on_unload_callbacks:
            with suppress(KeyError):
                callback()

    async def get_audio_sources(self) -> list[AudioSource]:
        """Return the AudioSources this plugin currently exposes."""
        return [self._audio_source]

    async def get_stream_details(self, item_id: str, media_type: MediaType) -> StreamDetails:
        """
        Return StreamDetails for streaming the Yandex Music Connect audio.

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
            # Fresh AudioFormat copy per call: MA's ffmpeg mutates
            # input_format.codec_type in place, so a shared instance would
            # propagate that mutation into future stream-details snapshots.
            audio_format=make_pcm_format(self._normalized_params),
            media_type=MediaType.AUDIO_SOURCE,
            stream_type=StreamType.CUSTOM,
            stream_metadata=self._stream_metadata,
        )

    async def on_source_control(
        self,
        source_id: str,
        action: SourceControl,
        value: SourceControlValue = None,
    ) -> None:
        """Proxy playback control commands to Yandex via the linked Yandex Music provider."""
        if source_id != AUDIO_SOURCE_ID:
            return
        if action == SourceControl.PLAY:
            await self._on_play()
        elif action == SourceControl.PAUSE:
            await self._on_pause()
        elif action == SourceControl.NEXT:
            await self._on_next()
        elif action == SourceControl.PREVIOUS:
            await self._on_previous()
        elif (
            action == SourceControl.SEEK
            # tolerate float positions from internal callers; bool is an int
            # subclass, so a misrouted toggle must not become a 1-second seek
            and isinstance(value, (int, float))
            and not isinstance(value, bool)
        ):
            await self._on_seek(int(value))

    async def get_audio_stream(  # noqa: PLR0915
        self, streamdetails: StreamDetails, seek_position: int = 0
    ) -> AsyncGenerator[bytes]:
        """
        Return continuous audio stream following Ynison track changes.

        Streams the current track, then waits for track changes and streams
        the next track automatically. Runs until the source is deselected.

        The PCM format is frozen at session start to match what the outer
        ffmpeg captured from ``self._normalized_format``.  If
        ``_update_normalized_format()`` fires mid-session (e.g. a provider
        reload), the new format takes effect only on the *next* session —
        preventing bit-depth/sample-rate mismatches that cause noise.
        """
        self._stream_stop_event.clear()
        # snapshot the consumer at session start; the rest of this generator
        # treats the queue_id as the player_id (they are the same by convention).
        # The lock may legitimately be empty here — MA's `_load_item` preload
        # path drives the generator to fill an initial audio buffer BEFORE
        # `on_source_selected` has been dispatched, so `_in_use_by_player` is
        # still None on that call. `had_claim` records whether a lock was
        # already in force at entry; only in that case do we enforce
        # cross-session invariants on the loop and the `finally` cleanup.
        player_id = self._in_use_by_player or ""
        had_claim = self._in_use_by_player is not None
        # Snapshot the active session id too so a same-queue reconnect (which
        # updates _active_session_id but not _in_use_by_player) is treated as a
        # superseding session: the loop exits early, and the finally clear
        # below skips the release so it doesn't clobber the new claim.
        captured_session_id = self._active_session_id

        # MA's streams controller may pass a non-zero seek_position (e.g. a
        # resume initiated through a path that does NOT go through Ynison and
        # therefore did not set `_seek_position_ms`). Honor it as the seed for
        # the upcoming track. The Ynison-driven seek path (`_activate_playback`
        # / `_on_seek`) keeps writing `_seek_position_ms` directly, which
        # subsequent track iterations consume — only the seed differs.
        if seek_position > 0 and self._seek_position_ms == 0:
            self._seek_position_ms = seek_position * 1000

        # Freeze format for this streaming session so every inner ffmpeg
        # produces data matching the outer ffmpeg's captured input_format.
        session_params: dict[str, Any] = dict(self._normalized_params)
        session_fmt: AudioFormat = make_pcm_format(session_params)

        try:
            while not self._stream_stop_event.is_set() and (
                # Preload path: no claim was active at entry — drive the
                # loop purely off Ynison state and the stop event.
                not had_claim or not self._session_lost(player_id, captured_session_id)
            ):
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

                # `_pause_playback` set the stop event; finalize.
                if self._ynison.state.is_paused:
                    return

                if not self._yandex_provider:
                    self.logger.warning(
                        "No linked Yandex Music provider — cannot stream track %s", track_id
                    )
                    self._stream_stop_event.set()
                    if self._in_use_by_player == player_id:
                        await self.mass.players.cmd_stop(player_id)
                    return

                # Stream the current track
                seek_ms = self._seek_position_ms
                self._seek_position_ms = 0
                bytes_yielded = 0
                self._streaming_progress_ms = seek_ms
                last_progress_sync = time.monotonic()

                track_fmt = make_pcm_format(session_params)
                track_stream = self._stream_track(
                    track_id, seek_ms=seek_ms, session_params=session_params
                )
                # aclosing: breaking out below must finalize the generator right away,
                # otherwise the linked provider's stream slot stays charged until GC.
                async with aclosing(track_stream):
                    async for chunk in track_stream:
                        yield chunk
                        bytes_yielded += len(chunk)
                        now_mono = time.monotonic()
                        if now_mono - last_progress_sync >= _PROGRESS_SYNC_INTERVAL:
                            last_progress_sync = now_mono
                            await self._sync_progress(
                                seek_ms, bytes_yielded, player_id, session_fmt
                            )
                        if (
                            self._track_changed_event.is_set()
                            or self._stream_stop_event.is_set()
                            or (had_claim and self._session_lost(player_id, captured_session_id))
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
                    break

                # Differentiate "track finished naturally" from "inner loop
                # broke out early". Signalling completion on an
                # interrupted track makes Yandex auto-advance the queue —
                # surfaces as an unwanted skip on pause / handoff.
                broke_for_pause = self._ynison is not None and self._ynison.state.is_paused
                broke_for_session_change = had_claim and self._session_lost(
                    player_id, captured_session_id
                )
                natural_end = (
                    not self._track_changed_event.is_set()
                    and not broke_for_pause
                    and not broke_for_session_change
                    and self._ynison is not None
                )
                if natural_end:
                    self.logger.info("Track %s finished, advancing to next", track_id)
                    await self._signal_track_completion()
                    if not await self._wait_for_track_change(track_id):
                        self._stream_stop_event.set()
                        break

                # Clear before next iteration — the new track ID will be set at
                # the top of the loop from the latest Ynison state.
                self._current_streaming_track_id = None
        finally:
            # Release ownership only if THIS generator owned the claim at
            # entry AND no one else has superseded it since. The double-guard
            # protects against a same-queue reconnect refreshing the session
            # id without changing the queue id; clearing the lock on the old
            # generator's teardown would otherwise clobber the new session's
            # claim. `had_claim` keeps the preload path from touching the lock
            # at all (no claim ever existed to release).
            if had_claim and not self._session_lost(player_id, captured_session_id):
                self._in_use_by_player = None
            self._current_streaming_track_id = None

    async def on_source_selected(
        self,
        source_id: str,
        player_id: str,
        owner_player_id: str,
        stream_session_id: str,
    ) -> None:
        """Handle callback when this AudioSource has been selected/started on a player."""
        if source_id != AUDIO_SOURCE_ID or not player_id:
            return

        # Check if manual player switching is allowed
        if not self._allow_player_switch:
            current_target = self._get_target_player_id()
            if player_id != current_target and current_target:
                # Redirect to the configured target, but only once per
                # idempotency window. The target may be a sendspin bridge /
                # sync-group whose stream is consumed under a player id that
                # never equals `current_target`, so each redirect re-triggers
                # selection here. Re-issuing `play_media` on every rejection
                # turns that into an unbounded AudioError storm; the raise
                # below still aborts every wrong-player stream regardless.
                if self._idempotent("source_redirect", current_target):
                    self.logger.debug(
                        "Player switching disabled, redirecting selection from %s to %s",
                        player_id,
                        current_target,
                    )
                    await self.mass.player_queues.play_media(
                        current_target, str(self._audio_source.uri)
                    )
                msg = f"Player switching is disabled; source must remain on {current_target}"
                raise RuntimeError(msg)

        # Stop previous player if switching. The lock claim a few lines below
        # replaces the previous queue's claim; the previous stream loop notices
        # the queue change and exits cleanly.
        if self._active_player_id and self._active_player_id != player_id:
            prev_player_id = self._active_player_id
            self.logger.info(
                "Source selected on %s, stopping %s",
                player_id,
                prev_player_id,
            )
            try:
                await self.mass.players.cmd_stop(prev_player_id)
            except Exception as err:
                self.logger.debug(
                    "Failed to stop previous player %s: %s",
                    prev_player_id,
                    err,
                )

        # Claim ownership for this queue. The lock lives here (not in
        # get_stream_details) so preload paths can fetch streamdetails without
        # accidentally blocking a subsequent cross-queue handoff at the actual
        # stream request.
        self._in_use_by_player = owner_player_id
        # Record this request's session id so a later on_source_unselected can
        # tell whether it is the live teardown or a stale callback from a
        # superseded same-queue request.
        self._active_session_id = stream_session_id
        self._active_player_id = player_id
        self.logger.debug("Active player set to: %s", player_id)

    async def on_source_unselected(
        self, source_id: str, owner_player_id: str, stream_session_id: str
    ) -> None:
        """Release the queue-scoped exclusive claim when MA tears down the stream."""
        if source_id != AUDIO_SOURCE_ID:
            return
        # Reject stale callbacks: only release if this is still the active
        # session. A owner_player_id check alone is not sufficient — same-queue
        # reconnects (player drops + reopens the same stream URL before the
        # original request's finally fires) would otherwise let the old
        # request's late callback clear the live claim of the new stream.
        if self._active_session_id != stream_session_id:
            return
        self._active_session_id = None
        if self._in_use_by_player == owner_player_id:
            self._in_use_by_player = None

    async def _wait_for_track_change(self, old_track_id: str, timeout: float = 30.0) -> bool:
        """
        Wait for Ynison to report a different track, ignoring echoes.

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
    ) -> AsyncGenerator[bytes]:
        """
        Stream a single track, normalizing to fixed PCM via per-track ffmpeg.

        Every track is decoded through its own ffmpeg process to produce a
        fixed PCM output (s16le or s24le based on YM quality setting). This
        ensures MA's single ffmpeg process never encounters mid-stream format
        changes (codec, bit depth, sample rate).

        *session_params* — frozen format dict from the enclosing
        ``get_audio_stream()`` session.  Falls back to the current
        ``_normalized_params`` when called outside a session.
        """
        provider = self._yandex_provider
        if provider is None:
            self.logger.warning(
                "Linked Yandex Music provider unavailable — stopping track %s",
                track_id,
            )
            self._stream_stop_event.set()
            return
        # In-flight stream fetch outranks unrelated 429 cooldowns:
        # dropping a stream the user is actively trying to play is
        # worse than risking another captcha. Prefetch deliberately
        # stays throttled (see `_prefetch_format_for_track`).
        bypass_token = BYPASS_THROTTLER.set(True)
        try:
            stream_details = await self._get_stream_details_with_retry(track_id, provider=provider)
        except Exception:
            self.logger.exception("Failed to get stream details for track %s", track_id)
            self._stream_stop_event.set()
            return
        finally:
            BYPASS_THROTTLER.reset(bypass_token)

        if not self._linked_provider_is_current(provider):
            self.logger.warning(
                "Linked Yandex Music provider changed mid-stream — stopping track %s",
                track_id,
            )
            self._stream_stop_event.set()
            return

        await self._update_metadata_from_stream(stream_details, seek_ms)
        if not self._linked_provider_is_current(provider):
            self.logger.warning(
                "Linked Yandex Music provider changed while preparing track %s",
                track_id,
            )
            self._stream_stop_event.set()
            return

        # No -re here: MA's realtime pacer is the single pacing authority for
        # AudioSources. Pacing the decode a second time would pin it to realtime
        # and forbid the small read-ahead that absorbs CDN jitter; back-pressure
        # through the generator chain still bounds memory.
        extra_input_args = list(PROBE_ARGS)
        if seek_ms > 0:
            extra_input_args += ["-ss", f"{seek_ms / 1000.0:.3f}"]

        # Use session format when available, otherwise current normalized params
        params = session_params if session_params is not None else self._normalized_params
        out_fmt = make_pcm_format(params)
        # Log the output rate + bit depth alongside the source format: with the
        # passthrough fast path this PCM IS the delivered audio, so the line must
        # let an operator read rate passthrough vs a resample, not just codec.
        self.logger.info(
            "Streaming track %s → %s/%dHz/%dbit: input=%s seek=%dms",
            track_id,
            out_fmt.content_type.value,
            out_fmt.sample_rate,
            out_fmt.bit_depth,
            stream_details.audio_format,
            seek_ms,
        )
        async with provider.acquire_stream_slot(STREAM_SLOT_PLAYBACK_WAIT_TIMEOUT):
            if not self._linked_provider_is_current(provider):
                self.logger.warning(
                    "Linked Yandex Music provider changed before starting track %s",
                    track_id,
                )
                self._stream_stop_event.set()
                return
            raw_stream = provider.get_audio_stream(stream_details)
            ffmpeg_stream = get_ffmpeg_stream(
                audio_input=raw_stream,
                input_format=stream_details.audio_format,
                output_format=out_fmt,
                extra_input_args=extra_input_args,
            )
            async with aclosing(raw_stream), aclosing(ffmpeg_stream):
                async for chunk in ffmpeg_stream:
                    if not self._linked_provider_is_current(provider):
                        self.logger.warning(
                            "Linked Yandex Music provider changed while streaming track %s",
                            track_id,
                        )
                        self._stream_stop_event.set()
                        break
                    yield chunk

    async def _get_stream_details_with_retry(
        self,
        track_id: str,
        media_type: MediaType = MediaType.TRACK,
        *,
        provider: YandexMusicProviderLike | None = None,
    ) -> StreamDetails:
        """Fetch stream details with caching, throttling, and retry."""
        # Capture the linked yandex_music provider into a local ref at entry.
        # self._yandex_provider can flip to None mid-await when the linked
        # MusicProvider is unloaded (see _check_yandex_provider_match, which
        # runs as a background task on provider-loaded/unloaded events).
        # Dereferencing the attribute after an await would raise
        # AttributeError and hard-stop the audio generator.
        provider = provider or self._yandex_provider
        if provider is None:
            raise LoginFailed(
                "Linked Yandex Music provider is not loaded — cannot fetch stream details"
            )

        cache_key = self._stream_details_cache_key(provider.instance_id, track_id)
        cached = await self.mass.cache.get(
            cache_key,
            provider=self.instance_id,
            base_class=StreamDetails,
        )
        if cached is not None:
            cached_streamdetails = cast("StreamDetails", cached)
            if cached_streamdetails.provider == provider.instance_id:
                self.logger.debug("Stream details cache hit for %s", track_id)
                return cached_streamdetails
            await self.mass.cache.delete(cache_key, provider=self.instance_id)
            self.logger.warning(
                "Discarded stream details for %s owned by %s instead of %s",
                track_id,
                cached_streamdetails.provider,
                provider.instance_id,
            )

        backoff = _API_INITIAL_BACKOFF
        last_err: Exception | None = None
        for attempt in range(_API_MAX_RETRIES):
            async with self._api_throttler.acquire() as delay:
                if delay > 0:
                    self.logger.debug("get_stream_details throttled %.1fs", delay)
            try:
                sd = await provider.get_stream_details(track_id, media_type)
                if sd.provider != provider.instance_id:
                    raise _StreamOwnerMismatchError(
                        f"Stream details for {track_id} belong to {sd.provider}, "
                        f"expected {provider.instance_id}"
                    )
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
            except _StreamOwnerMismatchError:
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

    async def _invalidate_stream_cache(
        self, track_id: str, provider_instance_id: str | None = None
    ) -> None:
        """
        Evict cached stream details for a track so the next fetch is fresh.

        :param track_id: Track whose cached stream details should be dropped.
        :param provider_instance_id: Linked provider instance that owns the entry,
            defaulting to the currently linked one.
        """
        if provider_instance_id is None:
            if self._yandex_provider is None:
                return
            provider_instance_id = self._yandex_provider.instance_id
        cache_key = self._stream_details_cache_key(provider_instance_id, track_id)
        await self.mass.cache.delete(cache_key, provider=self.instance_id)
        self.logger.debug("Invalidated stream cache for %s", track_id)

    @staticmethod
    def _stream_details_cache_key(provider_instance_id: str, track_id: str) -> str:
        """Return the cache key for one linked provider instance and track."""
        return f"ynison_sd_{provider_instance_id}_{track_id}"

    def _linked_provider_is_current(self, provider: YandexMusicProviderLike) -> bool:
        """Return whether the captured linked provider still owns streaming."""
        return self._yandex_provider is provider and provider.available

    # ------------------------------------------------------------------
    # Token handling
    # ------------------------------------------------------------------

    async def _refresh_via_x_token(self, x_token: str) -> SecretStr:
        """
        Refresh the music token from an x_token, caching the result.

        Within :data:`_MUSIC_TOKEN_TTL_S` of a successful refresh, subsequent
        calls for the same x_token return the cached :class:`SecretStr`
        without hitting Yandex Passport. Concurrent callers coalesce via
        :attr:`_token_refresh_lock`.

        :param x_token: Long-lived session token to exchange for a music
            token. Hashed before use as a cache key; the raw value is
            never stored in dict keys or logs.
        :returns: Fresh or cached music-scoped :class:`SecretStr`.
        :raises LoginFailed: When Yandex explicitly rejects the x_token
            (propagated from :func:`provider.auth.refresh_music_token`).
        :raises ResourceTemporarilyUnavailable: On transient Passport
            failures (network, rate limit) — retry later, credentials
            are still good.
        """
        cache_key = _hash_x_token(x_token)
        cached = self._token_cache.get(cache_key)
        now = self._now()
        if cached is not None and cached.expires_monotonic > now:
            return cached.token

        async with self._token_refresh_lock:
            # Double-check inside the lock — a peer caller may have refreshed
            # while we were waiting for the lock, in which case we reuse
            # their fresh entry instead of issuing a duplicate Passport call.
            cached = self._token_cache.get(cache_key)
            now = self._now()
            if cached is not None and cached.expires_monotonic > now:
                return cached.token

            token = await refresh_music_token(SecretStr(x_token))
            self._store_cached_token(cache_key, token)
            return token

    def _store_cached_token(self, cache_key: str, token: SecretStr) -> None:
        """
        Insert a cache entry, enforcing the LRU bound.

        Refreshing an existing key bumps its position to most-recent. When
        a new key would push the cache over :data:`_MUSIC_TOKEN_CACHE_MAX`,
        the oldest entry is evicted first.
        """
        # Reordering: pop-then-set positions the (possibly-new) key as
        # most-recent in Python's insertion-ordered dict.
        self._token_cache.pop(cache_key, None)
        while len(self._token_cache) >= _MUSIC_TOKEN_CACHE_MAX:
            oldest = next(iter(self._token_cache))
            self._token_cache.pop(oldest)
        self._token_cache[cache_key] = _CachedToken(
            token=token,
            expires_monotonic=self._now() + _MUSIC_TOKEN_TTL_S,
        )

    def _invalidate_cached_token(self, x_token: str) -> None:
        """Drop the cache entry for an x_token (e.g. after a 401)."""
        self._token_cache.pop(_hash_x_token(x_token), None)

    async def _resolve_token(self) -> SecretStr:
        """
        Resolve the Yandex Music OAuth token for the Ynison connection.

        In borrow mode: read from the linked yandex_music provider's config.
        If only x_token is present (YM hasn't refreshed yet), do a cached
        in-memory refresh without writing back — YM owns token persistence.

        In own mode: return CONF_TOKEN if set; otherwise, when CONF_X_TOKEN
        is present (QR-with-Remember-session path), cached in-memory refresh.
        """
        if self._borrow_source is not None:
            return await self._borrow_source.resolve_music_token()

        token = cast("str | None", self.get_setup_value(CONF_TOKEN))
        if token:
            return SecretStr(token)
        x_token = cast("str | None", self.get_setup_value(CONF_X_TOKEN))
        if x_token:
            self.logger.debug("Own-mode token not present — refreshing from stored x_token")
            return await self._refresh_via_x_token(x_token)
        raise LoginFailed("No Yandex Music token configured")

    async def _refresh_ynison_token(self) -> SecretStr:
        """
        Refresh the OAuth token for Ynison reconnection.

        Called by YnisonClient on auth failure (401/403) during reconnect.

        In borrow mode: re-read the linked YM instance's x_token and refresh
        in-memory only (no config writes — YM owns token persistence).

        In own mode: refresh from stored CONF_X_TOKEN when present (QR with
        "Remember session" enabled). When absent (manual token paste only),
        surface LoginFailed so the user knows to paste a new token.

        The cached token entry for the current x_token is invalidated up
        front — this method is reached only on a server-rejected token, so
        the cached value is provably stale.
        """
        if self._borrow_source is not None:
            ym_music_token, ym_x_token = self._borrow_source.read_tokens()
            if ym_x_token is None:
                raise LoginFailed("Cannot refresh: linked Yandex Music instance has no x_token")
            # Both the minted entry AND the owner's persisted token may be the
            # value the server just rejected — invalidate both so the source
            # can't re-serve either; it will mint fresh from x_token.
            if ym_music_token is not None:
                self._borrow_source.invalidate(ym_music_token)
            self._borrow_source.invalidate(ym_x_token)
            self.logger.info("Refreshing Yandex Music token for Ynison reconnect (borrow mode)")
            return await self._borrow_source.resolve_music_token()

        x_token = cast("str | None", self.get_setup_value(CONF_X_TOKEN))
        if x_token:
            self._invalidate_cached_token(x_token)
            self.logger.info("Refreshing Yandex Music token for Ynison reconnect (own mode)")
            return await self._refresh_via_x_token(x_token)

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

        # Post-reconnect settle window: the first inbound state after a WS
        # reconnect may reflect pre-reconnect peer state (active device etc).
        # Acting on it would re-issue play_media, mirror a stale paused flag
        # to MA, or worst case clobber a fresh local claim. The 2 s window in
        # YnisonClient._connect_state gives the server time to emit a state
        # broadcast that reflects our re-registered presence; until then we
        # only log.
        if self._ynison and self._ynison.in_post_reconnect_settle:
            self.logger.debug(
                "Skipping state inside post-reconnect settle window (track=%s paused=%s)",
                track_id,
                state.is_paused,
            )
            return

        if is_our_device and not state.is_paused:
            self.logger.info(
                "Ynison → playing (track=%s progress=%dms)", track_id, state.progress_ms
            )
            # Pre-fetch next batch when playing second-to-last track
            self._maybe_prefetch(current_index, playable_list, entity_id, entity_type)
            await self._activate_playback(state)
        elif is_our_device and state.is_paused:
            self.logger.info(
                "Ynison → paused (track=%s progress=%dms)", track_id, state.progress_ms
            )
            await self._pause_playback()
        elif self._in_use_by_player:
            self.logger.info(
                "Ynison → other device active (was=%s), clearing",
                state.active_device_id,
            )
            self._clear_active_player()

    async def _activate_playback(self, state: YnisonState) -> None:  # noqa: PLR0915
        """Activate playback on the target MA player."""
        target_player_id = self._get_target_player_id()
        if not target_player_id:
            self.logger.warning("Ynison active on our device but no MA player available")
            return

        # Resume after pause / fresh start: either signal triggers
        # play_media below. `_externally_paused` survives a stray stop-event
        # clear; the stop event covers non-pause stop reasons
        # (`_stream_track` warning branch, `_clear_active_player`).
        needs_reselect = self._stream_stop_event.is_set() or self._externally_paused
        self._stream_stop_event.clear()
        self._externally_paused = False

        # Start playback via the standard play_media flow if not already active.
        # Guard on _active_player_id (set immediately) rather than in_use_by_queue
        # (set by get_stream_details when the streams controller picks up the request)
        # to prevent queuing redundant play_media calls during the ~5s gap.
        if self._active_player_id != target_player_id or needs_reselect:
            # Pre-fetch the upcoming track's real format BEFORE submitting
            # play_media so the AudioSource's provider_mapping carries the
            # right audio_format when the streams controller calls
            # get_stream_details(). Skip on same-track same-player resume —
            # the cached format is still correct for that case.
            upcoming = state.current_track_id
            switching_player = self._active_player_id != target_player_id
            self._active_player_id = target_player_id
            if upcoming and (switching_player or upcoming != self._current_streaming_track_id):
                await self._prefetch_format_for_track(upcoming)
            self.mass.create_task(
                self.mass.player_queues.play_media(target_player_id, str(self._audio_source.uri))
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
                            drift_ms = abs(state.progress_ms - our_ms)
                            self.logger.info(
                                "Seek detected on track %s: "
                                "expected ~%dms, Ynison at %dms (drift %dms)",
                                new_track,
                                our_ms,
                                state.progress_ms,
                                int(drift_ms),
                            )
                            self._seek_position_ms = state.progress_ms
                            self._track_changed_event.set()
                            self._seek_grace_until = now + _ECHO_GRACE_PERIOD
                            significant_change = True
                        elif verdict == "queue_rebuild":
                            self.logger.debug(
                                "Drift on track %s classified as queue-rebuild "
                                "echo (Ynison=%dms, ours=%dms) — not seeking",
                                new_track,
                                state.progress_ms,
                                our_ms,
                            )

        # Update metadata from state
        self._update_metadata(state)

        # Always trigger player update on significant changes;
        # throttle regular updates to avoid UI churn (every 5 seconds).
        # Use force_update on seek/track change so the server broadcasts a full
        # PLAYER_UPDATED event instead of a lightweight elapsed-time-only one
        # that the frontend may not handle for AudioSource players.
        now_mono = time.monotonic()
        if significant_change or needs_reselect or now_mono - self._last_player_update_time >= 5.0:
            self.mass.players.trigger_player_update(
                target_player_id, force_update=significant_change
            )
            self._last_player_update_time = now_mono

    def _update_metadata(self, state: YnisonState) -> None:
        """Update AudioSource metadata from Ynison state."""
        meta = self._stream_metadata

        # Update duration (prefer actual from stream_details) and elapsed time
        best_duration = self._best_duration_ms()
        if best_duration:
            meta.duration = best_duration // 1000
        # Only update elapsed from Ynison when NOT actively streaming —
        # during streaming, _sync_progress provides byte-accurate progress.
        if state.progress_ms is not None and not self._in_use_by_player:
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
        """Update AudioSource metadata from stream details (authoritative for duration)."""
        meta = self._stream_metadata
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
        # `trigger_player_update` expects a player_id; `_in_use_by_player` is
        # a queue identifier which only happens to coincide with player_id
        # when there is no protocol bridge. Use `_active_player_id` — the
        # real player wrapping our stream (bridge if any).
        if self._active_player_id:
            self.mass.players.trigger_player_update(self._active_player_id, force_update=True)

    async def _send_progress_to_ynison(
        self,
        progress_ms: int,
        duration_ms: int,
        paused: bool,
        *,
        strict: bool = False,
    ) -> None:
        """
        Send progress to Ynison.

        Progress is clamped to duration because Ynison rejects updates where
        progress > duration (error 400030001) and disconnects the WebSocket.
        The byte counter can slightly overshoot duration at end-of-stream.

        Echo detection is done upstream via YnisonState.last_update_is_echo,
        which is set when Ynison rebroadcasts an update we authored.

        :param progress_ms: Current playback position in milliseconds.
        :param duration_ms: Current track duration in milliseconds.
        :param paused: Whether playback is paused.
        :param strict: When ``True``, propagate transport failures as
            :class:`provider.ynison_client.YnisonSendError`. Used by user-command
            and end-of-track callers. Heartbeat callers leave the default.
        """
        if duration_ms <= 0:
            # Ynison rejects progress > duration; skip until duration is known.
            return
        if not self._ynison or not self._ynison.connected:
            if strict:
                raise YnisonSendError("Ynison not connected")
            return
        progress_ms = min(progress_ms, duration_ms)
        await self._ynison.update_playing_status(
            progress_ms=progress_ms,
            duration_ms=duration_ms,
            paused=paused,
            strict=strict,
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
        meta = self._stream_metadata
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
        """
        Release the active player on external pause.

        ``cmd_stop`` is the only mechanism that flips ``PlaybackState``
        to IDLE for an AudioSource queue item; ``cmd_pause`` and
        ``queue.pause`` both short-circuit back to ``on_source_control``
        and leave MA's state untouched. Pattern matches upstream
        ``AriaCastReceiver._handle_playback_state_update``. Resume
        re-runs ``play_media`` (preload + ffmpeg startup) so it costs
        a few seconds — the alternative kept resume instant but left
        MA's UI stuck on PLAYING.
        """
        target = self._in_use_by_player
        if not target:
            self.logger.info("Pause requested but no active queue (_in_use_by_player is None)")
            return
        self.logger.info("Pause: cmd_stop(%s)", target)
        # stop event ends the audio generator; finally clears the lock.
        self._stream_stop_event.set()
        try:
            await self.mass.players.cmd_stop(target)
        except Exception:
            # cmd_stop is the only mechanism that flips MA's PlaybackState
            # to IDLE for an AudioSource. A silent failure here resurrects
            # the very UX bug this code path exists to fix.
            self.logger.warning(
                "cmd_stop(%s) failed during external pause — MA UI may stay PLAYING",
                target,
                exc_info=True,
            )
            return
        # Demote `_active_player_id` from the bridge MA streams to
        # (e.g. `spb_*`) back to the queue id; queues live on the bare
        # UUID. Without this, resume's `play_media(_active_player_id,
        # …)` would target the bridge and raise
        # `PlayerUnavailableError`. Post-success only so a failure
        # path keeps the bridge id intact for the next attempt.
        self._active_player_id = target
        self._externally_paused = True

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

    def _session_lost(self, player_id: str, session_id: str | None) -> bool:
        """
        Return ``True`` when our claim no longer matches the live session.

        :param player_id: Queue id captured at generator entry.
        :param session_id: ``_active_session_id`` captured at generator entry.
        """
        return self._in_use_by_player != player_id or self._active_session_id != session_id

    def _idempotent(self, action: str, key: str | None) -> bool:
        """
        Return ``True`` if ``(action, key)`` was not seen within the TTL window.

        :param action: A short string identifying the command kind.
        :param key: Sub-key inside the action namespace, or ``None``.
        """
        now = time.monotonic()
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
    ) -> Literal["ignore", "queue_rebuild", "seek"]:
        """
        Classify drift between Ynison-reported and our local position.

        Returns one of:

        - ``"ignore"`` — drift at or below ``threshold_ms``; no seek needed.
        - ``"queue_rebuild"`` — Ynison reports near-zero progress while we
          are past 5s into the track; treat as a RADIO queue-rebuild echo,
          not a user seek (otherwise we'd yank playback to the start every
          time the rotor station refills the queue).
        - ``"seek"`` — genuine drift; honor it.

        :param ynison_ms: Position reported by Ynison in milliseconds.
        :param our_ms: Position tracked locally in milliseconds.
        :param threshold_ms: Minimum drift to consider non-ignorable.
        """
        drift = abs(ynison_ms - our_ms)
        if drift <= threshold_ms:
            return "ignore"
        if ynison_ms < 1000 and our_ms > 5000:
            return "queue_rebuild"
        return "seek"

    async def _prefetch_format_for_track(self, track_id: str) -> None:
        """
        Pre-fetch stream details for *track_id* and adapt PCM format.

        Best-effort: bounded by ``_PREFETCH_FORMAT_TIMEOUT`` so a slow Yandex
        API does not stall ``_activate_playback``. On timeout / error the
        current format stays in place and the in-stream
        ``_get_stream_details_with_retry`` handles retries.

        :param track_id: Yandex Music track id to query.
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

    def _clear_active_player(self) -> None:
        """Clear the active player and reset plugin state."""
        prev_player_id = self._active_player_id
        # the owner is the user-facing MA player; _active_player_id can be the protocol
        # player that consumed the stream, which is not what holds the source session
        owner_player_id = self._in_use_by_player
        self._active_player_id = None
        self._in_use_by_player = None
        self._active_session_id = None
        self._stream_stop_event.set()
        self._streaming_progress_ms = 0
        self._prefetched_list = None
        self._command_idempotency.clear()
        self._externally_paused = False
        if self._prefetch_task and not self._prefetch_task.done():
            self._prefetch_task.cancel()

        if prev_player_id:
            self.logger.debug(
                "Playback ended on player %s, clearing active player",
                prev_player_id,
            )
            if owner_player_id:
                # give the source back as well as stopping: a session left on the player
                # keeps it publishing this source, so its own queue stays unreachable
                self.mass.create_task(
                    self.mass.players.deselect_source(
                        owner_player_id,
                        provider_instance_id=self.instance_id,
                        source_id=AUDIO_SOURCE_ID,
                    )
                )
            self.mass.players.trigger_player_update(prev_player_id)

    # ------------------------------------------------------------------
    # Yandex Music provider matching
    # ------------------------------------------------------------------

    def _on_provider_event(self, event: MassEvent) -> None:
        """Handle provider added/removed events."""
        self.mass.create_task(self._check_yandex_provider_match())

    async def _check_yandex_provider_match(self) -> None:
        """
        Check if a Yandex Music provider is available for audio streaming.

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

    def _snap_rate_to_player(self, rate: int) -> int:
        """
        Snap *rate* down to the nearest sample rate the target player accepts.

        Best-effort: returns *rate* unchanged when no target player or
        supported-rate set can be resolved, and never raises.

        :param rate: The sample rate the hint / floor logic chose.
        :return: A rate the target player can play (``rate`` itself when it is
            already supported or no player is resolvable).
        """
        # Mirror MA's _select_audio_source_pcm_format so the declared format
        # equals what the AudioSource passthrough picks — keeping MA off its
        # second resampling ffmpeg.
        try:
            player_id = self._get_target_player_id()
            if not player_id:
                return rate
            player = self.mass.players.get_player(player_id)
            if player is None:
                return rate
            supported = [sr for sr, _ in player.get_supported_sample_rates()]
            if not supported or rate in supported:
                return rate
            return max((r for r in supported if r <= rate), default=min(supported))
        except Exception:
            self.logger.debug(
                "Could not snap sample rate to player capabilities; keeping %d Hz",
                rate,
                exc_info=True,
            )
            return rate

    def _update_normalized_format(self, hint: AudioFormat | None = None) -> None:
        """
        Set PCM normalization profile based on config and YM quality.

        Priority: explicit config values > hint from real stream_details >
        auto-detection from YM quality. The hint is fed by
        ``_prefetch_format_for_track`` when ``CONF_OUTPUT_SAMPLE_RATE`` is
        ``auto`` so the AudioSource ``provider_mapping.audio_format`` matches
        the actual source rate of the upcoming track. Without a hint, falls
        back to YM-quality-based detection (superb/lossless → 24bit/44.1kHz,
        else → 16bit/44.1kHz). The resulting auto/hint rate is then snapped
        down to the nearest rate the target player supports; a valid explicit
        override is delivered verbatim and never snapped.

        Creates fresh AudioFormat instances each time to prevent mutation by
        MA's FFMpeg._log_reader_task (which sets input_format.codec_type
        in-place on the object passed as input_format to the outer ffmpeg).

        :param hint: Optional real source AudioFormat (from a stream-details
            pre-fetch). Lifts auto mode from the quality-based default to the
            track's actual sample rate and bit depth.
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
        base = dict(PCM_LOSSLESS_PARAMS if is_lossless else PCM_LOSSY_PARAMS)
        # Promote auto-base from the real stream details when available.
        # Validate the hint against the same allow-lists we use for explicit
        # config overrides — a Yandex API hiccup that returns an unsupported
        # rate (or 0) must not poison the AudioSource provider_mapping or the
        # outer ffmpeg input_format.
        if hint is not None:
            if hint.sample_rate and str(hint.sample_rate) in _VALID_SAMPLE_RATES:
                base["sample_rate"] = hint.sample_rate
            if hint.bit_depth and str(hint.bit_depth) in _VALID_BIT_DEPTHS:
                base["bit_depth"] = hint.bit_depth

        # Apply config overrides. MA's ConfigEntry options constrain the UI to
        # known-good strings, but a stale persisted value or hand-edited config
        # could still surface something unparsable or off-list — fall back to
        # the auto-detected base with a warning instead of crashing the load.
        sample_rate = base["sample_rate"]
        bit_depth = base["bit_depth"]
        explicit_rate = False
        if self._cfg_sample_rate != OUTPUT_AUTO:
            if self._cfg_sample_rate in _VALID_SAMPLE_RATES:
                sample_rate = int(self._cfg_sample_rate)
                explicit_rate = True
            else:
                self.logger.warning(
                    "Invalid %s=%r; falling back to auto-detected %d Hz",
                    CONF_OUTPUT_SAMPLE_RATE,
                    self._cfg_sample_rate,
                    sample_rate,
                )
        # Snap the auto / hint / floor rate to a value the target player accepts
        # so the declared format matches what MA's AudioSource passthrough picks
        # and no second resampling ffmpeg is spawned. A valid explicit override
        # is delivered verbatim and is never snapped.
        if not explicit_rate:
            sample_rate = self._snap_rate_to_player(sample_rate)
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
        if self._in_use_by_player and (
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
        # rebuild the AudioSource so its ProviderMapping carries the new audio_format
        self._audio_source = self._build_audio_source()
        self.logger.debug(
            "Normalization format: %s/%dHz/%dbit",
            self._normalized_format.content_type.value,
            self._normalized_format.sample_rate,
            self._normalized_format.bit_depth,
        )

    def _update_source_capabilities(self) -> None:
        """Rebuild AudioSource so capability flags reflect linked provider availability."""
        self._audio_source = self._build_audio_source()
        # The session publishes the controls from the object it holds, so hand it the
        # rebuilt one: the new capability flags reach the UI without waiting for the
        # source to be selected again.
        if not self._in_use_by_player:
            return
        self.mass.players.refresh_source(self._in_use_by_player, self._audio_source)

    def _build_audio_source(self) -> AudioSource:
        """Construct the AudioSource MediaItem with current capability flags."""
        has_provider = self._yandex_provider is not None
        return AudioSource(
            item_id=AUDIO_SOURCE_ID,
            provider=self.instance_id,
            name=self.name,
            provider_mappings={
                ProviderMapping(
                    item_id=AUDIO_SOURCE_ID,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                    # Fresh AudioFormat copy — `self._normalized_format` is a
                    # shared mutable that MA's ffmpeg sets `codec_type` on
                    # in-place. Sharing it would let that mutation leak into
                    # the rebuilt AudioSource and any future stream-details.
                    audio_format=make_pcm_format(self._normalized_params),
                )
            },
            can_play_pause=has_provider,
            can_seek=has_provider,
            can_next_previous=has_provider,
            exclusive=True,
            allow_external_trigger=True,
        )

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

    def _require_connected_ynison(self) -> YnisonClient:
        """
        Return the live Ynison client or raise an MA player-control error.

        :raises UnsupportedFeaturedException: When the provider's Ynison
            client has not been initialised yet (pre-`handle_async_init`
            or post-`unload`).
        :raises PlayerCommandFailed: When the Ynison WebSocket is currently
            disconnected (e.g. mid-reconnect after a transient network
            error). Surface to MA so the UI shows a clear failure toast
            instead of accepting the command and stalling.
        """
        if not self._ynison:
            raise UnsupportedFeaturedException("Ynison client not initialized")
        if not self._ynison.connected:
            raise PlayerCommandFailed("Ynison WebSocket disconnected")
        return self._ynison

    async def _on_play(self) -> None:
        """Handle play command — send resume to Ynison."""
        client = self._require_connected_ynison()
        if not self._idempotent("on_play", None):
            return
        state = client.state
        try:
            await self._send_progress_to_ynison(
                progress_ms=state.progress_ms,
                duration_ms=self._best_duration_ms(),
                paused=False,
                strict=True,
            )
        except YnisonSendError as exc:
            raise PlayerCommandFailed("Ynison send failed") from exc

    async def _on_pause(self) -> None:
        """Handle pause command — send pause to Ynison."""
        client = self._require_connected_ynison()
        if not self._idempotent("on_pause", None):
            return
        state = client.state
        try:
            await self._send_progress_to_ynison(
                progress_ms=state.progress_ms,
                duration_ms=self._best_duration_ms(),
                paused=True,
                strict=True,
            )
        except YnisonSendError as exc:
            raise PlayerCommandFailed("Ynison send failed") from exc

    # Entity types that use server-side "radio" queue replenishment.
    # Currently only RADIO (personal wave, genre stations).
    # Add "WAVE" here if/when Yandex supports it via the same
    # rotor_station_tracks API.
    _RADIO_ENTITY_TYPES: ClassVar[set[str]] = {"RADIO"}

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
        """
        Signal that the current track finished playing.

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
        # `strict=True`: a dropped end-of-track signal stalls the YM app on
        # the just-finished track. We log and continue — the reconnect is
        # already scheduled and the queue-advance below sees the same WS state
        # — but we don't reraise (this is end-of-stream, there's no command to
        # fail back to the user).
        try:
            await self._send_progress_to_ynison(
                progress_ms=duration, duration_ms=duration, paused=False, strict=True
            )
        except YnisonSendError:
            self.logger.warning(
                "Track-completion signal dropped (Ynison transport failure); "
                "queue advance will retry once the WS reconnects",
                exc_info=True,
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
        """
        Fetch more tracks from Yandex Music API and return expanded playable_list.

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
        """
        Send update_player_state to advance the queue to next_index.

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
        # `strict=True`: a dropped queue-advance leaves `_wait_for_track_change`
        # spinning for its full 30 s timeout. Log and return — the next
        # reconnect-broadcast picks up our authored version block and resyncs.
        try:
            await self._ynison.update_player_state(player_state=new_state, strict=True)
        except YnisonSendError:
            self.logger.warning(
                "Queue-advance dropped (Ynison transport failure); "
                "stream will stall until reconnect-broadcast resyncs",
                exc_info=True,
            )

    async def _update_queue_list(self, expanded_list: list[dict[str, Any]]) -> None:
        """
        Push an expanded playable_list to Ynison without changing index or progress.

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
        self._require_connected_ynison()
        await self._signal_track_completion()

    async def _on_previous(self) -> None:
        """Handle previous track command — update queue index in Ynison."""
        client = self._require_connected_ynison()
        queue = client.state.player_state.get("player_queue", {})
        current_index = queue.get("current_playable_index", 0)
        if current_index > 0:
            self._actual_duration_ms = 0
            await self._advance_queue_index(current_index - 1)

    async def _on_seek(self, position: int) -> None:
        """
        Handle seek command — send position update to Ynison.

        :param position: Position in seconds from Music Assistant.
        """
        client = self._require_connected_ynison()
        seek_ms = position * 1000
        state = client.state
        try:
            await self._send_progress_to_ynison(
                progress_ms=seek_ms,
                duration_ms=self._best_duration_ms(),
                paused=state.is_paused,
                strict=True,
            )
        except YnisonSendError as exc:
            # Do not mutate `_seek_position_ms` / `_seek_grace_until` on failure
            # — local stream state must not drift past a send that never landed.
            raise PlayerCommandFailed("Ynison send failed") from exc
        # Also trigger local stream restart so seek takes effect
        # immediately without waiting for the Ynison echo.
        self._seek_position_ms = seek_ms
        self._seek_grace_until = time.monotonic() + _ECHO_GRACE_PERIOD
        self._track_changed_event.set()
