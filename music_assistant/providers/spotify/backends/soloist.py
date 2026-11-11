"""
Spotify Soloist playback backend for the Spotify music provider.

Plays a queue through one run of ``soloist``, Spotify's official headless
client, per playback session. A run renders into a private PulseAudio capture
sink whose FIFO is read back slightly above realtime pace and handed to Music
Assistant as one stream per item: the item that follows is queued on the engine
ahead of time, and the capture is cut where the engine moves on to it.

SECURITY NOTE: the daemon takes the user's personal API key on its command
line; nothing in this module may ever log the process argv.

Shared infrastructure (binary manager, WebSocket client, audio prefs, pulse
capture) is owned by the Spotify Connect provider / core helpers and reused here.
"""

from __future__ import annotations

import array
import asyncio
import fcntl
import os
import shutil
import termios
from contextlib import suppress
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Final, NoReturn

from music_assistant_models.enums import ContentType, MediaType, PlaybackState
from music_assistant_models.errors import AudioError, LoginFailed, MusicAssistantError
from music_assistant_models.media_items import AudioFormat

from music_assistant.constants import (
    CONF_VALUE_DISABLED,
    CONF_VALUE_ENABLED,
    CONF_VOLUME_NORMALIZATION,
)
from music_assistant.helpers.process import AsyncProcess
from music_assistant.helpers.pulse_capture import (
    CAPTURE_CHANNELS,
    CAPTURE_SAMPLE_RATE,
    PipeSink,
    get_pulse_capture_server,
)
from music_assistant.models.music_provider import MusicProvider, ProviderStreamLimitError
from music_assistant.providers.spotify.constants import (
    CONF_AUDIO_QUALITY,
    CONF_SOLOIST_API_KEY,
    CONF_SOLOIST_CONSENT,
    CONF_SOLOIST_SESSION_DIR,
    SOLOIST_DATA_DIR_NAME,
    SOLOIST_DEVICE_NAME,
)
from music_assistant.providers.spotify.helpers import soloist_session_present
from music_assistant.providers.spotify_connect.base import (
    AUDIO_QUALITY_LOSSLESS,
    spotify_source_audio_format,
)
from music_assistant.providers.spotify_connect.soloist import (
    SoloistBinaryManager,
    SoloistClient,
    SoloistError,
    write_audio_prefs,
)
from music_assistant.providers.spotify_connect.soloist.runtime import (
    EXIT_CODE_BUILD_EXPIRED,
    WS_ADDR_FILE,
    WS_PORT_FILE,
    SoloistAuthState,
    SoloistDeviceChanged,
    SoloistOptionsChanged,
    SoloistPlaybackState,
    SoloistPositionSync,
    SoloistQueueChanged,
    SoloistTrackChanged,
    SoloistVolumeChanged,
)

from .base import SpotifyPlaybackBackend, StreamSupersededError

if TYPE_CHECKING:
    import logging
    from collections.abc import AsyncGenerator

    from music_assistant_models.streamdetails import StreamDetails

    from music_assistant.helpers.json import SerializableType
    from music_assistant.helpers.pulse_capture import PulseCaptureServer
    from music_assistant.providers.spotify.provider import SpotifyProvider
    from music_assistant.providers.spotify_connect.soloist.runtime import (
        SoloistEntity,
        SoloistEvent,
        SoloistPlaybackOptions,
    )

# The capture sink delivers fixed s32le/44.1kHz/2ch PCM. Soloist decodes
# internally and never exposes the source codec or bit depth (lossless up to
# 24-bit fits the 32-bit container losslessly), so this is what is handed over
# whatever the tier; what the user is shown comes from source_audio_format.
_FRAME_BYTES: Final[int] = 4 * CAPTURE_CHANNELS
_BYTES_PER_SECOND: Final[int] = CAPTURE_SAMPLE_RATE * _FRAME_BYTES

# Bounded soloist playback cache (docs: 0 = unlimited, otherwise at least 100 MB).
_CACHE_SIZE_MB: Final[int] = 512

# The reader clocks the pipe sink, and thus how fast Spotify must deliver.
# Both values are the result of live listening tests against the measured
# delivery envelope (1.1x sustained is clean even on a cold cache, 1.2x+
# starves mid-track):
# - the sustained 1.1x banks a downstream cushion (~6s per minute) that
#   absorbs the source-less gap at the next track boundary; at exactly 1.0x
#   every boundary gap reaches the player, where timeline-synced outputs
#   drop the late audio instead of delaying it.
# - the burst must stay SMALL: during the burst window the read is unpaced,
#   and demanding audio faster than the warming fetch pipeline can deliver
#   destabilizes it — large bursts audibly worsened track starts.
_PACE_RATE: Final[float] = 1.1
_PACE_BURST_S: Final[float] = 1.0

_READ_CHUNK_SIZE: Final[int] = 32768
# one wait slice on the FIFO read; state (process exit, errors) is checked between slices
_READ_SLICE_S: Final[float] = 1.0
# a suspended sink delivers nothing while soloist rebuffers; give recovery some room
_STALL_TIMEOUT_S: Final[float] = 30.0
# waiting for the daemon's WS endpoint, events and the requested item to appear
_STARTUP_TIMEOUT_S: Final[float] = 30.0
# How long a jump on a live session is given to land. Generous against the
# milliseconds the engine actually takes, but short enough that a jump which
# will not land still leaves the queue patience for the fresh session that
# serves the item instead.
_JUMP_TIMEOUT_S: Final[float] = 5.0
# how often to check whether the events task has the WebSocket up yet
_CONNECT_POLL_S: Final[float] = 0.05
_SEEK_CONFIRM_TIMEOUT_S: Final[float] = 15.0
# a seek is re-sent at this interval until a position anchor confirms it
_SEEK_RETRY_INTERVAL_S: Final[float] = 0.5
# position reported by a seek anchor may fall slightly before the requested target
_SEEK_TOLERANCE_MS: Final[int] = 2000
# Infrastructure silence precedes the session's first decoded sample; trim at
# most this much, once per session. Kept small on purpose: the trim cannot tell
# capture pre-roll from a genuinely digitally-silent intro, so the budget bounds
# what an intro can lose while still covering the measured pre-roll (~140 ms).
_MAX_LEAD_TRIM_S: Final[float] = 0.5
# Switching to another track (the next one queued, or a jump) the engine renders
# about half a second of digital silence before the track's first sample
# (measured 0.42-0.5 s). An item it switched to drops at most this much of it:
# its exact end is counted from its first sample.
_MAX_SWITCH_TRIM_S: Final[float] = 1.5
# What of that trim counts towards the item's own end: a silent intro beyond the
# switch silence is part of the track's length.
_MAX_SWITCH_GAP_S: Final[float] = 0.6
# below this much audio an item rendered nothing: the engine either refused it
# or never got going, and anything above it played and was cut short
_REFUSED_DELIVERY_MS: Final[int] = 1000
_REFUSED_MESSAGE: Final[str] = (
    "Spotify would not play this track: it is unavailable for this account "
    "or region, or Spotify refused it for now"
)
# How long a track Spotify refused fails at once instead of being tried again. The
# speaker asks for it only once it gets there, often a minute after the refusal,
# and a fresh run waits out its startup budget on it, far past the speaker's patience.
_REFUSAL_MEMORY_S: Final[float] = 300.0
# The elastic cushion between the paced FIFO reader and the consumer. A full
# cushion suspends the capture sink, which pauses the engine: the FIFO itself
# holds well under a second, so the reader must keep draining it whenever the
# consumer (the item's buffer, at its memory-tiered capacity) stops taking audio.
_RUN_CUSHION_S: Final[float] = 10.0
# how close to its target a position report has to come to confirm a seek
_SEEK_CONFIRM_GRACE_MS: Final[int] = 3000
# Past the last item it was given the engine pauses, rewound to 0:00. A pause
# anywhere else that lasts this long came from the Spotify app or Web API.
_EXTERNAL_PAUSE_GRACE_S: Final[float] = 2.0

# The sink renders exact-zero padding while the engine idles between items, and the
# item-change event arrives after those frames landed in the outgoing item's channel.
# Zero frames past a short grace, inside the item's own tail zone, are that padding.
_TAIL_PAD_ZONE_S: Final[float] = 10.0
_TAIL_PAD_GRACE_S: Final[float] = 1.0
# The engine allows one daemon per data directory and refuses to start otherwise,
# exiting with a plain code 1 - its message is the only way to tell that case
# apart from any other startup failure.
_DATA_DIR_BUSY_MARKER: Final[str] = "another session is running"
# A daemon that cannot log in advertises itself for pairing instead of failing,
# and then sits there until the startup budget runs out. The engine reports no
# other way that a stored session is gone.
_UNPAIRED_MARKER: Final[str] = "waiting for login"
# A sign-in Spotify turns down leaves the daemon running and silent, with no
# event and no exit: the line is its only report. Seen as a one-off that the
# very next daemon did not repeat, so it fails the item, not the pairing.
_LOGIN_FAILED_MARKER: Final[str] = "login failed"
# how long the log reader is given to catch up on a daemon's parting words
_LOG_DRAIN_TIMEOUT_S: Final[float] = 2.0
# How long a run whose item was let go keeps the engine for the request that
# follows it (a seek, a skip, the queue's next item) before stopping.
_IDLE_GRACE_S: Final[float] = 2.0
# How long that lasts while the run's queue still plays. A speaker that drops a track
# asks for the next one at once, but under load its request can take seconds to reach
# the engine, and a fresh run then starts too late for the speaker's patience.
_QUEUE_PLAYING_GRACE_S: Final[float] = 10.0
# how long the item the engine moved on to may sit unclaimed with a full cushion
_UNCLAIMED_ITEM_S: Final[float] = 10.0
# a playing report this close to 0:00, after the queued entry left the queue, is
# the engine starting the same track again
_RESTART_POSITION_MS: Final[int] = 3000
# a capture FIFO this long without new audio has drained
_DRAIN_QUIET_S: Final[float] = 0.05
# The engine's exact track length puts the cut where its audio ends; the
# reported moment of the track change trails that by the capture pipeline. A
# length further off than this from the reported moment is not trusted.
_MAX_CUT_SKEW_S: Final[float] = 1.0
# An item queued earlier than about the account's crossfade length plus 9 s before
# the current one ends is crossfaded into, by the account's own Spotify setting
# (which overrides the off written into the prefs). Queued this close to the end,
# the engine plays the two back to back whatever that setting is.
_FEED_LEAD_S: Final[float] = 8.0


class SoloistSessionBusyError(ProviderStreamLimitError):
    """
    Raised when the one Soloist run is delivering a different item.

    A ProviderStreamLimitError so a speculative prepare gives up softly and the
    item is not marked unplayable, but with a message of its own: "already
    playing something else" says more than the slot-budget phrasing.
    """

    def __init__(self, provider: MusicProvider) -> None:
        """
        Initialize the error.

        :param provider: The provider whose session is busy.
        """
        # deliberately skips ProviderStreamLimitError.__init__, whose whole job is
        # to phrase the message in terms of that source-stream budget
        MusicAssistantError.__init__(
            self,
            f"{provider.name} is already playing something else",
            translation_key="soloist_session_busy",
            translation_owner="provider.spotify",
            translation_args=[provider.name],
        )
        self.provider_instance = provider.instance_id
        self.limit = 1


