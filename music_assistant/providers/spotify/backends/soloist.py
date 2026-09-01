"""
Spotify Soloist playback backend for the Spotify music provider.

Runs one continuous session of ``soloist``, Spotify's official headless client,
and feeds it one track ahead so the engine plays consecutive tracks without a
break. The session renders into a private PulseAudio capture sink whose FIFO is
read back slightly above realtime pace, and that one continuous audio stream is
handed to Music Assistant as ordinary per-item streams: an item's stream ends
where the session moves on to the next track, and the next item's stream begins
there. Played back to back the items reproduce the session's audio sample for
sample. The engine itself never crossfades — Music Assistant mixes the queue's
crossfade, so every item's audio starts at its first sample and stays aligned
with its analysis.

SECURITY NOTE: the daemon takes the user's personal API key on its command
line; nothing in this module may ever log the process argv.

Shared infrastructure (binary manager, WebSocket client, audio prefs, pulse
capture) is owned by the Spotify Connect provider / core helpers and reused here.
"""

from __future__ import annotations

import asyncio
import os
import shutil
from contextlib import suppress
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Final, NoReturn

from music_assistant_models.enums import ContentType, MediaType
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
    SoloistPlaybackState,
    SoloistPositionSync,
    SoloistTrackChanged,
)

from .base import SpotifyPlaybackBackend, StreamSupersededError

if TYPE_CHECKING:
    import logging
    from collections.abc import AsyncGenerator

    from music_assistant_models.streamdetails import StreamDetails

    from music_assistant.helpers.json import SerializableType
    from music_assistant.helpers.pulse_capture import PulseCaptureServer
    from music_assistant.providers.spotify.provider import SpotifyProvider
    from music_assistant.providers.spotify_connect.soloist.runtime import SoloistEvent

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
# how far short of an item's duration its delivered PCM may end before it is
# rejected as incomplete (an engine that refuses an unavailable item exits
# within moments; reported durations are approximate)
_SHORT_DELIVERY_TOLERANCE_MS: Final[int] = 10000
# The elastic cushion between the paced FIFO reader and the consumer. A full
# cushion suspends the capture sink, which pauses the engine: the FIFO itself
# holds well under a second, so the reader must keep draining it whenever the
# consumer (the item's buffer, at its memory-tiered capacity) stops taking audio.
_RUN_CUSHION_S: Final[float] = 10.0
# how close to its target a position report has to come to confirm a seek
_SEEK_CONFIRM_GRACE_MS: Final[int] = 3000

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
# how long the log reader is given to catch up on a daemon's parting words
_LOG_DRAIN_TIMEOUT_S: Final[float] = 2.0