class SoloistBackend(SpotifyPlaybackBackend):
    """
    Fetches Spotify audio through one ``soloist`` engine run per playback session.

    Requires a stored paired session in the per-instance data directory
    (provisioned by the setup flow via ``soloist --pair``).
    """

    _server: PulseCaptureServer | None = None
    _binary: Path | None = None
    _run: _EngineRun | None = None

    def __init__(self, provider: SpotifyProvider) -> None:
        """
        Initialize the backend.

        :param provider: The owning Spotify provider instance.
        """
        super().__init__(provider)
        # Guards every write of _run, every run teardown and every change of the
        # item a run serves. The engine allows one daemon per data directory, so
        # a replacement can only be spawned once the previous one is gone —
        # holding this across the teardown is what sequences that.
        self._run_lock = asyncio.Lock()
        # tracks Spotify refused, with the time until which they are not tried again
        self._refused_until: dict[str, float] = {}

    def source_audio_format(self, media_type: MediaType) -> AudioFormat:
        """
        Return the format Spotify is asked to stream for this item.

        The engine decodes internally and never reports what it fetched, so this
        is the configured ceiling rather than a measurement — the same thing the
        Spotify apps show. Only music is served losslessly; spoken content is
        Ogg Vorbis whatever the setting says.

        :param media_type: What is being streamed.
        """
        quality = self._audio_quality
        return spotify_source_audio_format(
            quality,
            lossless=media_type == MediaType.TRACK and quality == AUDIO_QUALITY_LOSSLESS,
        )

    @property
    def handoff_audio_format(self) -> AudioFormat:
        """Return the PCM the capture sink actually delivers."""
        return AudioFormat(
            content_type=ContentType.PCM_S32LE,
            codec_type=ContentType.PCM_S32LE,
            sample_rate=CAPTURE_SAMPLE_RATE,
            bit_depth=32,
            channels=CAPTURE_CHANNELS,
        )

    @property
    def is_realtime(self) -> bool:
        """Soloist delivers at playback pace (~1.1x ceiling): no read-ahead."""
        return True

    def session_normalizes(self, streamdetails: StreamDetails) -> bool | None:
        """
        Return whether the session serving this item's queue is normalizing.

        None when no session serves that queue, or the one serving it gives the
        item up to a fresh session, in which case the configuration is the only
        thing to go on.

        :param streamdetails: Stream details of the item being asked about.
        """
        run = self._run_for(streamdetails)
        return run.engine_normalizes if run is not None else None

    async def setup(self) -> None:
        """
        Validate the binary and paired session, and start the capture server.

        :raises LoginFailed: When the API key or paired session is missing, which
            requires the user to re-run the setup flow.
        """
        if not self._api_key:
            raise LoginFailed(
                "Spotify Soloist API key missing",
                translation_key="soloist_pairing_required",
                translation_owner="provider.spotify",
            )
        # setup errors (unsupported platform, missing consent, download failure,
        # expired build) propagate so the provider load fails with a clear error
        manager = SoloistBinaryManager(self.mass)
        self._binary = await manager.ensure_fresh(self._consent)
        await self._adopt_paired_session()
        if not await asyncio.to_thread(self._has_stored_session):
            raise LoginFailed(
                "Spotify Soloist is not paired with a Spotify account",
                translation_key="soloist_pairing_required",
                translation_owner="provider.spotify",
            )
        await asyncio.to_thread(self._cache_dir.mkdir, parents=True, exist_ok=True)
        self._server = await get_pulse_capture_server(self.mass).acquire()

    async def unload(self) -> None:
        """Stop the run and release the capture server."""
        async with self._run_lock:
            if (run := self._run) is not None:
                # dropped only once the teardown finished, so a cancellation
                # part-way leaves a later stop() something to clean up
                await run.stop()
                self._run = None
        if (server := self._server) is not None:
            self._server = None
            await server.release()

    async def stream_spotify_uri(
        self,
        spotify_uri: str,
        seek_position: int = 0,
        *,
        streamdetails: StreamDetails | None = None,
        continuation: bool = False,
    ) -> AsyncGenerator[bytes]:
        """
        Yield the PCM audio for one Spotify URI from the session's engine run.

        :param spotify_uri: Canonical Spotify URI (``spotify:track:<id>`` or
            ``spotify:episode:<id>``).
        :param seek_position: Position in seconds to start from.
        :param streamdetails: The StreamDetails this audio is requested for.
        :param continuation: Whether this URI continues an item stream already
            under way (a later chapter of an audiobook).
        """
        if self._server is None or self._binary is None:
            raise AudioError("Spotify Soloist backend is not started")
        run, item = await self._acquire_item(
            spotify_uri, seek_position, streamdetails, continuation=continuation
        )
        try:
            async for chunk in run.stream(item):
                yield chunk
        finally:
            # As its own task the release survives a second cancel of the consumer
            # (two aborts of one item land close together), which would otherwise
            # leave the item held and every other item refused as busy.
            self.mass.create_task(self.release_item(run, item))

    async def feed_next(self, media_key: str, spotify_uri: str) -> None:
        """
        Queue the item that follows the one being streamed on the engine.

        :param media_key: The StreamDetails uri of the item being streamed.
        :param spotify_uri: Canonical Spotify URI of the item that follows it.
        """
        async with self._run_lock:
            if (run := self._run) is not None:
                await run.feed_next(media_key, spotify_uri)

    async def release_item(self, run: _EngineRun, item: _Item) -> None:
        """
        End a stream's hold on its item; a run left serving nobody stops after a grace period.

        :param run: The run the item was served by.
        :param item: The item whose stream ended.
        """
        async with self._run_lock:
            await run.release(item)
            if run.stopped:
                await run.stop()
                if self._run is run:
                    self._run = None

    async def discard_run(self, run: _EngineRun, *, unless_busy: bool = False) -> None:
        """
        Stop a run for good, dropping it if it is still the current one.

        The teardown happens under the run lock, not after it: the engine
        refuses to start while another daemon still holds its data directory, so
        a replacement must not be spawned until this one is gone.

        :param run: The run to tear down.
        :param unless_busy: Keep the run when a stream took it up in the meantime.
        """
        async with self._run_lock:
            if unless_busy and run.busy and not run.stopped:
                return
            await run.stop()
            if self._run is run:
                self._run = None

    async def get_diagnostics(self) -> dict[str, SerializableType]:
        """Return diagnostic details about the backend (never any secret)."""
        run = self._run
        return {
            "soloist": SoloistBinaryManager(self.mass).diagnostics(),
            "paired": await asyncio.to_thread(self._has_stored_session),
            "session_active": run is not None,
            **(run.diagnostics() if run is not None else {}),
        }

    async def _acquire_item(
        self,
        spotify_uri: str,
        seek_position: int,
        streamdetails: StreamDetails | None,
        *,
        continuation: bool,
    ) -> tuple[_EngineRun, _Item]:
        """
        Get this item served: by the engine already on it, the running engine, or a fresh run.

        The engine allows one daemon per data directory (and Spotify one stream
        per account), so a run still serving another stream is reported as
        capacity: a speculative prepare gives up softly and the real request,
        made once that stream has been released, gets the slot.

        :raises StreamSupersededError: When a continuation finds the item taken
            over by the stream that replaced it.
        :raises AudioError: When Spotify refused this track a short while ago.
        """
        media_key = streamdetails.uri if streamdetails is not None else None
        seek_ms = seek_position * 1000
        waited = False
        while True:
            async with self._run_lock:
                # checked under the lock: a refusal can land while this request waits for it
                if self._refused_lately(spotify_uri):
                    raise AudioError(_REFUSED_MESSAGE)
                if (run := self._run) is not None and run.stopped:
                    # a run whose teardown was cut short serves nobody anymore: finish it
                    await run.stop()
                    self._run = None
                if (run := self._run) is None:
                    fresh = await self._start_run(spotify_uri, seek_ms, streamdetails)
                    return fresh, fresh.current_item
                try:
                    if streamdetails is not None and (
                        item := await run.claim(spotify_uri, seek_ms, streamdetails)
                    ):
                        return run, item
                    if not run.busy:
                        return run, await run.move_to(spotify_uri, seek_ms, streamdetails)
                except AudioError as err:
                    # the engine would not go where it was asked: a fresh run
                    # serves the item instead
                    self.logger.debug("Spotify Soloist session could not move on: %s", err)
                    await run.stop()
                    self._run = None
                    continue
                except asyncio.CancelledError:
                    # cut short half-way: no stream holds what was taken, so
                    # nothing would ever let the run go - unless a request
                    # waiting on the lock takes it up first
                    run.drop_holds()
                    self.mass.create_task(self.discard_run(run, unless_busy=True))
                    raise
                if run.media_key != media_key or media_key is None:
                    raise SoloistSessionBusyError(self.provider)
                if continuation and run.spotify_uri != spotify_uri:
                    # this stream was replaced (a seek of the same item) before it
                    # could continue into its next chapter - a run it must not
                    # take back
                    raise StreamSupersededError(f"The stream of {spotify_uri} was replaced")
            if waited:
                raise SoloistSessionBusyError(self.provider)
            # Same item, still held. A seek or restart races the release of the
            # stream it replaces, so give that release a moment - but never steal
            # a held item: a second queue occurrence of the same track asks with
            # these same details, and taking over its playing twin would cut it
            # mid-track.
            waited = True
            await self._wait_run_released(run)

    async def _start_run(
        self, spotify_uri: str, seek_ms: int, streamdetails: StreamDetails | None
    ) -> _EngineRun:
        """Spawn a fresh engine run on the given item (called with the run lock held)."""
        # cheap thanks to the shared verify cache; swaps in a fresh build
        # when the installed one is nearing its 90-day expiry
        try:
            self._binary = await SoloistBinaryManager(self.mass).ensure_fresh(self._consent)
        except SoloistError as err:
            raise AudioError(f"Spotify Soloist binary unavailable: {err}") from err
        fresh = _EngineRun(self, spotify_uri, seek_ms, streamdetails)
        try:
            await fresh.start()
        except BaseException:
            await fresh.stop()
            raise
        self._run = fresh
        return fresh

    async def _wait_run_released(self, run: _EngineRun, timeout: float = 2.0) -> None:
        """Wait briefly for the stream holding the given run to release it."""
        deadline = asyncio.get_running_loop().time() + timeout
        while self._run is run and run.busy and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.05)

    def _note_refusal(self, spotify_uri: str) -> None:
        """Remember that Spotify refused a track, so it is not tried again right away."""
        self._refused_until[spotify_uri] = asyncio.get_running_loop().time() + _REFUSAL_MEMORY_S

    def _refused_lately(self, spotify_uri: str) -> bool:
        """Return whether Spotify refused this track a short while ago."""
        now = asyncio.get_running_loop().time()
        self._refused_until = {
            uri: until for uri, until in self._refused_until.items() if until > now
        }
        if spotify_uri not in self._refused_until:
            return False
        self.logger.debug("Not trying %s again: Spotify refused it a moment ago", spotify_uri)
        return True

    @property
    def _api_key(self) -> str:
        """Return the stored Soloist API key."""
        return str(self.provider.get_setup_value(CONF_SOLOIST_API_KEY) or "")

    @property
    def _audio_quality(self) -> str:
        """
        Return the configured streaming quality ceiling.

        Stated rather than left to the engine's own default, which would
        otherwise decide it silently. Spotify serves the best the account is
        entitled to below the ceiling.
        """
        return str(self.provider.config.get_value(CONF_AUDIO_QUALITY) or AUDIO_QUALITY_LOSSLESS)

    @property
    def _consent(self) -> bool:
        """Return whether the user consented to downloading the binary."""
        return bool(self.provider.get_setup_value(CONF_SOLOIST_CONSENT))

    @property
    def _data_dir(self) -> Path:
        """Return the per-instance soloist data directory (paired session)."""
        return (
            Path(self.mass.storage_path)
            / "spotify"
            / self.provider.instance_id
            / SOLOIST_DATA_DIR_NAME
        )

    @property
    def _cache_dir(self) -> Path:
        """Return the per-instance soloist playback cache directory."""
        return Path(self.mass.cache_path) / self.provider.instance_id / "soloist-cache"

    @property
    def _sink_prefix(self) -> str:
        """Return the capture sink name prefix (PA-safe form of the instance id)."""
        return "".join(
            ch if ch.isalnum() or ch in "_.-" else "_" for ch in self.provider.instance_id
        )

    def _run_for(self, streamdetails: StreamDetails) -> _EngineRun | None:
        """
        Return the run serving this very item, unless it gives the item up to a fresh run.

        :param streamdetails: Stream details of the item being asked about.
        """
        run = self._run
        if (
            run is None
            or not run.serves(streamdetails.uri)
            # a seek or restart of the item moves on to a fresh run then
            or not run._suits_queue(streamdetails.queue_id)
        ):
            return None
        return run

    async def _adopt_paired_session(self) -> None:
        """Adopt a session paired by the setup flow into the per-instance data dir."""
        pending = str(self.provider.get_setup_value(CONF_SOLOIST_SESSION_DIR) or "")
        if not pending:
            return
        await asyncio.to_thread(self._copy_paired_session, pending)
        # config writes schedule tasks and must therefore run on the event loop
        self.provider._update_setup_data(CONF_SOLOIST_SESSION_DIR, None)

    def _copy_paired_session(self, pending: str) -> None:
        """
        Copy the paired session files into the canonical data dir (blocking).

        A copy (not a move) so the flow-private source survives a failed
        provider load: the setup flow can then retry its finish step and adopt
        the same pairing again. The source is removed when the flow ends.
        """
        source = Path(self.mass.storage_path) / pending
        canonical = self._data_dir
        if source.is_dir() and source != canonical:
            # stage first: a failed copy must never leave the canonical dir
            # half-written or destroy the currently working session
            staging = canonical.with_name(canonical.name + ".new")
            shutil.rmtree(staging, ignore_errors=True)
            canonical.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(source, staging)
            staging.chmod(0o700)
            if canonical.exists():
                shutil.rmtree(canonical)
            staging.replace(canonical)

    def _has_stored_session(self) -> bool:
        """Return whether the data dir holds a stored (paired) session (blocking)."""
        return soloist_session_present(self._data_dir)

    def _prepare_data_dir(self, *, normalize: bool) -> None:
        """
        Prepare the data dir for a fresh session spawn (blocking).

        :param normalize: Whether the engine should normalize loudness itself.
        """
        self._data_dir.mkdir(parents=True, exist_ok=True)
        self._data_dir.chmod(0o700)
        # endpoint files from a previous run would point the client at a dead port
        for endpoint_file in (WS_ADDR_FILE, WS_PORT_FILE):
            (self._data_dir / endpoint_file).unlink(missing_ok=True)
        # The engine reads its prefs at startup only, so they are refreshed on
        # every spawn. Whoever normalizes, only one of us may: with the engine
        # doing it the provider declares the audio pre-normalized, which takes
        # MA's own normalization out of the path (see
        # SpotifyProvider.delivers_normalized_audio).
        # crossfade 0: Music Assistant mixes the queue's crossfade itself, so the
        # engine plays every track clean from its first sample and an item's
        # delivered audio lines up with its analysis (waveform, beat grid, light sync).
        # A crossfade in the account's Spotify playback settings overrides it, which
        # is why the next item is only queued in the current one's last seconds.
        if not write_audio_prefs(
            self._data_dir,
            self.logger,
            crossfade_ms=0,
            loudness_normalization=normalize,
            audio_quality=self._audio_quality,
        ):
            # the provider has told the rest of the server who normalizes this
            # audio; running the engine on settings that may say otherwise would
            # mean normalizing twice, or not at all
            raise AudioError("Spotify Soloist audio settings could not be applied")

    def _session_args(self) -> list[str]:
        """
        Build the argv for one engine run.

        SECURITY: the argv carries the user's API key — it must never be logged
        or end up in any error message.
        """
        assert self._binary is not None
        return [
            str(self._binary),
            # the ordinary mode advertises the device on the network while it
            # runs: only it plays the items it is given one after another
            "--device-name",
            SOLOIST_DEVICE_NAME,
            "--api-key",
            self._api_key,
            "--data-dir",
            str(self._data_dir),
            "--cache-dir",
            str(self._cache_dir),
            # bounded playback cache (0 would be unlimited)
            "--cache-size",
            str(_CACHE_SIZE_MB),
            # unity volume so unaltered PCM reaches the capture sink
            "--initial-volume",
            "100",
            # local WebSocket API on a free loopback port; the daemon publishes
            # the actual endpoint in its data dir where SoloistClient finds it
            "--ws",
            "127.0.0.1:0",
        ]


@dataclass
class _FedEntry:
    """An item queued on the engine to follow the one being streamed."""

    spotify_uri: str
    # the engine's id of the queue entry, known once a queue report lists it
    uid: str | None = None
    # the entry left the queue: the engine started it, or somebody removed it
    left: bool = False


class _Item:
    """
    One queue item's share of a run's capture.

    Every item has its own cushion, so the item the engine left can still drain
    to its consumer while the engine already renders the next one.
    """

    def __init__(
        self,
        spotify_uri: str,
        seek_target_ms: int,
        streamdetails: StreamDetails | None,
        *,
        max_lead_trim_s: float,
        max_counted_lead_s: float | None = None,
    ) -> None:
        """
        Initialize the state of one item.

        :param spotify_uri: Canonical Spotify URI of the item.
        :param seek_target_ms: Position the item starts from, in milliseconds.
        :param streamdetails: The StreamDetails claiming the item, None while
            nobody has asked for it yet.
        :param max_lead_trim_s: How much leading digital silence may be dropped.
        :param max_counted_lead_s: How much of the dropped silence precedes the
            item's first sample (all of it when not given).
        """
        self.spotify_uri = spotify_uri
        self.seek_target_ms = seek_target_ms
        self.media_key: str | None = None
        self.duration_ms: int | None = None
        # the exact length the engine reports; the catalog only knows whole seconds
        self.engine_duration_ms: int | None = None
        self.started = asyncio.Event()
        self.error: str | None = None
        cushion_chunks = max(2, int(_RUN_CUSHION_S * _BYTES_PER_SECOND / _READ_CHUNK_SIZE))
        self.chunks: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=cushion_chunks)
        self.shaper = _CaptureShaper(max_lead_trim_s)
        self._max_counted_lead = int(
            (max_lead_trim_s if max_counted_lead_s is None else max_counted_lead_s)
            * _BYTES_PER_SECOND
        )
        # capture bytes that belong to this item, and where (in those) it ends
        self.raw_read = 0
        # bytes at its start that finish the frame the capture was in
        self.align_skip = 0
        self.end_at: int | None = None
        self.read_bytes = 0
        self.delivered = 0
        self.tail_zeros = 0
        self.delivery_done = False
        # a stream is consuming the item
        self.streaming = False
        # its stream let go of it before it was delivered in full
        self.abandoned = False
        self.unclaimed_since: float | None = None
        # the engine reached it through its queue, fed to follow another item
        self.fed = False
        if streamdetails is not None:
            self.bind(streamdetails)

    @property
    def length_ms(self) -> int | None:
        """Return the item's length as best known."""
        return self.engine_duration_ms or self.duration_ms

    @property
    def ended(self) -> bool:
        """Return whether the engine has finished this item."""
        return self.end_at is not None

    @property
    def position_ms(self) -> float:
        """Return how far into the track the capture has read."""
        return self.seek_target_ms + self.raw_read * 1000 / _BYTES_PER_SECOND

    @property
    def in_tail(self) -> bool:
        """Return whether the capture has reached the last seconds of the item."""
        if (length_ms := self.length_ms) is None:
            return False
        return self.position_ms >= length_ms - _TAIL_PAD_ZONE_S * 1000

    def bind(self, streamdetails: StreamDetails) -> None:
        """
        Claim the item for the stream of the given details.

        :param streamdetails: The StreamDetails whose stream takes the item.
        """
        self.media_key = streamdetails.uri
        if streamdetails.duration:
            self.duration_ms = streamdetails.duration * 1000
        self.streaming = True
        self.unclaimed_since = None

    def end_offset(self, event_offset: int) -> int:
        """
        Return the capture offset at which the item ends.

        :param event_offset: Where the capture stood when the engine reported
            the item's end, back-dated by how far the engine was into the next one.
        """
        offset = event_offset
        if self.engine_duration_ms is not None:
            frames = round(
                (self.engine_duration_ms - self.seek_target_ms) * CAPTURE_SAMPLE_RATE / 1000
            )
            lead = min(self.shaper.lead_skipped, self._max_counted_lead)
            exact = self.align_skip + lead + frames * _FRAME_BYTES
            if abs(exact - event_offset) <= _MAX_CUT_SKEW_S * _BYTES_PER_SECOND:
                offset = exact
        # what was read already cannot be taken back
        offset = max(offset, self.raw_read)
        # the next item starts on a whole frame
        return self.align_skip + -(-(offset - self.align_skip) // _FRAME_BYTES) * _FRAME_BYTES


class _EngineRun:
    """
    One engine run serving a queue's items one after another, streamed as it renders.

    The engine runs in its ordinary mode with shuffle and repeat off. The item
    that follows the one being streamed is queued on it ahead of time; where
    the engine moves on to it, the capture is cut and the rest goes to that
    item, which its stream then claims. A seek, skip or jump moves the running
    engine instead of starting another one. The capture FIFO is reader-clocked:
    it is read at ``_PACE_RATE`` into each item's bounded cushion, whose
    backpressure suspends the capture sink, which is what pauses the engine
    when the consumer stops taking audio.
    """

    # switches shuffle and repeat off while the engine reports one on
    _options_reset: asyncio.Task[None] | None = None
    # the item to queue on the engine once the current one is in its last seconds
    _pending_feed: str | None = None

    def __init__(
        self,
        backend: SoloistBackend,
        spotify_uri: str,
        seek_position_ms: int,
        streamdetails: StreamDetails | None,
    ) -> None:
        """
        Initialize a run (nothing is spawned yet).

        :param backend: The owning backend.
        :param spotify_uri: Canonical Spotify URI the engine is to play first.
        :param seek_position_ms: Position to start from, in milliseconds.
        :param streamdetails: The StreamDetails the audio is requested for.
        """
        self.backend = backend
        self.mass = backend.mass
        self.logger: logging.Logger = backend.logger
        self.queue_id = streamdetails.queue_id if streamdetails is not None else None
        # what the engine was actually told at spawn, which is what the streams
        # core has to agree with - the setting may be toggled while this plays
        self.engine_normalizes = False
        self._item = _Item(
            spotify_uri, seek_position_ms, streamdetails, max_lead_trim_s=_MAX_LEAD_TRIM_S
        )
        # the item the capture moves on to where the current one ends
        self._next: _Item | None = None
        self._fed: _FedEntry | None = None
        self._queue_uids: set[str] = set()
        self._engine_uri: str | None = None
        # the exact length of the item the engine last described
        self._engine_length: tuple[str, int] | None = None
        self._client: SoloistClient | None = None
        self._sink: PipeSink | None = None
        self._proc: AsyncProcess | None = None
        self._tasks: list[asyncio.Task[None]] = []
        self._log_task: asyncio.Task[None] | None = None
        self._transport: asyncio.ReadTransport | None = None
        self._reader: asyncio.StreamReader | None = None
        self._error: str | None = None
        self._logged_in: bool | None = None
        self._data_dir_busy = False
        self._unpaired = False
        self._stopped = False
        self._engine_exited = False
        self._teardown_done = False
        self._engine_playing = False
        self._engine_paused = False
        # the engine last reported shuffle or repeat switched on
        self._options_on = False
        self._pause_watch: asyncio.Task[None] | None = None
        self._idle_watch: asyncio.Task[None] | None = None
        self._sink_running = False
        self._sink_lock = asyncio.Lock()
        self._play_sent = False
        # a seek, skip or jump is under way: what the capture holds belongs to nobody
        self._moving = False
        self._move_target: str | None = None
        self._move_landed = asyncio.Event()
        self._fed_consumed = asyncio.Event()
        self._skipping_to_fed = False
        self._seek_target_ms: int | None = None
        self._seek_landed = asyncio.Event()
        self._pace_reset = False
        self._last_read_at = 0.0
        # every capture byte read; items start on its frame grid
        self._raw_total = 0
        self._read_waiting = False
        self._startup_logged = False
        # startup phase timings, for the one debug line per run
        self._spawned_at = 0.0
        self._endpoint_at: float | None = None
        self._login_at: float | None = None
        self._playing_at: float | None = None

    @property
    def current_item(self) -> _Item:
        """Return the item the capture belongs to now."""
        return self._item

    @property
    def spotify_uri(self) -> str:
        """Return the Spotify URI of the item the capture belongs to now."""
        return self._item.spotify_uri

    @property
    def media_key(self) -> str | None:
        """Return the StreamDetails uri claiming the current item, if any."""
        return self._item.media_key

    @property
    def busy(self) -> bool:
        """Return whether a stream holds the item the engine is on, or the one it moves on to."""
        return self._item.streaming or (self._next is not None and self._next.streaming)

    @property
    def stopped(self) -> bool:
        """Return whether this run's teardown has begun."""
        return self._stopped

    def serves(self, media_key: str) -> bool:
        """
        Return whether the run serves (or is about to serve) the item of this key.

        :param media_key: The StreamDetails uri of the item.
        """
        return media_key in (
            self._item.media_key,
            self._next.media_key if self._next is not None else None,
        )

    def diagnostics(self) -> dict[str, SerializableType]:
        """Return what the run is serving (never any secret)."""
        upcoming = self._next or self._fed
        return {
            "current_item": self._item.spotify_uri,
            "next_item": upcoming.spotify_uri if upcoming is not None else self._pending_feed,
            "idle": not self._item.streaming,
        }

    async def claim(
        self, spotify_uri: str, seek_position_ms: int, streamdetails: StreamDetails
    ) -> _Item | None:
        """
        Claim the item the engine moved on to by itself, when it is this one.

        :param spotify_uri: Canonical Spotify URI of the requested item.
        :param seek_position_ms: Position the request starts from.
        :param streamdetails: The StreamDetails the audio is requested for.
        """
        if (
            seek_position_ms
            or self._stopped
            or self._error is not None
            or self._engine_exited
            or not self._suits_queue(streamdetails.queue_id)
        ):
            return None
        for item in (self._item, self._next):
            if item is None or item.media_key is not None or item.spotify_uri != spotify_uri:
                continue
            item.bind(streamdetails)
            self.queue_id = streamdetails.queue_id
            self._cancel_idle_watch()
            if item is self._item or self._item.abandoned:
                # the capture may be held for a request: this is the one (the
                # item before it may have been let go short of its end)
                await self._set_sink(running=not item.chunks.full())
            self.logger.debug("%s taken up from the running Spotify session", spotify_uri)
            return item
        return None

    async def feed_next(self, media_key: str, spotify_uri: str) -> None:
        """
        Queue the item that follows the one being streamed on the engine, once.

        The engine gets it in the current item's last seconds: until then the run
        holds it, and every later hint replaces it (one the engine cannot take
        leaves nothing held).

        :param media_key: The StreamDetails uri of the item being streamed.
        :param spotify_uri: Canonical Spotify URI of the item that follows it.
        """
        client = self._client
        held = self._pending_feed
        if media_key == self._item.media_key:
            self._pending_feed = None
        if self._queue_uids and self._fed is None:
            # the account's own queued items would play before it
            self.logger.debug(
                "Not queueing %s: the account's own Spotify queue holds %d item(s)",
                spotify_uri,
                len(self._queue_uids),
            )
            return
        if (
            self._fed is not None
            or self._next is not None
            # the engine does not take episodes into its queue
            or not spotify_uri.startswith("spotify:track:")
            or self._item.media_key != media_key
            or not self._item_live()
            or client is None
            or not client.connected
            # its request fails at once, so the engine would play it to nobody
            or self.backend._refused_lately(spotify_uri)
        ):
            return
        if not self._feed_due():
            if spotify_uri != held:
                self.logger.debug(
                    "Holding %s back until %s nears its end", spotify_uri, self._item.spotify_uri
                )
            self._pending_feed = spotify_uri
            return
        self._fed = _FedEntry(spotify_uri)
        try:
            await client.add_to_queue(spotify_uri)
        except (SoloistError, OSError) as err:
            self._fed = None
            self.logger.debug("Could not queue %s on the Spotify session: %s", spotify_uri, err)
            return
        self.logger.debug(
            "Queued %s on the Spotify session to follow %s", spotify_uri, self._item.spotify_uri
        )

    async def move_to(
        self, spotify_uri: str, seek_position_ms: int, streamdetails: StreamDetails | None
    ) -> _Item:
        """
        Serve an item, or another position of one, on the running engine.

        :param spotify_uri: Canonical Spotify URI of the requested item.
        :param seek_position_ms: Position the request starts from.
        :param streamdetails: The StreamDetails the audio is requested for.
        :raises AudioError: When the engine did not get there; the run is of no
            use anymore then.
        """
        client = self._client
        if (
            self._stopped
            or self._error is not None
            or self._engine_exited
            or client is None
            or not client.connected
        ):
            raise AudioError("The Spotify session is no longer running")
        if streamdetails is not None and not self._suits_queue(streamdetails.queue_id):
            raise AudioError("The Spotify session normalizes for another player")
        self._cancel_idle_watch()
        previous = self._item
        # back out of the feed lead, the item queued on the engine would be crossfaded
        # into: the jump skips onto it, which takes it off, and plays the track afresh
        in_place = spotify_uri == self._engine_uri and not self._fed_too_early_for(
            spotify_uri, seek_position_ms
        )
        self._pending_feed = None
        self._moving = True
        # nobody takes the previous item anymore: a capture waiting on its
        # cushion has to move on to the drain
        previous.abandoned = True
        self._release_item_waiters(previous)
        try:
            await self._set_sink(running=False)
            await self._drain()
            async with asyncio.timeout(_JUMP_TIMEOUT_S):
                landed_on = None
                if not in_place:
                    if self._fed is not None and self._fed.uid is None:
                        # never confirmed queued: most likely refused
                        self._fed = None
                    if self._fed is not None:
                        # a queued item survives a jump: left there, it would play
                        # after the item jumped to
                        landed_on = await self._skip_to_fed(client)
                    if spotify_uri != landed_on:
                        await self._play(client, spotify_uri)
                item = self._new_item(spotify_uri, seek_position_ms, switched=not in_place)
                if item.engine_duration_ms is None and previous.spotify_uri == spotify_uri:
                    # the engine may describe the track skipped through last
                    item.engine_duration_ms = previous.engine_duration_ms
                # a seek can land just before its end, so once the move is done only
                # a skip onto its start can still meet a refusal
                item.fed = landed_on == spotify_uri and not seek_position_ms
                # the drain may have stopped mid-frame
                item.align_skip = -self._raw_total % _FRAME_BYTES
                self._item, self._next = item, None
                item.started.set()
                if in_place or seek_position_ms:
                    await self._seek(client, seek_position_ms)
                if in_place and (self._engine_paused or previous.ended):
                    # past the last item it was given the engine sits paused
                    await client.resume()
        except (TimeoutError, SoloistError, OSError) as err:
            raise AudioError(f"Spotify did not move to {spotify_uri}: {err!r}") from err
        finally:
            self._moving = False
        if landed_on == spotify_uri and self._engine_uri != spotify_uri:
            # the engine already left the fed track while the move landed (a report
            # of the track it fell back to can confirm a short seek): Spotify refused it
            self._refuse(item)
        if streamdetails is not None:
            item.bind(streamdetails)
            self.queue_id = streamdetails.queue_id
        self._pace_reset = True
        await self._set_sink(running=True)
        self.logger.debug("Spotify session moved to %s at %d ms", spotify_uri, seek_position_ms)
        return item

    def drop_holds(self) -> None:
        """Let go of every item a stream holds, leaving the run to whoever takes it next."""
        for item in (self._item, self._next):
            if item is not None and item.streaming:
                item.streaming = False
                item.abandoned = True
                self._release_item_waiters(item)

    async def release(self, item: _Item) -> None:
        """
        End a stream's hold on its item; a run left serving nobody idles out.

        :param item: The item whose stream ended.
        """
        item.streaming = False
        if not item.delivery_done:
            item.abandoned = True
            self._release_item_waiters(item)
        current = self._item
        if item is current:
            if not self._stopped and not self.busy:
                await self._go_idle()
        elif current.media_key is None and current.unclaimed_since is None:
            # the queue asks for the next item once the one before it is done
            self._watch_unclaimed(current)

    async def start(self) -> None:
        """Spawn the engine, play this run's first item and get its audio flowing."""
        backend = self.backend
        server = backend._server
        assert server is not None
        assert backend._binary is not None
        loop = asyncio.get_running_loop()
        self.engine_normalizes = self._engine_normalization_enabled(self.queue_id)
        await asyncio.to_thread(
            partial(backend._prepare_data_dir, normalize=self.engine_normalizes)
        )
        self._sink = sink = await PipeSink.create(server, backend._sink_prefix)
        # unity gain so the FIFO carries the engine's PCM unaltered; the sink
        # stays suspended until the (seeked) item is ready so no infrastructure
        # silence accumulates
        await sink.set_volume(100)
        await sink.suspend()
        self._proc = proc = AsyncProcess(
            backend._session_args(),
            # the daemon writes all of its logging to stdout and only ever puts
            # argument-parsing complaints on stderr, so the two are merged into
            # one captured stream. Capturing is what makes the redaction below
            # reachable at all: an unset stdout is inherited, which would leak
            # the daemon's output straight to the server console instead.
            stdout=True,
            stderr=asyncio.subprocess.STDOUT,
            # the explicit process name keeps AsyncProcess logging free of the
            # argv (which carries the API key)
            name=f"soloist[{backend.provider.name}]",
            env=server.child_env(sink.sink_name),
        )
        await proc.start()
        self._spawned_at = loop.time()
        # kept out of _tasks so stop() does not cancel it before the daemon has
        # exited, but a reader that dies still has to fail the run: nothing
        # else drains stdout and the daemon would block on a full pipe
        self._log_task = asyncio.create_task(self._log_output(proc))
        self._log_task.add_done_callback(self._task_done)
        self._client = client = SoloistClient(self.mass, backend._data_dir, self.logger)
        client_ready = asyncio.Event()
        self._spawn_task(self._run_events(client, client_ready))
        # Commands travel over the events connection, and the engine takes them
        # in three stages: it publishes its endpoint, then accepts a connection,
        # then restores its session and logs in. A failure reported while the
        # endpoint is still awaited - the engine having no session to log in
        # with - is watched for throughout.
        try:
            async with asyncio.timeout(_STARTUP_TIMEOUT_S):
                while not self._error and not (
                    client_ready.is_set() and client.connected and self._logged_in
                ):
                    await asyncio.sleep(_CONNECT_POLL_S)
        except TimeoutError:
            self._raise_startup_error("could not connect and sign in")
        if self._error or not client.connected:
            self._raise_startup_error("did not start up correctly")
        try:
            # claim the device first: a bare play would not transfer playback to
            # it. The engine restores the account's last shuffle and repeat
            # options, which would reorder or loop the items it is given.
            await client.activate(await_result=True)
            await client.set_shuffle(False, await_result=True)
            await client.set_repeat_context(False, await_result=True)
            await client.set_repeat_track(False, await_result=True)
            self._play_sent = True
            await client.play(self._item.spotify_uri)
            # the account's own queued items decide whether items can be fed
            await client.get_queue()
        except (SoloistError, TimeoutError) as err:
            self._raise_startup_error(f"did not take the play command ({err})")
        await self._await_playback_started(proc)
        if self._item.seek_target_ms:
            await self._seek(client, self._item.seek_target_ms)
        # the reader must be attached before the sink starts producing, or the
        # sink's first writes go to a reader-less FIFO and are dropped
        self._reader, self._transport = await _open_fifo_reader(sink.fifo_path)
        self._spawn_task(self._watch_exit(proc))
        self._spawn_task(self._read_capture())
        await self._set_sink(running=True)

    async def stream(self, item: _Item) -> AsyncGenerator[bytes]:
        """
        Yield an item's PCM audio, ending where the engine ended the item.

        :param item: The item to stream.
        """
        while True:
            # the flag ends a delivery whose sentinel found the cushion full:
            # only an empty cushion can leave the consumer blocked below, and
            # an empty cushion always has room for the sentinel
            if item.delivery_done and item.chunks.empty():
                break
            if (chunk := await item.chunks.get()) is None:
                break
            item.delivered += len(chunk)
            yield chunk
        if item.error is not None:
            raise AudioError(item.error)
        self._validate_delivery(item)

    async def stop(self) -> None:
        """
        Tear the run down: stop the daemon, the reader and the capture sink.

        Safe to call again after a cancelled teardown: every step is idempotent
        and the run is only marked torn down once they have all run, so a
        cancellation part-way cannot leave the daemon or the sink behind.
        """
        if self._teardown_done:
            return
        self._stopped = True
        self._release_waiters()
        if self._engine_exited and (proc := self._proc) is not None:
            # Reap an engine that already exited before anything else, so close()
            # below finds a returncode and returns right away. Closing an unreaped
            # process instead silently waits out its stream-lock and flush budgets.
            with suppress(Exception):
                await asyncio.wait_for(proc.wait(), 5)
        await _cancel_and_join(self._tasks)
        self._tasks.clear()
        if self._transport is not None:
            self._transport.close()
            self._transport = None
        if (proc := self._proc) is not None:
            # Closed straight away, with no grace period: the engine never quits
            # on its own. The log reader stays alive across the close: nothing
            # else drains the daemon's stdout, and a full pipe would keep it
            # from exiting. A forced close must never be judged by its exit code.
            with suppress(Exception):
                await proc.close()
            if proc.returncode is None:
                # close() has exhausted its kill attempts, so nothing here can do
                # better; the daemon keeps the data directory and the next spawn
                # reports it as busy
                self.logger.warning("The Spotify Soloist daemon could not be stopped")
            # dropped only now: a cancellation during the awaits above must leave
            # the retry something to close, or the daemon keeps the data
            # directory and every later run is refused
            self._proc = None
        if self._log_task is not None:
            await _cancel_and_join([self._log_task])
            self._log_task = None
        if (sink := self._sink) is not None:
            with suppress(Exception):
                await sink.unload()
            self._sink = None
        self._teardown_done = True

    # ---- internals ----

    def _spawn_task(self, coro: object) -> asyncio.Task[None]:
        """Track a run-scoped task so stop() can cancel and join it."""
        task: asyncio.Task[None] = asyncio.create_task(coro)  # type: ignore[arg-type]
        task.add_done_callback(self._task_done)
        self._tasks = [running for running in self._tasks if not running.done()]
        self._tasks.append(task)
        return task

    def _task_done(self, task: asyncio.Task[None]) -> None:
        """Fail the run when one of its tasks died of an unexpected error."""
        if task.cancelled() or (err := task.exception()) is None:
            return
        self.logger.error("Spotify Soloist task failed: %s", err, exc_info=err)
        self._fail(f"Spotify playback stopped unexpectedly: {err}")

    def _fail(self, message: str) -> None:
        """Record a fatal error, unblock the consumers and tear the run down."""
        if self._error is not None or self._stopped:
            return
        self._error = message
        for item in (self._item, self._next):
            if item is not None and not item.ended:
                item.error = message
        self._release_waiters()
        # the teardown runs as its own task: it cancels the very tasks this is
        # called from, and the daemon has to go either way
        self.mass.create_task(self.backend.discard_run(self))

    def _release_waiters(self) -> None:
        """Unblock everything waiting on this run: startup, moves and the consumers."""
        self._move_landed.set()
        self._fed_consumed.set()
        self._seek_landed.set()
        for item in (self._item, self._next):
            if item is not None:
                item.started.set()
                self._release_item_waiters(item)

    def _release_item_waiters(self, item: _Item) -> None:
        """Unblock an item's consumer, unless it is still owed the tail it ended with."""
        if item.ended and item.error is None and not item.abandoned:
            # the consumer is still entitled to the cushioned tail and ends once
            # it drains, even when the capture never reached the item's end
            self._finish_delivery(item)
            return
        # the cushion holds audio nobody will take anymore; the sentinel has to
        # reach the consumer either way
        while True:
            try:
                item.chunks.get_nowait()
            except asyncio.QueueEmpty:
                break
        with suppress(asyncio.QueueFull):
            item.chunks.put_nowait(None)

    def _validate_delivery(self, item: _Item) -> None:
        """
        Raise when the engine died on this item, or played none of it.

        Audio that arrives and then stops belongs to something ending the item
        early - a skip, a seek or the account playing elsewhere - so a partial
        delivery is only a failure when the engine itself did not end cleanly.
        """
        if self._stopped:
            return
        if self._engine_exited and not item.ended:
            # audio may well have arrived first, but the engine did not end the
            # item: it died on it, whatever the queue got to play
            proc = self._proc
            exit_code = proc.returncode if proc is not None else None
            raise AudioError(
                f"Spotify playback stopped unexpectedly (engine exit code {exit_code})"
            )
        if (length_ms := item.length_ms) is None:
            return
        played_ms = item.delivered / _BYTES_PER_SECOND * 1000
        expected_ms = length_ms - item.seek_target_ms
        if (
            played_ms > _REFUSED_DELIVERY_MS
            # a seek can leave an item with almost nothing left to play
            or expected_ms <= _REFUSED_DELIVERY_MS
            # a complete delivery arrives short by what the lead trim took off it
            or played_ms + item.shaper.lead_skipped * 1000 / _BYTES_PER_SECOND >= expected_ms
        ):
            return
        if item.ended:
            raise AudioError(_REFUSED_MESSAGE)
        raise AudioError("Spotify stopped before it played any of this track")

    def _engine_normalization_enabled(self, queue_id: str | None) -> bool:
        """
        Return whether the engine should normalize the loudness it delivers.

        The player's own volume normalization switch decides first: turning it
        off means nobody normalizes, not that the job passes to Spotify.

        :param queue_id: The queue the audio plays on.
        """
        if not self.backend.provider.spotify_normalization_configured:
            return False
        if queue_id is None:
            # nothing to read the switch from, so the provider option stands
            return True
        return (
            self.mass.config.get_effective_player_queue_config_value(
                queue_id, CONF_VOLUME_NORMALIZATION, CONF_VALUE_ENABLED
            )
            != CONF_VALUE_DISABLED
        )

    def _suits_queue(self, queue_id: str | None) -> bool:
        """
        Return whether the engine's audio suits the given queue.

        :param queue_id: The queue the audio is requested for.
        """
        # the engine reads its normalization setting at startup only
        return self._engine_normalization_enabled(queue_id) == self.engine_normalizes

    async def _await_playback_started(self, proc: AsyncProcess) -> None:
        """Wait until the engine reports it is playing this run's first item."""
        try:
            async with asyncio.timeout(_STARTUP_TIMEOUT_S):
                exit_task = asyncio.ensure_future(proc.wait())
                started_task = asyncio.ensure_future(self._item.started.wait())
                try:
                    await asyncio.wait(
                        {exit_task, started_task}, return_when=asyncio.FIRST_COMPLETED
                    )
                finally:
                    exit_task.cancel()
                    started_task.cancel()
        except TimeoutError:
            self._raise_startup_error("did not start playing in time")
        if self._error or not self._item.started.is_set():
            if proc.returncode is not None:
                # let the log reader catch up, so the daemon's own complaint can
                # be reported instead of a generic startup failure
                await self._drain_log()
            if proc.returncode == EXIT_CODE_BUILD_EXPIRED:
                # an expired build exits with code 10 right at spawn
                await self._handle_expired_build()
            self._raise_startup_error("stopped before it started playing")

    async def _drain_log(self) -> None:
        """Give the log reader a moment to deliver the daemon's parting words."""
        if self._log_task is not None:
            with suppress(TimeoutError):
                async with asyncio.timeout(_LOG_DRAIN_TIMEOUT_S):
                    await asyncio.shield(self._log_task)

    async def _handle_expired_build(self) -> NoReturn:
        """Replace the expired soloist build and fail the item with an accurate message."""
        try:
            # bypass the verify cache — it would hand back the same expired binary
            self.backend._binary = await SoloistBinaryManager(self.mass).ensure_fresh(
                self.backend._consent, force=True
            )
        except SoloistError as err:
            raise AudioError(
                "Spotify Soloist build expired and no replacement could be installed"
            ) from err
        raise AudioError("Spotify Soloist build expired; a replacement was installed, retry")

    def _raise_startup_error(self, detail: str) -> NoReturn:
        """Raise the most specific startup failure for the requested item."""
        # a lost pairing is checked first: it also fails the run, and its
        # recovery (back through the setup flow) beats failing every track with
        # a generic error. Only the confirmed loss counts: a sign-in that never
        # arrived looks the same when Spotify is slow or another player took
        # the account over, and pairing again cures neither
        if self._unpaired:
            # mirrors librespot's INVALID_CREDENTIALS handling
            error = LoginFailed(
                "Spotify Soloist pairing lost",
                translation_key="soloist_pairing_required",
                translation_owner="provider.spotify",
            )
            provider = self.backend.provider
            if provider.available:
                provider.unload_with_error(error)
            raise error
        if self._data_dir_busy:
            # a daemon from an earlier Music Assistant process is still holding
            # this provider's data directory; nothing here can reach it
            raise AudioError(
                "Another Spotify Soloist session is still running for this provider "
                "and has to be stopped first (restarting Music Assistant clears it)"
            )
        if self._error:
            raise AudioError(self._error)
        raise AudioError(f"Spotify {detail}")

    async def _seek(self, client: SoloistClient, target_ms: int) -> None:
        """
        Seek the engine to the target position before any PCM of it is released.

        The sink is suspended, so no pre-seek audio enters the FIFO; PCM demand
        only starts once a position report confirms the seek landed.
        """
        # the engine silently drops a seek that arrives while the track is still
        # loading (verified via event trace), so re-send it until a position
        # report confirms it landed
        self._seek_target_ms = target_ms
        self._seek_landed.clear()
        deadline = asyncio.get_running_loop().time() + _SEEK_CONFIRM_TIMEOUT_S
        try:
            while True:
                await client.seek(target_ms)
                with suppress(TimeoutError):
                    async with asyncio.timeout(_SEEK_RETRY_INTERVAL_S):
                        await self._seek_landed.wait()
                if self._seek_landed.is_set():
                    if self._error:
                        raise AudioError(f"Spotify Soloist failed: {self._error}")
                    return
                if asyncio.get_running_loop().time() >= deadline:
                    raise AudioError(f"Spotify Soloist did not confirm seeking to {target_ms}ms")
        finally:
            self._seek_target_ms = None

    async def _skip_to_fed(self, client: SoloistClient) -> str | None:
        """Move the engine onto the item queued on it and return that item's URI."""
        landed_on = self._fed.spotify_uri if self._fed is not None else None
        self._fed_consumed.clear()
        self._skipping_to_fed = True
        try:
            await client.skip_next()
            await self._fed_consumed.wait()
        finally:
            self._skipping_to_fed = False
        return landed_on

    async def _play(self, client: SoloistClient, spotify_uri: str) -> None:
        """Have the engine play the given item."""
        self._move_target = spotify_uri
        self._move_landed.clear()
        try:
            await client.play(spotify_uri)
            await self._move_landed.wait()
        finally:
            self._move_target = None

    async def _drain(self) -> None:
        """Wait until what the suspended sink rendered has been read off (and dropped)."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + _JUMP_TIMEOUT_S
        while self._reader is not None and loop.time() < deadline:
            # a capture waiting on the FIFO holds nothing read but not yet taken
            if (
                self._read_waiting
                and _unread_fifo_bytes(self._transport) == 0
                and loop.time() - self._last_read_at >= _DRAIN_QUIET_S
            ):
                return
            await asyncio.sleep(_DRAIN_QUIET_S / 5)

    async def _go_idle(self) -> None:
        """
        Hold the engine where it is for the request that may follow.

        A seek, a skip or the queue's next item finds it there; with nothing
        coming, the run stops after the grace period (longer while its queue
        still plays) instead of letting the engine play on to nobody.
        """
        await self._set_sink(running=False)
        self._cancel_idle_watch()
        self._idle_watch = self._spawn_task(self._stop_when_idle())

    async def _stop_when_idle(self) -> None:
        """Stop the run once its released item has gone unclaimed for the grace period."""
        await asyncio.sleep(_IDLE_GRACE_S)
        if not self.busy and self._queue_playing():
            await asyncio.sleep(_QUEUE_PLAYING_GRACE_S - _IDLE_GRACE_S)
        if not self.busy:
            self.logger.debug("Nothing more to play, stopping the Spotify session")
            self.mass.create_task(self.backend.discard_run(self, unless_busy=True))

    async def _pin_volume(self, volume: int) -> None:
        """
        Keep the engine at unity volume: the capture has to carry its PCM unaltered.

        :param volume: The volume the engine reports.
        """
        if volume == 100 or (client := self._client) is None or not client.connected:
            return
        # the device is controllable from the Spotify app, Music Assistant sets
        # the player's volume itself
        with suppress(SoloistError, OSError):
            await client.set_volume(100)

    def _pin_options(self, options: SoloistPlaybackOptions) -> None:
        """
        Keep shuffle and repeat off: the engine has to play each item it is given once.

        :param options: The playback options the engine reports.
        """
        self._options_on = options.shuffle or options.repeat != "off"
        if self._options_on and self._options_reset is None:
            # the engine's acks arrive through the event callback, so they are
            # awaited from a task of their own
            self._options_reset = self._spawn_task(self._reset_options())

    async def _reset_options(self) -> None:
        """Switch shuffle and repeat off again for as long as the engine reports one on."""
        try:
            if (client := self._client) is None:
                return
            self.logger.debug("Shuffle or repeat switched on in Spotify, switching it off again")
            while self._options_on:
                # a report during the commands that still shows one on runs them again
                self._options_on = False
                await client.set_repeat_track(False, await_result=True)
                await client.set_repeat_context(False, await_result=True)
                await client.set_shuffle(False, await_result=True)
        except (SoloistError, TimeoutError, OSError) as err:
            # the next report that still shows one on starts over
            self.logger.debug("Could not switch shuffle and repeat off: %r", err)
        finally:
            self._options_reset = None

    def _watch_unclaimed(self, item: _Item) -> None:
        """Stop the run when nobody takes up the item the engine moved on to in time."""
        item.unclaimed_since = asyncio.get_running_loop().time()
        self._spawn_task(self._stop_if_unclaimed(item))

    async def _stop_if_unclaimed(self, item: _Item) -> None:
        """Stop the run when the given item is still unclaimed after the grace period."""
        await asyncio.sleep(_UNCLAIMED_ITEM_S)
        if item.media_key is None and item in (self._item, self._next):
            # the queue went elsewhere: the engine must not play on to nobody on
            # the account's one stream
            self.logger.debug("Nobody took up %s, stopping the Spotify session", item.spotify_uri)
            self.mass.create_task(self.backend.discard_run(self, unless_busy=True))

    def _queue_playing(self) -> bool:
        """Return whether the queue this run serves still plays."""
        queue = self.mass.player_queues.get(self.queue_id) if self.queue_id else None
        return queue is not None and queue.state == PlaybackState.PLAYING

    def _cancel_idle_watch(self) -> None:
        """Keep the run: a request has come for it."""
        if self._idle_watch is not None:
            self._idle_watch.cancel()
            self._idle_watch = None

    async def _log_output(self, proc: AsyncProcess) -> None:
        """Log the daemon's output with the API key redacted."""
        api_key = self.backend._api_key
        async for line in proc.iter_stdout():
            # the third-party binary's own output may echo argv (which carries
            # the api key), so redact it before logging
            text = line.replace(api_key, "<redacted>") if api_key else line
            if _DATA_DIR_BUSY_MARKER in text:
                self._data_dir_busy = True
            if _UNPAIRED_MARKER in text and not self._unpaired:
                await self._check_pairing_lost()
            if _LOGIN_FAILED_MARKER in text and not self._item.started.is_set():
                # only a sign-in refused before the first item started is this
                # run's failure; once it plays, the engine's own recovery (or the
                # stall timeout) decides
                self._fail(
                    "Spotify Soloist could not sign in for this track (the next track "
                    "tries again; check the API key if this keeps happening)"
                )
            self.logger.debug("[soloist] %s", text)

    async def _check_pairing_lost(self) -> None:
        """
        Fail the run when the engine has no stored session left to log in with.

        The engine reports being unpaired while it is still restoring a session
        too, so its report is confirmed against the stored session: acting on
        it alone would fail every playback on a perfectly good pairing.
        """
        if await asyncio.to_thread(self.backend._has_stored_session):
            return
        self._unpaired = True
        self._fail("Spotify is no longer signed in and has to be paired again")

    async def _watch_exit(self, proc: AsyncProcess) -> None:
        """Record the daemon's exit the moment it happens."""
        await proc.wait()
        self._engine_exited = True

    async def _read_capture(self) -> None:
        """
        Read the capture FIFO for the run's whole life and cushion it for the items.

        The pace is the run's clock: the pipe sink applies no rate limit of its
        own, so how fast this reads is how fast the engine plays. Reading
        slightly above realtime is what banks the lead a boundary's crossfade
        uses; reading unpaced makes PulseAudio render silence instead of
        applying backpressure, and the engine runs off the end of its content.
        """
        reader = self._reader
        assert reader is not None
        loop = asyncio.get_running_loop()
        pace_anchor: float | None = None
        paced_bytes = 0
        stalled_for = 0.0
        while not self._stopped:
            # once the engine is gone the FIFO only drains, so a short wait tells
            # an emptied pipe as well as the full slice would
            read_slice = _DRAIN_QUIET_S if self._engine_exited else _READ_SLICE_S
            self._read_waiting = True
            try:
                raw = await asyncio.wait_for(reader.read(_READ_CHUNK_SIZE), read_slice)
            except TimeoutError:
                if self._engine_exited:
                    # the engine exited and its last audio has been read
                    self._finish_deliveries()
                    return
                await self._end_short_of_cut()
                # No data. Either the sink is suspended on purpose (the engine
                # is paused, the cushion is at its cap, or nothing is being
                # served) or the run has died or is stuck rebuffering; only the
                # former is fine. A move that landed since the last slice starts
                # its item on a fresh clock.
                if (
                    self._pace_reset
                    or self._moving
                    or not (self._sink_running or self._rebuffering())
                ):
                    self._pace_reset = False
                    stalled_for = 0.0
                else:
                    stalled_for += _READ_SLICE_S
                    if stalled_for >= _STALL_TIMEOUT_S:
                        self._fail("Spotify stopped sending audio")
                        return
                # Restart the pacing clock rather than carry the gap: making up
                # lost time would mean an unpaced burst, which over-demands the
                # engine's fetch pipeline exactly when it is recovering.
                pace_anchor = None
                continue
            finally:
                self._read_waiting = False
            stalled_for = 0.0
            if not raw:
                # writer end closed: the capture sink is gone (pulse restart)
                self._fail("Spotify playback was interrupted")
                return
            self._last_read_at = loop.time()
            self._raw_total += len(raw)
            if self._engine_exited and not raw.strip(b"\x00"):
                # the engine is gone and the sink only pads: its audio has all arrived
                await self._set_sink(running=False)
                self._finish_deliveries()
                return
            if self._moving:
                # rendered before a seek or jump: nobody's audio
                continue
            if self._pace_reset:
                self._pace_reset = False
                pace_anchor = None
            if pace_anchor is None:
                pace_anchor = loop.time()
                paced_bytes = 0
            paced_bytes += len(raw)
            if not await self._route(raw):
                return
            paced_s = paced_bytes / (_BYTES_PER_SECOND * _PACE_RATE)
            # time lost without a read timeout (a stalled loop, a full cushion) is
            # not made up either: at most the burst is ever read unpaced
            pace_anchor = max(pace_anchor, loop.time() - paced_s)
            if (delay := pace_anchor + paced_s - _PACE_BURST_S - loop.time()) > 0:
                await asyncio.sleep(delay)

    async def _route(self, raw: bytes) -> bool:
        """
        Hand capture bytes to the item they belong to, moving on where an item ends.

        :return: False when the run ended while waiting for cushion space.
        """
        item = self._item
        if item.delivery_done:
            # past the last item the engine was given: nobody's audio
            return True
        if item.end_at is not None and item.raw_read + len(raw) >= item.end_at:
            split = max(0, item.end_at - item.raw_read)
            if split and not await self._deliver(item, raw[:split]):
                return False
            if item is not self._item:
                # a move took over while the capture waited on the cushion
                return True
            raw = raw[split:]
            self._finish_delivery(item)
            if (upcoming := self._next) is None:
                # nothing follows: whatever the engine renders now is not ours
                await self._set_sink(running=False)
                return True
            self._item, self._next = upcoming, None
            if upcoming.abandoned:
                # its stream already let go of it
                await self._go_idle()
                return True
            if upcoming.media_key is None and not item.streaming:
                self._watch_unclaimed(upcoming)
            item = upcoming
            if not raw:
                return True
        return await self._deliver(item, raw)

    async def _deliver(self, item: _Item, raw: bytes) -> bool:
        """
        Shape capture bytes of one item and cushion them for its consumer.

        :return: False when the run ended while waiting for cushion space.
        """
        if (skip := item.align_skip - item.raw_read) > 0:
            item.raw_read += min(skip, len(raw))
            raw = raw[skip:]
        item.raw_read += len(raw)
        self._feed_when_due()
        if item.abandoned or not (chunk := item.shaper.shape(raw)):
            return True
        chunk = self._scrub(item, chunk)
        if chunk and self._engine_exited and chunk.count(0) == len(chunk):
            # the engine is gone: what the sink renders from here on is only
            # padding, so the item's audio has fully arrived
            chunk = b""
        if not chunk:
            return True
        if not self._startup_logged:
            self._startup_logged = True
            self._log_startup_timing(item)
        item.read_bytes += len(chunk)
        return await self._hand_over(item, chunk)

    def _scrub(self, item: _Item, chunk: bytes) -> bytes:
        """Drop the sink's tail padding (the capture shaper already trims the lead)."""
        if (length_ms := item.length_ms) is not None and chunk.count(0) == len(chunk):
            # zeros inside the item's own tail zone are the sink idling while the
            # engine winds down, not content; an item no longer than the zone has
            # no distinguishable tail and its silence is left alone
            target = int((length_ms - item.seek_target_ms) / 1000 * _BYTES_PER_SECOND)
            zone = int(_TAIL_PAD_ZONE_S * _BYTES_PER_SECOND)
            if target > zone and item.read_bytes >= target - zone:
                item.tail_zeros += len(chunk)
                if item.tail_zeros > int(_TAIL_PAD_GRACE_S * _BYTES_PER_SECOND):
                    return b""
        else:
            item.tail_zeros = 0
        return chunk

    async def _hand_over(self, item: _Item, chunk: bytes) -> bool:
        """
        Cushion one chunk for the item's consumer, pausing the engine when it is full.

        :return: False when the run ended while waiting for cushion space.
        """
        if not item.chunks.full():
            item.chunks.put_nowait(chunk)
            return True
        # the consumer is not taking audio (its buffer is at capacity):
        # suspend the sink so the engine pauses instead of overflowing the
        # FIFO, and resume once there is room again
        await self._set_sink(running=False)
        while not self._stopped and self._error is None:
            if self._let_go(item):
                return True
            with suppress(TimeoutError):
                async with asyncio.timeout(_READ_SLICE_S):
                    await item.chunks.put(chunk)
                    break
        else:
            return False
        if self._let_go(item):
            # the room came from a release or a move, which hold the sink themselves
            return True
        # an item whose end is known is owed its tail even from a paused engine
        await self._set_sink(running=self._engine_playing or item.ended)
        return True

    def _let_go(self, item: _Item) -> bool:
        """Return whether the capture no longer owes the item its audio."""
        return item.abandoned or self._moving

    def _finish_delivery(self, item: _Item) -> None:
        """Mark the item's audio as fully handed over."""
        item.delivery_done = True
        # the sentinel wakes a consumer already blocked on an empty cushion;
        # when the cushion is full, the flag alone ends the stream once the
        # consumer has drained it
        with suppress(asyncio.QueueFull):
            item.chunks.put_nowait(None)

    def _finish_deliveries(self) -> None:
        """Mark the audio of the current item, and of the one it moves on to, as fully handed over."""
        for item in (self._item, self._next):
            if item is not None:
                self._finish_delivery(item)

    def _end_item(self, past_end_ms: int = 0) -> None:
        """
        Have the capture end the current item where the engine left it.

        :param past_end_ms: How far the engine reported to be into what follows.
        """
        item = self._item
        self._pending_feed = None
        reported_at = (
            item.raw_read
            + _unread_fifo_bytes(self._transport)
            - round(past_end_ms * _BYTES_PER_SECOND / 1000)
        )
        item.end_at = item.end_offset(reported_at)
        self.logger.debug(
            "%s ends %.3f s into its capture (reported at %.3f s)",
            item.spotify_uri,
            item.end_at / _BYTES_PER_SECOND,
            reported_at / _BYTES_PER_SECOND,
        )

    async def _end_short_of_cut(self) -> None:
        """End the last item the engine was given when the paused engine stopped short of its cut."""
        item = self._item
        if (
            item.end_at is None
            or item.delivery_done
            or self._next is not None
            or not self._engine_paused
            or not self._sink_running
            or self._moving
            # an exact length moves the cut at most this far past the engine's report;
            # a bigger gap is a stalled engine, left to the stall timeout
            or item.end_at - item.raw_read > _MAX_CUT_SKEW_S * _BYTES_PER_SECOND
        ):
            return
        # a paused engine writes nothing more: the rest of the cut never comes
        self.logger.debug(
            "%s ends %.3f s into its capture, short of its cut at %.3f s",
            item.spotify_uri,
            item.raw_read / _BYTES_PER_SECOND,
            item.end_at / _BYTES_PER_SECOND,
        )
        self._finish_delivery(item)
        await self._set_sink(running=False)

    async def _set_sink(self, *, running: bool) -> None:
        """Run the capture sink only while its audio has somewhere to go."""
        async with self._sink_lock:
            if (sink := self._sink) is None or running == self._sink_running:
                return
            try:
                if running:
                    await sink.resume()
                else:
                    await sink.suspend()
            except Exception as err:
                # fail closed: a sink with unknown suspend state would leak stall
                # silence into (or withhold audio from) the delivered PCM
                self._fail(f"Spotify playback could not be controlled: {err}")
                return
            self._sink_running = running

    async def _run_events(self, client: SoloistClient, client_ready: asyncio.Event) -> None:
        """Keep the WebSocket client connected and feed its events into the run state."""
        proc = self._proc
        assert proc is not None
        if not await client.wait_until_ready(_STARTUP_TIMEOUT_S):
            # an exit right at startup still has to release the waiters
            if not self._engine_exited and proc.returncode is None:
                self._fail("Spotify playback did not start up correctly")
            client_ready.set()
            return
        self._endpoint_at = asyncio.get_running_loop().time()
        client_ready.set()
        while not self._stopped:
            try:
                await client.listen_events(self._handle_event)
            except asyncio.CancelledError:
                raise
            except Exception as err:
                if self._engine_exited or proc.returncode is not None:
                    # the daemon exited; the socket dying with it is not an error
                    return
                self.logger.debug("Soloist event connection lost, reconnecting: %s", err)
                await asyncio.sleep(_CONNECT_POLL_S)

    async def _handle_event(self, event: SoloistEvent) -> None:
        """Track what the engine is doing with this run's items."""
        data = event.data
        if isinstance(data, SoloistAuthState):
            self._logged_in = data.logged_in
            if data.logged_in and self._login_at is None:
                self._login_at = asyncio.get_running_loop().time()
            if data.logged_in is False and not self._unpaired:
                await self._check_pairing_lost()
            return
        if isinstance(data, SoloistDeviceChanged):
            self._observe_device_active(active=data.is_active)
            return
        if isinstance(data, SoloistQueueChanged):
            self._observe_queue(data)
            return
        if isinstance(data, SoloistVolumeChanged):
            await self._pin_volume(data.volume)
            return
        if isinstance(data, SoloistOptionsChanged):
            self._pin_options(data.options)
            return
        if isinstance(data, SoloistTrackChanged):
            if data.item is not None and data.item.uri:
                self._observe_engine_item(data.item.uri, announced=True)
            return
        if isinstance(data, SoloistPositionSync):
            self._observe_position(data.position.position_ms)
            return
        if isinstance(data, SoloistPlaybackState):
            if data.volume is not None:
                await self._pin_volume(data.volume)
            if data.options is not None:
                self._pin_options(data.options)
            playing = data.status == "playing"
            if data.item is not None and data.item.uri:
                self._observe_engine_item(data.item.uri, announced=playing, details=data.item)
                if (
                    (fed := self._fed) is not None
                    and fed.left
                    and playing
                    and not self._moving
                    and data.item.uri == fed.spotify_uri
                    and data.position is not None
                    and data.position.position_ms < _RESTART_POSITION_MS
                    and self._item.in_tail
                ):
                    # the same track queued after itself: only its restart from
                    # the end of the current occurrence tells
                    self._consume_fed(data.position.position_ms)
            if data.position is not None:
                self._observe_position(data.position.position_ms)
            self._engine_playing = playing
            was_paused, self._engine_paused = self._engine_paused, data.status == "paused"
            if self._engine_paused and data.position is not None and data.position.position_ms == 0:
                self._observe_rewound_pause()
            # the engine repeats a pause across events: act on the transition only
            if self._engine_paused and not was_paused:
                self._observe_external_pause()
            if self._capturing():
                # pause silence stays out of the delivered PCM; the cushion gate
                # takes priority over resuming
                if playing and not self._item.chunks.full():
                    await self._set_sink(running=True)
                elif not playing and proc_running(self._proc):
                    await self._set_sink(running=False)

    def _observe_engine_item(
        self, uri: str, *, announced: bool, details: SoloistEntity | None = None
    ) -> None:
        """
        Record the engine reaching an item.

        :param uri: The Spotify URI the engine reports.
        :param announced: Whether the report says the engine moved to (or plays) it.
        :param details: The engine's description of the item, when it sent one.
        """
        self._engine_uri = uri
        if details is not None and (duration_ms := _decorated_duration_ms(details)):
            # only the full playback state carries the exact length
            self._engine_length = (uri, duration_ms)
            for candidate in (self._next, self._item):
                if candidate is not None and candidate.spotify_uri == uri:
                    if candidate.engine_duration_ms is None:
                        candidate.engine_duration_ms = duration_ms
                    break
        if self._moving:
            if announced and uri == self._move_target:
                self._move_landed.set()
            # an in-place seek announces the current item, which may be the
            # queued one too: only a skip onto the queued item consumes it
            if (
                self._skipping_to_fed
                and self._fed is not None
                and uri == self._fed.spotify_uri
                and announced
            ):
                self._consume_fed()
            return
        item = self._item
        if not item.started.is_set():
            # the account's previous item is announced before the play command lands
            if uri == item.spotify_uri and announced and self._play_sent:
                if self._playing_at is None:
                    self._playing_at = asyncio.get_running_loop().time()
                item.started.set()
            return
        if uri in (item.spotify_uri, self._next and self._next.spotify_uri):
            return
        if announced and self._fed is not None and uri == self._fed.spotify_uri:
            self._consume_fed()
            return
        if (refused := self._unplayed_fed_item()) is not None:
            # the engine left the track it was fed before any of it played: Spotify
            # refused it, and the engine moved on to another track
            self._refuse(refused)
            return
        if not announced or item.ended:
            return
        if self._fed is not None and self._fed.uid is not None and not self._fed.left:
            self._fail(
                "Spotify is already streaming somewhere else - an account "
                "can only play in one place at a time"
            )
            return
        self._fed = None
        # past the last item it was given the engine wandered on by itself
        # (autoplay): this item is over, and what renders now is not its audio
        self.logger.debug("Engine wandered to %s; %s has ended", uri, item.spotify_uri)
        self._end_item()

    def _observe_queue(self, data: SoloistQueueChanged) -> None:
        """Follow the item queued on the engine: its entry leaving means the engine is on it."""
        entries = [entry for entry in data.upcoming if entry.source == "queue"]
        uids = {entry.uid for entry in entries}
        if (fed := self._fed) is not None:
            if fed.uid is None:
                # the engine acknowledges neither the add nor a refusal of it:
                # the entry stays unconfirmed until a report lists it
                fed.uid = next(
                    (
                        entry.uid
                        for entry in entries
                        if entry.uid not in self._queue_uids
                        and entry.item is not None
                        and entry.item.uri == fed.spotify_uri
                    ),
                    None,
                )
            elif fed.uid not in uids:
                # the engine reporting it plays the item confirms it was started
                fed.left = True
        self._queue_uids = uids

    def _feed_due(self) -> bool:
        """Return whether the current item is close enough to its end to queue what follows."""
        item = self._item
        return (
            item.engine_duration_ms is None
            or item.engine_duration_ms - item.position_ms <= _FEED_LEAD_S * 1000
        )

    def _fed_too_early_for(self, spotify_uri: str, position_ms: int) -> bool:
        """Return whether the item queued on the engine is too far ahead of this track position."""
        fed = self._fed
        # only an entry confirmed and still queued can be skipped onto; another stays tracked
        if fed is None or fed.uid is None or fed.left:
            return False
        # the engine may last have described the track skipped through on a seek back
        length_ms = self._item.engine_duration_ms if self._item.spotify_uri == spotify_uri else None
        if length_ms is None and (known := self._engine_length) and known[0] == spotify_uri:
            length_ms = known[1]
        return length_ms is not None and length_ms - position_ms > _FEED_LEAD_S * 1000

    def _feed_when_due(self) -> None:
        """Queue the held item on the engine once the current one is in its last seconds."""
        if (
            self._pending_feed is None
            or (media_key := self._item.media_key) is None
            or not self._feed_due()
        ):
            return
        spotify_uri, self._pending_feed = self._pending_feed, None
        # the capture must not wait on the engine
        self._spawn_task(self._feed_held(media_key, spotify_uri))

    async def _feed_held(self, media_key: str, spotify_uri: str) -> None:
        """Queue a held item on the engine, as a hint arriving now would."""
        # under the run lock, like every hint: a move must not cross the add
        async with self.backend._run_lock:
            await self.feed_next(media_key, spotify_uri)

    def _consume_fed(self, position_ms: int = 0) -> None:
        """
        Move on to the queued item the engine has reached.

        :param position_ms: How far the engine reported to be into that item.
        """
        if (fed := self._fed) is None:
            return
        self._fed = None
        if self._moving:
            self._fed_consumed.set()
            return
        if self._item.ended:
            return
        upcoming = self._new_item(fed.spotify_uri, 0, switched=True)
        upcoming.fed = True
        upcoming.started.set()
        self._next = upcoming
        self._end_item(position_ms)

    def _unplayed_fed_item(self) -> _Item | None:
        """Return the item the engine moved on to from its queue, while none of its audio arrived."""
        item = self._next or self._item
        if item.fed and not item.read_bytes and item.error is None and not item.ended:
            return item
        return None

    def _refuse(self, item: _Item) -> None:
        """
        Fail the fed item Spotify would not play; the run stays for the item after it.

        :param item: The fed item the engine left before playing any of it.
        """
        self.logger.debug(
            "Spotify refused %s, the engine went to %s", item.spotify_uri, self._engine_uri
        )
        self.backend._note_refusal(item.spotify_uri)
        item.error = _REFUSED_MESSAGE
        if item is self._next:
            # the capture stops where the item before it ends
            self._next = None
        else:
            self._end_item()
        self._release_item_waiters(item)

    def _new_item(self, spotify_uri: str, seek_position_ms: int, *, switched: bool) -> _Item:
        """
        Return the state of an item the engine moves to.

        :param spotify_uri: Canonical Spotify URI of the item.
        :param seek_position_ms: Position the item starts from.
        :param switched: Whether the engine switched tracks to get there.
        """
        item = _Item(
            spotify_uri,
            seek_position_ms,
            None,
            max_lead_trim_s=_MAX_SWITCH_TRIM_S if switched else _MAX_LEAD_TRIM_S,
            max_counted_lead_s=_MAX_SWITCH_GAP_S if switched else None,
        )
        if self._engine_length is not None and self._engine_length[0] == spotify_uri:
            item.engine_duration_ms = self._engine_length[1]
        return item

    def _observe_rewound_pause(self) -> None:
        """End the current item when the engine has run past the last item it was given."""
        if (
            not self._moving
            and (refused := self._unplayed_fed_item()) is not None
            and self._engine_uri != refused.spotify_uri
        ):
            # back at the head of its context, which can be the item before the fed
            # one, before any of the fed track played: Spotify refused it
            self._refuse(refused)
            return
        item = self._item
        if self._moving or item.ended or not item.started.is_set():
            return
        # anywhere but at its end, a pause at 0:00 is somebody else's
        if item.in_tail:
            # the engine stopped instead of moving on: whatever was queued on
            # it never came to play
            self._fed = None
            self._end_item()

    def _observe_device_active(self, *, active: bool) -> None:
        """Fail the run when the account starts playing somewhere else."""
        # the engine reports itself active as the run starts, and a run that is
        # stopping is only shedding the device on its way out: another player
        # took the account over when the engine loses it. Auth and playback
        # states carry the same flag but report False during startup.
        if active or self._engine_exited or self._stopped or not self._item.started.is_set():
            return
        if not self._item_live():
            # nothing is being streamed: stand down quietly
            self.mass.create_task(self.backend.discard_run(self, unless_busy=True))
            return
        self._fail(
            "Spotify is already streaming somewhere else - an account "
            "can only play in one place at a time"
        )

    def _observe_external_pause(self) -> None:
        """Stop the queue once a pause proves to come from outside Music Assistant."""
        if not self._item_live():
            return
        # a pause taken back and repeated gets a fresh grace, not the old deadline
        if self._pause_watch is not None:
            self._pause_watch.cancel()
        self._pause_watch = self._spawn_task(self._stop_queue_if_still_paused())

    async def _stop_queue_if_still_paused(self) -> None:
        """Stop the queue when the engine neither resumed nor moved on after a pause."""
        await asyncio.sleep(_EXTERNAL_PAUSE_GRACE_S)
        if not self._engine_paused or not self._item_live() or (queue_id := self.queue_id) is None:
            return
        # only the Spotify app or Web API pauses a live engine, and a paused engine
        # stalls the stream; a stop keeps the resume position, so play restarts at it
        self.logger.info("Spotify paused %s, stopping queue %s", self.spotify_uri, queue_id)
        self.mass.create_task(self.mass.player_queues.stop(queue_id))

    def _capturing(self) -> bool:
        """Return whether the capture is running for an item (claimed or not)."""
        item = self._item
        return (
            # still starting up (or seeking): no audio has been released yet
            self._reader is not None
            and not self._stopped
            and not self._moving
            and self._error is None
            and item.started.is_set()
            and not item.ended
            and not item.abandoned
        )

    def _item_live(self) -> bool:
        """Return whether this run is delivering an item to a stream."""
        return (
            self._capturing()
            and self._item.streaming
            and not self._engine_exited
            and not self._item.delivery_done
        )

    def _rebuffering(self) -> bool:
        """Return whether the engine owes the item being streamed audio it is not sending."""
        return self._item_live() and not self._engine_paused and not self._item.chunks.full()

    def _observe_position(self, position_ms: int) -> None:
        """Confirm an armed seek once the engine reports at its target."""
        if (
            self._seek_target_ms is not None
            and abs(position_ms - self._seek_target_ms) <= _SEEK_CONFIRM_GRACE_MS
        ):
            self._seek_landed.set()

    def _log_startup_timing(self, item: _Item) -> None:
        """Log how long each startup stage took, measured from the spawn (never the argv)."""
        now = asyncio.get_running_loop().time()

        def since(stamp: float | None) -> str:
            return f"{stamp - self._spawned_at:.2f}s" if stamp is not None else "n/a"

        self.logger.debug(
            "Spotify session started for %s: endpoint %s, login %s, playing %s, first audio %s",
            item.spotify_uri,
            since(self._endpoint_at),
            since(self._login_at),
            since(self._playing_at),
            since(now),
        )


def proc_running(proc: AsyncProcess | None) -> bool:
    """Return whether the daemon process is still alive."""
    return proc is not None and proc.returncode is None


def _decorated_duration_ms(item: object) -> int | None:
    """Return the item duration the engine reports in its playback decorations."""
    decorations = getattr(item, "decorations", None)
    if not isinstance(decorations, dict):
        return None
    playback = decorations.get("playback")
    if not isinstance(playback, dict):
        return None
    duration_ms = playback.get("duration_ms")
    return int(duration_ms) if isinstance(duration_ms, int | float) else None


class _CaptureShaper:
    """
    Turns raw FIFO reads into whole sample frames of real audio.

    Two jobs. It drops the digital silence the capture holds before an item's
    first decoded sample (bounded, see ``_MAX_LEAD_TRIM_S``). And it carries a
    partial frame across reads: ``StreamReader.read`` returns whatever is
    available, which is not always a whole number of frames, so without this an
    item change between two reads would end one item mid-frame and start the
    next on the remainder - swapping its channels.
    """

    def __init__(self, max_lead_trim_s: float = _MAX_LEAD_TRIM_S) -> None:
        """
        Initialize the shaper for one item.

        :param max_lead_trim_s: How much leading digital silence may be dropped.
        """
        self._max_lead_trim = int(max_lead_trim_s * _BYTES_PER_SECOND)
        self._lead_skipped = 0
        self._first_audio_seen = False
        self._carry = b""

    @property
    def lead_skipped(self) -> int:
        """Return how many bytes of pre-roll were dropped."""
        return self._lead_skipped

    def shape(self, chunk: bytes) -> bytes:
        """
        Return the next whole frames of audio, or empty when there are none yet.

        :param chunk: The bytes just read from the capture FIFO.
        """
        if self._carry:
            chunk = self._carry + chunk
            self._carry = b""
        if not self._first_audio_seen:
            chunk, skipped = _trim_lead_silence(chunk, self._lead_skipped, self._max_lead_trim)
            self._lead_skipped += skipped
            if not chunk.lstrip(b"\x00"):
                # still nothing but pre-roll: hold what was left of the frame so
                # the next read continues on the same grid
                self._carry = chunk
                return b""
            self._first_audio_seen = True
        if remainder := len(chunk) % _FRAME_BYTES:
            self._carry = chunk[-remainder:]
            return chunk[:-remainder]
        self._carry = b""
        return chunk


async def _cancel_and_join(tasks: list[asyncio.Task[None]]) -> None:
    """Cancel the given tasks and wait for them to finish."""
    for task in tasks:
        task.cancel()
    for task in tasks:
        with suppress(asyncio.CancelledError, Exception):
            await task


async def _open_fifo_reader(
    fifo_path: Path,
) -> tuple[asyncio.StreamReader, asyncio.ReadTransport]:
    """Open the sink's FIFO for non-blocking reads with a small buffer limit."""
    loop = asyncio.get_running_loop()
    fd = os.open(fifo_path, os.O_RDONLY | os.O_NONBLOCK)
    try:
        pipe_file = os.fdopen(fd, "rb", buffering=0)
    except OSError:
        os.close(fd)
        raise
    reader = asyncio.StreamReader(limit=_READ_CHUNK_SIZE * 2)
    try:
        transport, _ = await loop.connect_read_pipe(
            partial(asyncio.StreamReaderProtocol, reader), pipe_file
        )
    except BaseException:
        pipe_file.close()
        raise
    return reader, transport


def _trim_lead_silence(
    chunk: bytes,
    already_skipped: int,
    max_lead_trim: int = int(_MAX_LEAD_TRIM_S * _BYTES_PER_SECOND),
) -> tuple[bytes, int]:
    """
    Drop leading digital silence from the head of an item (bounded).

    :param chunk: The chunk read from the FIFO.
    :param already_skipped: Silence bytes dropped from earlier chunks.
    :param max_lead_trim: The most silence, in bytes, that may be dropped.
    :return: The (possibly emptied/shortened) chunk and how many bytes were dropped.
    """
    stripped = chunk.lstrip(b"\x00")
    if not stripped:
        if already_skipped + len(chunk) <= max_lead_trim:
            # whole frames only: the offset below places the first sample on the
            # session's frame grid, which holds only while everything dropped
            # before it was a whole number of frames. The leftover bytes go back
            # to the caller, which carries them into the next read.
            dropped = len(chunk) // _FRAME_BYTES * _FRAME_BYTES
            return chunk[dropped:], dropped
        # budget exhausted: this is genuine silence content, not infrastructure
        return chunk, 0
    # Keep sample-frame alignment when the audio starts mid-chunk, and never trim
    # past the budget: whatever silence is left by then is content, not pre-roll.
    remaining = max(0, max_lead_trim - already_skipped)
    offset = min(len(chunk) - len(stripped), remaining) // _FRAME_BYTES * _FRAME_BYTES
    return chunk[offset:], offset


def _unread_fifo_bytes(transport: asyncio.ReadTransport | None) -> int:
    """Return how many bytes wait unread in the capture FIFO (0 when it cannot be told)."""
    pipe = transport.get_extra_info("pipe") if transport is not None else None
    if pipe is None:
        return 0
    unread = array.array("i", [0])
    try:
        fcntl.ioctl(pipe.fileno(), termios.FIONREAD, unread, True)
    except OSError, ValueError:
        return 0
    return int(unread[0])