class SoloistSessionBusyError(ProviderStreamLimitError):
    """
    Raised when the one Soloist session is delivering a different item.

    A ProviderStreamLimitError so a speculative prepare gives up softly and the
    item is not marked unplayable, but with a message of its own: the engine
    allows a single session, which is not the same thing as the provider's
    source-stream budget (a handover legitimately holds two streams against it).
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
    Fetches Spotify audio with one ``soloist --single-track`` engine run per item.

    Requires a stored paired session in the per-instance data directory
    (provisioned by the setup flow via ``soloist --pair``).
    """

    _server: PulseCaptureServer | None = None
    _binary: Path | None = None
    _run: _SingleTrackRun | None = None

    def __init__(self, provider: SpotifyProvider) -> None:
        """
        Initialize the backend.

        :param provider: The owning Spotify provider instance.
        """
        super().__init__(provider)
        # Guards every write of _run AND every run teardown.
        # The engine allows one daemon per data directory, so a replacement can
        # only be spawned once the previous one is gone — holding this across
        # the teardown is what sequences that.
        self._run_lock = asyncio.Lock()

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

        None when no session serves that queue, in which case the configuration is
        the only thing to go on.

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
        Yield the PCM audio for one Spotify URI as its own single-track run.

        :param spotify_uri: Canonical Spotify URI (``spotify:track:<id>`` or
            ``spotify:episode:<id>``).
        :param seek_position: Position in seconds to start from.
        :param streamdetails: The StreamDetails this audio is requested for.
        :param continuation: Ignored: every URI is its own engine run, so a
            later chapter needs no special handling.
        """
        if self._server is None or self._binary is None:
            raise AudioError("Spotify Soloist backend is not started")
        run = await self._acquire_run(
            spotify_uri, seek_position, streamdetails, continuation=continuation
        )
        try:
            async for chunk in run.stream():
                yield chunk
        finally:
            await run.stop()
            async with self._run_lock:
                if self._run is run:
                    self._run = None

    async def discard_run(self, run: _SingleTrackRun) -> None:
        """
        Stop a run for good, dropping it if it is still the current one.

        The teardown happens under the run lock, not after it: the engine
        refuses to start while another daemon still holds its data directory, so
        a replacement must not be spawned until this one is gone.

        :param run: The run to tear down.
        """
        async with self._run_lock:
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
        }

    async def _acquire_run(
        self,
        spotify_uri: str,
        seek_position: int,
        streamdetails: StreamDetails | None,
        *,
        continuation: bool,
    ) -> _SingleTrackRun:
        """
        Start the engine run for this item, replacing one this request supersedes.

        The engine allows one daemon per data directory (and Spotify one stream
        per account), so a run still serving another stream is reported as
        capacity: a speculative prepare gives up softly and the real request,
        made once that stream has been released, gets the slot.

        :raises StreamSupersededError: When a continuation finds a run for the
            same item already started by the stream that replaced it.
        """
        media_key = streamdetails.uri if streamdetails is not None else None
        waited = False
        while True:
            async with self._run_lock:
                if (run := self._run) is None:
                    # cheap thanks to the shared verify cache; swaps in a fresh build
                    # when the installed one is nearing its 90-day expiry
                    try:
                        self._binary = await SoloistBinaryManager(self.mass).ensure_fresh(
                            self._consent
                        )
                    except SoloistError as err:
                        raise AudioError(f"Spotify Soloist binary unavailable: {err}") from err
                    fresh = _SingleTrackRun(self, spotify_uri, seek_position * 1000, streamdetails)
                    try:
                        await fresh.start()
                    except BaseException:
                        await fresh.stop()
                        raise
                    self._run = fresh
                    return fresh
                if run.media_key != media_key or media_key is None:
                    raise SoloistSessionBusyError(self.provider)
                if continuation and run.spotify_uri != spotify_uri:
                    # this stream was replaced (a seek of the same item started a
                    # fresh run) before it could continue into its next chapter -
                    # a run it must not take back
                    raise StreamSupersededError(f"The stream of {spotify_uri} was replaced")
                if seek_position:
                    # a positive seek can only target the item being delivered (a
                    # prefetch never seeks): the run restarts at the target
                    await run.stop()
                    self._run = None
                    continue
            if waited:
                raise SoloistSessionBusyError(self.provider)
            # Same item, no seek, run still held. A restart-from-zero races the
            # release of the stream it replaces, so give that release a moment -
            # but never steal a held run: a second queue occurrence of the same
            # track asks with these same details, and stopping its playing twin
            # would cut it mid-track.
            waited = True
            await self._wait_run_released(run)

    async def _wait_run_released(self, run: _SingleTrackRun, timeout: float = 2.0) -> None:
        """Wait briefly for the given run to be released by the stream holding it."""
        deadline = asyncio.get_running_loop().time() + timeout
        while self._run is run and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.05)

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

    def _run_for(self, streamdetails: StreamDetails) -> _SingleTrackRun | None:
        """
        Return the run serving this very item, if one is.

        :param streamdetails: Stream details of the item being asked about.
        """
        run = self._run
        if run is None or run.media_key != streamdetails.uri:
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
        # delivered audio lines up with its analysis (waveform, beat grid, light sync)
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

    def _session_args(self, spotify_uri: str) -> list[str]:
        """
        Build the argv for one single-track engine run.

        SECURITY: the argv carries the user's API key — it must never be logged
        or end up in any error message.
        """
        assert self._binary is not None
        return [
            str(self._binary),
            # play exactly this item and exit when it finishes: the stored
            # session is restored without advertising a Spotify Connect device,
            # and shuffle/repeat start disabled
            "--single-track",
            spotify_uri,
            # required by the binary even though single-track mode never
            # advertises it anywhere
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


class _SingleTrackRun:
    """
    One engine run playing exactly one Spotify URI, streamed as it renders.

    Single-track mode starts the stored session without advertising a Spotify
    Connect device, plays the one URI with shuffle and repeat off, and exits
    when the item finishes. The capture FIFO is reader-clocked: it is read at
    ``_PACE_RATE`` into a small bounded cushion whose backpressure suspends the
    capture sink, which is what pauses the engine when the consumer stops
    taking audio.
    """

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
        :param spotify_uri: Canonical Spotify URI the engine is to play.
        :param seek_position_ms: Position to start from, in milliseconds.
        :param streamdetails: The StreamDetails the audio is requested for.
        """
        self.backend = backend
        self.mass = backend.mass
        self.logger: logging.Logger = backend.logger
        self.spotify_uri = spotify_uri
        self.media_key = streamdetails.uri if streamdetails is not None else None
        self.queue_id = streamdetails.queue_id if streamdetails is not None else None
        # what the engine was actually told at spawn, which is what the streams
        # core has to agree with - the setting may be toggled while this plays
        self.engine_normalizes = False
        self._seek_target_ms = seek_position_ms
        self._duration_ms: int | None = (
            streamdetails.duration * 1000
            if streamdetails is not None and streamdetails.duration
            else None
        )
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
        self._item_over = False
        self._engine_exited = False
        self._teardown_done = False
        self._engine_playing = False
        self._sink_running = False
        self._sink_lock = asyncio.Lock()
        # the engine reported it is playing this run's uri
        self._started = asyncio.Event()
        self._seek_confirmed = asyncio.Event()
        self._position_ms: int | None = None
        # the elastic cushion between the paced reader and the consumer; a full
        # cushion suspends the sink, which pauses the engine
        cushion_chunks = max(2, int(_RUN_CUSHION_S * _BYTES_PER_SECOND / _READ_CHUNK_SIZE))
        self._chunks: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=cushion_chunks)
        self._delivery_done = False
        self._delivered = 0
        self._read_bytes = 0
        self._tail_zeros = 0

    async def start(self) -> None:
        """Spawn the engine on this run's URI and get its audio flowing."""
        backend = self.backend
        server = backend._server
        assert server is not None
        assert backend._binary is not None
        self.engine_normalizes = self._engine_normalization_enabled()
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
            backend._session_args(self.spotify_uri),
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
            self._raise_startup_error("did not connect and log in")
        if self._error or not client.connected:
            self._raise_startup_error("published no usable WebSocket endpoint")
        await self._await_playback_started(proc)
        if self._seek_target_ms:
            await self._cold_seek(client, self._seek_target_ms)
        # the reader must be attached before the sink starts producing, or the
        # sink's first writes go to a reader-less FIFO and are dropped
        self._reader, self._transport = await _open_fifo_reader(sink.fifo_path)
        self._spawn_task(self._watch_exit(proc))
        self._spawn_task(self._read_capture())
        await self._set_sink(running=True)

    async def stream(self) -> AsyncGenerator[bytes]:
        """Yield the run's PCM audio, ending where the engine ended the item."""
        while True:
            # the flag ends a delivery whose sentinel found the cushion full:
            # only an empty cushion can leave the consumer blocked below, and
            # an empty cushion always has room for the sentinel
            if self._delivery_done and self._chunks.empty():
                break
            if (chunk := await self._chunks.get()) is None:
                break
            self._delivered += len(chunk)
            yield chunk
        if self._error is not None:
            raise AudioError(self._error)
        self._validate_delivery()

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
        if (self._engine_exited or self._item_over) and (proc := self._proc) is not None:
            # The engine exits on its own at its item's end (a wander event can
            # precede the exit by a moment): reap it before anything else, so
            # close() below finds a returncode and returns right away. Closing
            # an unreaped process instead silently waits out its stream-lock
            # and flush budgets - ten seconds on every natural track end.
            with suppress(Exception):
                await asyncio.wait_for(proc.wait(), 5)
        await _cancel_and_join(self._tasks)
        self._tasks.clear()
        if self._transport is not None:
            self._transport.close()
            self._transport = None
        if (proc := self._proc) is not None:
            # Closed straight away, with no grace period for a natural exit: on
            # an aborted stream the engine is mid-item and never quits on its
            # own; after a natural end it has already exited and close() only
            # reaps it. The log reader stays alive across the close: nothing
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

    def _spawn_task(self, coro: object) -> None:
        """Track a run-scoped task so stop() can cancel and join it."""
        task: asyncio.Task[None] = asyncio.create_task(coro)  # type: ignore[arg-type]
        task.add_done_callback(self._task_done)
        self._tasks.append(task)

    def _task_done(self, task: asyncio.Task[None]) -> None:
        """Fail the run when one of its tasks died of an unexpected error."""
        if task.cancelled() or (err := task.exception()) is None:
            return
        self.logger.error("Spotify Soloist task failed: %s", err, exc_info=err)
        self._fail(f"task failed: {err}")

    def _fail(self, message: str) -> None:
        """Record a fatal error, unblock the consumer and tear the run down."""
        if self._error is not None or self._stopped:
            return
        self._error = message
        self._release_waiters()
        # the teardown runs as its own task: it cancels the very tasks this is
        # called from, and the daemon has to go either way
        self.mass.create_task(self.backend.discard_run, self)

    def _release_waiters(self) -> None:
        """Unblock everything waiting on this run: startup, seek and the consumer."""
        self._started.set()
        self._seek_confirmed.set()
        if self._item_over and self._error is None:
            # a cleanly ended run has marked its delivery done; the consumer is
            # still entitled to the cushioned tail and ends once it drains
            return
        # a failed or aborted run's cushion holds audio nobody will take anymore;
        # the sentinel has to reach the consumer either way
        while True:
            try:
                self._chunks.get_nowait()
            except asyncio.QueueEmpty:
                break
        with suppress(asyncio.QueueFull):
            self._chunks.put_nowait(None)

    def _validate_delivery(self) -> None:
        """
        Raise when the engine ended the item long before its own duration.

        An engine that refuses an item (unavailable to the account or region)
        exits within moments; crediting that as a completed stream would mark
        the track as played and hide the real cause.
        """
        if self._stopped or self._duration_ms is None:
            return
        delivered_ms = self._delivered / _BYTES_PER_SECOND * 1000 + self._seek_target_ms
        if delivered_ms < self._duration_ms - _SHORT_DELIVERY_TOLERANCE_MS:
            raise AudioError(
                f"Spotify Soloist delivered incomplete audio for {self.spotify_uri} "
                f"(reached {int(delivered_ms)}ms of {self._duration_ms}ms)"
            )

    def _engine_normalization_enabled(self) -> bool:
        """
        Return whether the engine should normalize the loudness it delivers.

        The player's own volume normalization switch decides first: turning it
        off means nobody normalizes, not that the job passes to Spotify.
        """
        if not self.backend.provider.spotify_normalization_configured:
            return False
        if self.queue_id is None:
            # nothing to read the switch from, so the provider option stands
            return True
        return (
            self.mass.config.get_effective_player_queue_config_value(
                self.queue_id, CONF_VOLUME_NORMALIZATION, CONF_VALUE_ENABLED
            )
            != CONF_VALUE_DISABLED
        )

    async def _await_playback_started(self, proc: AsyncProcess) -> None:
        """Wait until the engine reports it is playing this run's item."""
        try:
            async with asyncio.timeout(_STARTUP_TIMEOUT_S):
                exit_task = asyncio.ensure_future(proc.wait())
                started_task = asyncio.ensure_future(self._started.wait())
                try:
                    await asyncio.wait(
                        {exit_task, started_task}, return_when=asyncio.FIRST_COMPLETED
                    )
                finally:
                    exit_task.cancel()
                    started_task.cancel()
        except TimeoutError:
            self._raise_startup_error("timed out waiting for playback to start")
        if self._error or not self._started.is_set():
            if proc.returncode is not None:
                # let the log reader catch up, so the daemon's own complaint can
                # be reported instead of a generic startup failure
                await self._drain_log()
            if proc.returncode == EXIT_CODE_BUILD_EXPIRED:
                # an expired build exits with code 10 right at spawn
                await self._handle_expired_build()
            self._raise_startup_error("exited before playback started")

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
        # a pairing that never logged in is checked first: it also fails the
        # run, and its recovery (back through the setup flow) beats failing
        # every track with a generic error
        if self._unpaired or self._logged_in is False:
            # the stored session no longer logs in: route the user through the
            # setup flow instead of failing every item (mirrors librespot's
            # INVALID_CREDENTIALS handling)
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
            raise AudioError(f"Spotify Soloist failed: {self._error}")
        raise AudioError(f"Spotify Soloist {detail} for {self.spotify_uri}")

    async def _cold_seek(self, client: SoloistClient, target_ms: int) -> None:
        """
        Seek the engine to the target position before any PCM is released.

        The sink is still suspended, so no pre-seek audio enters the FIFO; PCM
        demand only starts once a position report confirms the seek landed.
        """
        # the engine silently drops a seek that arrives while the track is still
        # loading (verified via event trace), so re-send it until a position
        # report confirms it landed
        deadline = asyncio.get_running_loop().time() + _SEEK_CONFIRM_TIMEOUT_S
        while True:
            await client.seek(target_ms)
            with suppress(TimeoutError):
                async with asyncio.timeout(_SEEK_RETRY_INTERVAL_S):
                    await self._seek_confirmed.wait()
            if self._seek_confirmed.is_set():
                if self._error:
                    raise AudioError(f"Spotify Soloist failed: {self._error}")
                return
            if asyncio.get_running_loop().time() >= deadline:
                raise AudioError(f"Spotify Soloist did not confirm seeking to {target_ms}ms")

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
        self._fail("the stored session is gone")

    async def _watch_exit(self, proc: AsyncProcess) -> None:
        """
        Record the daemon's exit the moment it happens.

        The engine exits when its item finishes, and the sink renders silence from
        then on: the reader needs a signal that does not depend on the process
        being reaped, or the item's end is only found by wading through that
        silence - the tail-zone budget late, every track.
        """
        await proc.wait()
        self._engine_exited = True

    async def _read_capture(self) -> None:
        """
        Read the capture FIFO for the run's whole life and cushion it for the consumer.

        The pace is the run's clock: the pipe sink applies no rate limit of its
        own, so how fast this reads is how fast the engine plays. Reading
        slightly above realtime is what banks the lead a boundary's crossfade
        uses; reading unpaced makes PulseAudio render silence instead of
        applying backpressure, and the engine runs off the end of its content.
        """
        reader = self._reader
        proc = self._proc
        assert reader is not None
        assert proc is not None
        loop = asyncio.get_running_loop()
        shaper = _CaptureShaper()
        pace_anchor: float | None = None
        paced_bytes = 0
        stalled_for = 0.0
        while not self._stopped:
            if self._item_over:
                # the engine moved on: everything after this point is the next
                # (autoplayed) track's audio, not this item's
                self._finish_delivery()
                return
            try:
                chunk = await asyncio.wait_for(reader.read(_READ_CHUNK_SIZE), _READ_SLICE_S)
            except TimeoutError:
                if self._engine_exited:
                    # the engine exited and its tail has drained: the item is over
                    self._finish_delivery()
                    return
                # No data. Either the sink is suspended on purpose (the engine
                # is paused or the cushion is at its cap) or the run has died;
                # only the former is fine.
                if self._sink_running:
                    stalled_for += _READ_SLICE_S
                    if stalled_for >= _STALL_TIMEOUT_S:
                        self._fail("audio stalled")
                        return
                # Restart the pacing clock rather than carry the gap: making up
                # lost time would mean an unpaced burst, which over-demands the
                # engine's fetch pipeline exactly when it is recovering.
                pace_anchor = None
                continue
            stalled_for = 0.0
            if not chunk:
                # writer end closed: the capture sink is gone (pulse restart)
                self._fail("the capture sink was lost mid-stream")
                return
            if not (chunk := shaper.shape(chunk)):
                continue
            if pace_anchor is None:
                pace_anchor = loop.time()
                paced_bytes = 0
            paced_bytes += len(chunk)
            chunk = self._scrub(chunk)
            if chunk and self._engine_exited and chunk.count(0) == len(chunk):
                # the engine is gone: what the sink renders from here on is only
                # padding, so the item's audio has fully arrived
                chunk = b""
            if chunk:
                self._read_bytes += len(chunk)
                if not await self._hand_over(chunk):
                    return
            elif self._engine_exited:
                self._finish_delivery()
                return
            resume_at = pace_anchor + paced_bytes / (_BYTES_PER_SECOND * _PACE_RATE) - _PACE_BURST_S
            if (delay := resume_at - loop.time()) > 0:
                await asyncio.sleep(delay)

    def _scrub(self, chunk: bytes) -> bytes:
        """Drop the sink's tail padding (the capture shaper already trims the lead)."""
        if self._duration_ms is not None and chunk.count(0) == len(chunk):
            # zeros inside the item's own tail zone are the sink idling while the
            # engine winds down, not content; an item no longer than the zone has
            # no distinguishable tail and its silence is left alone
            target = int((self._duration_ms - self._seek_target_ms) / 1000 * _BYTES_PER_SECOND)
            zone = int(_TAIL_PAD_ZONE_S * _BYTES_PER_SECOND)
            if target > zone and self._read_bytes >= target - zone:
                self._tail_zeros += len(chunk)
                if self._tail_zeros > int(_TAIL_PAD_GRACE_S * _BYTES_PER_SECOND):
                    return b""
        else:
            self._tail_zeros = 0
        return chunk

    async def _hand_over(self, chunk: bytes) -> bool:
        """
        Cushion one chunk for the consumer, pausing the engine when it is full.

        :return: False when the run ended while waiting for cushion space.
        """
        if self._chunks.full():
            # the consumer is not taking audio (its buffer is at capacity):
            # suspend the sink so the engine pauses instead of overflowing the
            # FIFO, and resume once there is room again
            await self._set_sink(running=False)
            while not self._stopped and self._error is None:
                with suppress(TimeoutError):
                    async with asyncio.timeout(_READ_SLICE_S):
                        await self._chunks.put(chunk)
                        break
            else:
                return False
            await self._set_sink(running=self._engine_playing)
            return True
        self._chunks.put_nowait(chunk)
        return True

    def _finish_delivery(self) -> None:
        """Mark the item's audio as fully handed over."""
        self._delivery_done = True
        # the sentinel wakes a consumer already blocked on an empty cushion;
        # when the cushion is full, the flag alone ends the stream once the
        # consumer has drained it
        with suppress(asyncio.QueueFull):
            self._chunks.put_nowait(None)

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
                self._fail(f"capture sink control failed: {err}")
                return
            self._sink_running = running

    async def _run_events(self, client: SoloistClient, client_ready: asyncio.Event) -> None:
        """Keep the WebSocket client connected and feed its events into the run state."""
        proc = self._proc
        assert proc is not None
        if not await client.wait_until_ready(_STARTUP_TIMEOUT_S):
            # a natural exit right at startup still has to release the waiters
            if not self._engine_exited and proc.returncode is None:
                self._fail("the run did not publish its WebSocket endpoint")
            client_ready.set()
            return
        client_ready.set()
        while not self._stopped:
            try:
                await client.listen_events(self._handle_event)
            except asyncio.CancelledError:
                raise
            except Exception as err:
                if self._engine_exited or proc.returncode is not None:
                    # the daemon exited (the item finished); the socket dying with
                    # it is not an error
                    return
                self.logger.debug("Soloist event connection lost, reconnecting: %s", err)
                await asyncio.sleep(_CONNECT_POLL_S)

    async def _handle_event(self, event: SoloistEvent) -> None:
        """Track what the engine is doing with this run's one item."""
        data = event.data
        if isinstance(data, SoloistAuthState):
            self._logged_in = data.logged_in
            if data.logged_in is False and not self._unpaired:
                await self._check_pairing_lost()
            return
        if isinstance(data, SoloistTrackChanged):
            if data.item is not None and data.item.uri:
                self._observe_item(data.item.uri, _decorated_duration_ms(data.item))
            return
        if isinstance(data, SoloistPositionSync):
            self._observe_position(data.position.position_ms)
            return
        if isinstance(data, SoloistPlaybackState):
            if data.item is not None and data.item.uri:
                self._observe_item(data.item.uri, _decorated_duration_ms(data.item))
            if data.position is not None:
                self._observe_position(data.position.position_ms)
            playing = data.status == "playing"
            self._engine_playing = playing
            if self._started.is_set() and self._reader is not None:
                # pause silence stays out of the delivered PCM; the cushion gate
                # takes priority over resuming
                if playing and not self._chunks.full():
                    await self._set_sink(running=True)
                elif not playing and proc_running(self._proc):
                    await self._set_sink(running=False)

    def _observe_item(self, uri: str, duration_ms: int | None) -> None:
        """Record the engine reaching an item."""
        if uri != self.spotify_uri:
            if not self._started.is_set():
                # the engine started on something else entirely: whatever it is
                # playing, it is not what was asked
                self._fail(f"the engine started on {uri}")
                return
            # The engine wanders into the next track (autoplay) in the instant
            # before a finished single-track run exits: this run's item is over,
            # and what renders now is not its audio. The daemon is put down
            # rather than left to play to nobody on the account's one stream.
            self._item_over = True
            self.logger.debug("Engine wandered to %s; %s has ended", uri, self.spotify_uri)
            self._finish_delivery()
            self.mass.create_task(self.backend.discard_run, self)
            return
        if duration_ms:
            self._duration_ms = duration_ms
        self._started.set()

    def _observe_position(self, position_ms: int) -> None:
        """Confirm an armed seek once the engine reports at (or past) its target."""
        self._position_ms = position_ms
        if (
            not self._seek_confirmed.is_set()
            and self._seek_target_ms
            and position_ms >= self._seek_target_ms - _SEEK_CONFIRM_GRACE_MS
        ):
            self._seek_confirmed.set()


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

    Two jobs. It drops the infrastructure silence that precedes the session's
    first decoded sample (bounded, see ``_MAX_LEAD_TRIM_S``). And it carries a
    partial frame across reads: ``StreamReader.read`` returns whatever is
    available, which is not always a whole number of frames, so without this an
    item change between two reads would end one item mid-frame and start the
    next on the remainder - swapping its channels.
    """

    def __init__(self) -> None:
        """Initialize the shaper for one session."""
        self._lead_skipped = 0
        self._first_audio_seen = False
        self._carry = b""

    def shape(self, chunk: bytes) -> bytes:
        """
        Return the next whole frames of audio, or empty when there are none yet.

        :param chunk: The bytes just read from the capture FIFO.
        """
        if self._carry:
            chunk = self._carry + chunk
            self._carry = b""
        if not self._first_audio_seen:
            chunk, skipped = _trim_lead_silence(chunk, self._lead_skipped)
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


def _trim_lead_silence(chunk: bytes, already_skipped: int) -> tuple[bytes, int]:
    """
    Drop leading infrastructure silence from the head of the session (bounded).

    :param chunk: The chunk read from the FIFO.
    :param already_skipped: Silence bytes dropped from earlier chunks.
    :return: The (possibly emptied/shortened) chunk and how many bytes were dropped.
    """
    max_lead_trim = int(_MAX_LEAD_TRIM_S * _BYTES_PER_SECOND)
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
