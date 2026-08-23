"""
Spotify Soloist playback backend for the Spotify music provider.

Runs one continuous session of ``soloist``, Spotify's official headless client,
and feeds it one track ahead so the engine plays consecutive tracks without a
break — with Spotify's own crossfade at the boundaries. The session renders into
a private PulseAudio capture sink whose FIFO is read back slightly above
realtime pace, and that one continuous audio stream is handed to Music Assistant
as ordinary per-item streams: an item's stream ends where the session moves on to
the next track, and the next item's stream begins there. Played back to back the
items reproduce the session's audio sample for sample, so the cut position does
not matter and a crossfade simply lives inside the bytes.

SECURITY NOTE: the daemon takes the user's personal API key on its command
line; nothing in this module may ever log the process argv.

Shared infrastructure (binary manager, WebSocket client, audio prefs, pulse
capture) is owned by the Spotify Connect provider / core helpers and reused here.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import time
from collections import deque
from contextlib import suppress
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Final, NoReturn

from aiohttp import ClientError
from music_assistant_models.enums import ContentType, MediaType
from music_assistant_models.errors import AudioError, LoginFailed
from music_assistant_models.media_items import AudioFormat

from music_assistant.constants import CONF_CROSSFADE_DURATION, CONF_PLAYER_QUEUES
from music_assistant.helpers.process import AsyncProcess
from music_assistant.helpers.pulse_capture import (
    CAPTURE_CHANNELS,
    CAPTURE_SAMPLE_RATE,
    PipeSink,
    get_pulse_capture_server,
)
from music_assistant.providers.spotify.constants import (
    CONF_SOLOIST_API_KEY,
    CONF_SOLOIST_CONSENT,
    CONF_SOLOIST_SESSION_DIR,
    SOLOIST_DATA_DIR_NAME,
    SOLOIST_DEVICE_NAME,
)
from music_assistant.providers.spotify.helpers import soloist_session_present
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
    SoloistVolumeChanged,
)

from .base import SpotifyPlaybackBackend

if TYPE_CHECKING:
    import logging
    from collections.abc import AsyncGenerator

    from music_assistant_models.queue_item import QueueItem
    from music_assistant_models.streamdetails import StreamDetails

    from music_assistant.helpers.json import SerializableType
    from music_assistant.helpers.pulse_capture import PulseCaptureServer
    from music_assistant.providers.spotify.provider import SpotifyProvider
    from music_assistant.providers.spotify_connect.soloist.runtime import SoloistEvent

# The capture sink delivers fixed s32le/44.1kHz/2ch PCM. Soloist decodes
# internally and never exposes the source codec or bit depth (lossless up to
# 24-bit fits the 32-bit container losslessly), so the capture format doubles
# as the display format.
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
_SEEK_CONFIRM_TIMEOUT_S: Final[float] = 15.0
# a seek is re-sent at this interval until a position anchor confirms it
_SEEK_RETRY_INTERVAL_S: Final[float] = 2.0
# position reported by a seek anchor may fall slightly before the requested target
_SEEK_TOLERANCE_MS: Final[int] = 2000
# Infrastructure silence precedes the session's first decoded sample; trim at
# most this much, once per session. Kept small on purpose: the trim cannot tell
# capture pre-roll from a genuinely digitally-silent intro, so the budget bounds
# what an intro can lose while still covering the measured pre-roll (~140 ms).
_MAX_LEAD_TRIM_S: Final[float] = 0.5
# how far the last observed playback position may fall short of an item's
# duration before its delivered PCM is rejected as incomplete
_INCOMPLETE_TOLERANCE_MS: Final[int] = 10000
# after the last item of a run ends, how long to keep draining its tail
_DRAIN_TIMEOUT_S: Final[float] = 2.0
# how long an idle session (nothing playing, nothing fed) is kept alive so a
# follow-up item can continue on it instead of paying a cold start
_IDLE_TIMEOUT_S: Final[float] = 30.0
# an item's stream may run past its nominal duration (it carries the head of the
# crossfade into the next track), but never unboundedly: without the session
# reporting a track change by then, something is wrong and the item fails
_ITEM_OVERRUN_S: Final[float] = 30.0
# close() SIGINTs a live daemon, so it is given this long to exit by itself first
_DAEMON_EXIT_GRACE_S: Final[float] = 3.0
# how far ahead of the playing item to look for the one being streamed: a flow
# stream runs ahead of the player, a per-item stream is the playing item or its
# successor
_FOLLOWER_SEARCH_DEPTH: Final[int] = 4
# audio held for an item whose stream has not been opened (or reopened) yet;
# beyond this the session is considered abandoned
_UNCLAIMED_LIMIT_S: Final[float] = 60.0


class SoloistBackend(SpotifyPlaybackBackend):
    """
    Fetches Spotify audio from one continuous ``soloist`` session, fed one track ahead.

    Requires a stored paired session in the per-instance data directory
    (provisioned by the setup flow via ``soloist --pair``).
    """

    _server: PulseCaptureServer | None = None
    _binary: Path | None = None
    _session: _SoloistSession | None = None

    def __init__(self, provider: SpotifyProvider) -> None:
        """
        Initialize the backend.

        :param provider: The owning Spotify provider instance.
        """
        super().__init__(provider)
        self._session_lock = asyncio.Lock()

    @property
    def audio_format(self) -> AudioFormat:
        """Return the audio format this backend delivers, for use in StreamDetails."""
        return AudioFormat(
            content_type=ContentType.PCM_S32LE,
            codec_type=ContentType.PCM_S32LE,
            sample_rate=CAPTURE_SAMPLE_RATE,
            bit_depth=32,
            channels=CAPTURE_CHANNELS,
        )

    @property
    def max_concurrent_streams(self) -> int:
        """
        Two: a handover holds two item streams against the one Spotify session.

        The account still runs a single Soloist session; the second slot exists
        because the item that is ending and the item that continues from it are
        two Music Assistant streams reading the same session in turn.
        """
        return 2

    @property
    def is_realtime(self) -> bool:
        """Soloist delivers at playback pace (~1.1x ceiling): no read-ahead."""
        return True

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
        """Stop the session and release the capture server."""
        async with self._session_lock:
            if (session := self._session) is not None:
                self._session = None
                await session.stop()
        if (server := self._server) is not None:
            self._server = None
            await server.release()

    async def stream_spotify_uri(
        self,
        spotify_uri: str,
        seek_position: int = 0,
        *,
        streamdetails: StreamDetails | None = None,
    ) -> AsyncGenerator[bytes]:
        """
        Yield the PCM audio for one Spotify URI out of the continuous session.

        :param spotify_uri: Canonical Spotify URI (``spotify:track:<id>`` or
            ``spotify:episode:<id>``).
        :param seek_position: Position in seconds to start from. Any seek
            restarts the session at that position (a continuous run cannot be
            rewound without disrupting it).
        :param streamdetails: The StreamDetails this audio is requested for.
            They tell the session which queue it serves and which item of it is
            being streamed, so it can feed the engine the following track.
        """
        if self._server is None or self._binary is None:
            raise AudioError("Spotify Soloist backend is not started")
        queue_id = streamdetails.queue_id if streamdetails is not None else None
        session, item = await self._acquire(spotify_uri, seek_position, queue_id)
        try:
            # feed before the first byte is handed over: the item's own stream
            # must not be able to reach its end before the next one is queued
            if streamdetails is not None:
                await session.feed_after(streamdetails, spotify_uri)
            async for chunk in item.read():
                yield chunk
        finally:
            item.release()
        await session.validate_item(item)

    async def discard_session(self, session: _SoloistSession) -> None:
        """
        Stop a session for good, dropping it if it is still the current one.

        :param session: The session to tear down.
        """
        async with self._session_lock:
            if self._session is session:
                self._session = None
        await session.stop()

    async def get_diagnostics(self) -> dict[str, SerializableType]:
        """Return diagnostic details about the backend (never any secret)."""
        session = self._session
        return {
            "soloist": SoloistBinaryManager(self.mass).diagnostics(),
            "paired": await asyncio.to_thread(self._has_stored_session),
            "session_active": session is not None and session.usable,
        }

    @property
    def _api_key(self) -> str:
        """Return the stored Soloist API key."""
        return str(self.provider.get_setup_value(CONF_SOLOIST_API_KEY) or "")

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

    async def _acquire(
        self, spotify_uri: str, seek_position: int, queue_id: str | None
    ) -> tuple[_SoloistSession, _ItemAudio]:
        """
        Return the session and audio channel to stream this item from.

        Continues the running session when it already plays (or has been fed)
        this item for this queue; anything else — a seek, another queue, a
        skipped-to item, a session that is gone — starts a fresh session.
        """
        async with self._session_lock:
            session = self._session
            if (
                session is not None
                and session.usable
                and session.queue_id == queue_id
                and not seek_position
                and (item := session.item_for(spotify_uri)) is not None
            ):
                item.claim()
                return session, item
            if session is not None:
                self._session = None
                await session.stop()
            # cheap thanks to the shared verify cache; swaps in a fresh build when
            # the installed one is nearing its 90-day expiry
            try:
                self._binary = await SoloistBinaryManager(self.mass).ensure_fresh(self._consent)
            except SoloistError as err:
                raise AudioError(f"Spotify Soloist binary unavailable: {err}") from err
            session = _SoloistSession(self, queue_id)
            try:
                item = await session.start(spotify_uri, seek_position)
            except BaseException:
                await session.stop()
                raise
            self._session = session
            item.claim()
            return session, item

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

    def _prepare_data_dir(self, crossfade_ms: int) -> None:
        """
        Prepare the data dir for a fresh session spawn (blocking).

        :param crossfade_ms: Crossfade duration to configure the engine with.
        """
        self._data_dir.mkdir(parents=True, exist_ok=True)
        self._data_dir.chmod(0o700)
        # endpoint files from a previous run would point the client at a dead port
        for endpoint_file in (WS_ADDR_FILE, WS_PORT_FILE):
            (self._data_dir / endpoint_file).unlink(missing_ok=True)
        # the engine reads its prefs at startup only, so they are refreshed on
        # every spawn. Its own loudness normalization stays off: Music Assistant
        # normalizes this audio itself and would otherwise do so twice.
        write_audio_prefs(
            self._data_dir,
            self.logger,
            crossfade_ms=crossfade_ms,
            loudness_normalization=False,
        )

    def _session_args(self) -> list[str]:
        """
        Build the soloist session's argv.

        SECURITY: the argv carries the user's API key — it must never be logged
        or end up in any error message.
        """
        assert self._binary is not None
        return [
            str(self._binary),
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


class _SoloistSession:
    """
    One continuous soloist session, serving the consecutive items of one queue.

    The session reads its capture FIFO once, at ``_PACE_RATE``, and routes the
    audio to the item that the engine reports as current. An item's audio
    channel is created when the item is played or fed, and closed when the
    engine moves on — that boundary is the cut between two Music Assistant
    item streams.
    """

    def __init__(self, backend: SoloistBackend, queue_id: str | None) -> None:
        """
        Initialize a session for one queue (nothing is spawned yet).

        :param backend: The owning backend.
        :param queue_id: The player queue this session serves, when known.
        """
        self.backend = backend
        self.queue_id = queue_id
        self.mass = backend.mass
        self.logger: logging.Logger = backend.logger
        self.crossfade_ms = 0
        self._items: dict[str, _ItemAudio] = {}
        self._current: _ItemAudio | None = None
        # uris handed to the engine that it has not started playing yet
        self._pending: deque[str] = deque()
        self._client: SoloistClient | None = None
        self._sink: PipeSink | None = None
        self._proc: AsyncProcess | None = None
        self._tasks: list[asyncio.Task[None]] = []
        self._log_task: asyncio.Task[None] | None = None
        self._transport: asyncio.ReadTransport | None = None
        self._reader: asyncio.StreamReader | None = None
        self._error: str | None = None
        self._stopped = False
        self._logged_out = False
        self._pin_in_flight = False
        self._demand_started = False
        self._idle_since: float | None = None

    @property
    def current(self) -> _ItemAudio | None:
        """Return the item the engine is currently playing, if any."""
        return self._current

    @property
    def has_pending(self) -> bool:
        """Return whether the engine was fed an item it has not started yet."""
        return bool(self._pending)

    @property
    def usable(self) -> bool:
        """Return whether this session can still serve items."""
        return not self._stopped and self._error is None and self._logged_out is False

    def item_for(self, spotify_uri: str) -> _ItemAudio | None:
        """
        Return the audio channel for an item this session plays or was fed, if any.

        A channel whose stream was abandoned mid-item cannot be resumed (the
        audio it already handed over is gone), so it is not offered again.
        """
        item = self._items.get(spotify_uri)
        if item is None or item.broken:
            return None
        return item

    async def start(self, spotify_uri: str, seek_position: int) -> _ItemAudio:
        """
        Spawn the session and start playing the given item.

        :param spotify_uri: Canonical Spotify URI to start on.
        :param seek_position: Position in seconds to start from.
        :return: The audio channel for the requested item.
        """
        backend = self.backend
        server = backend._server
        assert server is not None
        assert backend._binary is not None
        self.crossfade_ms = self._queue_crossfade_ms()
        await asyncio.to_thread(backend._prepare_data_dir, self.crossfade_ms)
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
        self._log_task = asyncio.create_task(self._log_output(proc))
        self._client = client = SoloistClient(self.mass, backend._data_dir, self.logger)
        client_ready = asyncio.Event()
        self._spawn_task(self._run_events(client, client_ready))
        item = await self._play(spotify_uri, seek_position, client_ready)
        # the reader must be attached before the sink starts producing, or the
        # sink's first writes go to a reader-less FIFO and are dropped
        self._reader, self._transport = await _open_fifo_reader(sink.fifo_path)
        self._spawn_task(self._read_capture())
        self._demand_started = True
        if item.status == "playing":
            await sink.resume()
        return item

    async def feed_after(self, streamdetails: StreamDetails, spotify_uri: str) -> None:
        """
        Hand the engine the item that follows the one being streamed, if any.

        Only consecutive tracks are stitched: the engine's queue command takes
        track URIs, and a podcast episode or audiobook chapter gains nothing
        from a crossfade anyway.

        :param streamdetails: The StreamDetails of the item being streamed, used
            to locate it in the queue.
        :param spotify_uri: The URI being streamed (only tracks are fed ahead).
        """
        client = self._client
        if client is None or not spotify_uri.startswith("spotify:track:"):
            return
        follower = self._follower(streamdetails)
        if follower is None:
            return
        next_uri = self._track_uri(follower)
        if next_uri is None or next_uri in self._items:
            return
        try:
            await client.add_to_queue(next_uri)
        except (TimeoutError, OSError, ClientError, SoloistError) as err:
            # a failed feed only costs the crossfade at that boundary: the next
            # item still plays, on a fresh session
            self.logger.debug("Unable to feed %s to the soloist session: %s", next_uri, err)
            return
        self._items[next_uri] = _ItemAudio(next_uri, self)
        self._pending.append(next_uri)
        self.logger.debug("Fed %s to the soloist session", next_uri)

    async def validate_item(self, item: _ItemAudio) -> None:
        """
        Validate that an item's delivered audio actually covered it.

        A starved session pads with silence rather than failing, so completeness
        is judged by the furthest playback position the engine reported while
        the item was current.

        :param item: The item whose stream just finished.
        :raises AudioError: When the item was cut short.
        """
        if self._error:
            raise AudioError(f"Spotify Soloist: {self._error}")
        if not item.playing_seen:
            raise AudioError(f"Spotify Soloist never started playing {item.uri}")
        if item.duration_ms is None:
            return
        # with crossfade the engine starts the next track before this one ends,
        # so the last position reported for it falls a crossfade short by design
        tolerance_ms = min(
            max(_INCOMPLETE_TOLERANCE_MS, self.crossfade_ms + _INCOMPLETE_TOLERANCE_MS),
            item.duration_ms // 2,
        )
        if item.last_position_ms is None or item.last_position_ms + tolerance_ms < item.duration_ms:
            raise AudioError(
                f"Spotify Soloist delivered incomplete audio for {item.uri} "
                f"(reached {item.last_position_ms or 0}ms of {item.duration_ms}ms)"
            )

    async def stop(self) -> None:
        """Tear the session down: stop the daemon, the reader and the capture sink."""
        if self._stopped:
            return
        self._stopped = True
        for item in self._items.values():
            item.close()
        if self._client is not None and self._proc is not None:
            # commands travel over the events connection, so this has to happen
            # before that task is cancelled; stopping playback lets the engine
            # wind down by itself instead of on the SIGINT that close() sends
            with suppress(Exception):
                await self._client.pause()
        await _cancel_and_join(self._tasks)
        self._tasks.clear()
        if self._transport is not None:
            self._transport.close()
            self._transport = None
        if (proc := self._proc) is not None:
            self._proc = None
            # the log reader stays alive across this wait: nothing else drains
            # the daemon's stdout, and a full pipe would keep it from exiting
            with suppress(TimeoutError):
                async with asyncio.timeout(_DAEMON_EXIT_GRACE_S):
                    await proc.wait()
            # a forced close must never be judged by its exit code
            with suppress(Exception):
                await proc.close()
        if self._log_task is not None:
            await _cancel_and_join([self._log_task])
            self._log_task = None
        if (sink := self._sink) is not None:
            self._sink = None
            with suppress(Exception):
                await sink.unload()

    def _spawn_task(self, coro: object) -> None:
        """Track a session-scoped task so stop() can cancel and join it."""
        self._tasks.append(asyncio.create_task(coro))  # type: ignore[arg-type]

    def _fail(self, message: str) -> None:
        """
        Record a fatal session error, unblock every waiting item and tear the session down.

        The teardown runs as its own task: it cancels the very tasks this is
        called from, and the daemon has to go either way — an unusable session
        would otherwise keep playing to nobody.
        """
        if self._error is not None:
            return
        self._error = message
        for item in self._items.values():
            item.close()
        self.mass.create_task(self.backend.discard_session, self)

    def _queue_crossfade_ms(self) -> int:
        """
        Return the crossfade the engine should apply, from the queue's own preference.

        Music Assistant cannot crossfade audio it is not mixing, so its queue
        setting is handed to the engine instead. The pref is in milliseconds and
        sub-second values silently disable crossfade, which the seconds-based
        queue setting can never produce.
        """
        queue = self.mass.player_queues.get(self.queue_id) if self.queue_id else None
        if queue is None or not queue.crossfade_enabled:
            return 0
        seconds = self.mass.config.get_raw_core_config_value(
            CONF_PLAYER_QUEUES, CONF_CROSSFADE_DURATION, 8
        )
        return int(seconds) * 1000

    def _follower(self, streamdetails: StreamDetails) -> QueueItem | None:
        """Return the queue item that follows the one these StreamDetails belong to."""
        queue_id = self.queue_id
        queue = self.mass.player_queues.get(queue_id) if queue_id else None
        if queue is None or queue_id is None or queue.current_index is None:
            return None
        controller = self.mass.player_queues
        # the item being streamed is the one playing or one of the few ahead of
        # it (a flow stream runs ahead of the player); identify it by the
        # StreamDetails object itself, so a repeated track cannot be mistaken
        for offset in range(_FOLLOWER_SEARCH_DEPTH):
            item = controller.get_item(queue_id, queue.current_index + offset)
            if item is None:
                break
            if item.streamdetails is streamdetails:
                return controller.get_next_item(queue_id, item.queue_item_id)
        return None

    def _track_uri(self, queue_item: QueueItem) -> str | None:
        """Return the Spotify track URI of a queue item on this provider instance."""
        media_item = queue_item.media_item
        if media_item is None or media_item.media_type != MediaType.TRACK:
            return None
        instance_id = self.backend.provider.instance_id
        if media_item.provider == instance_id:
            return f"spotify:track:{media_item.item_id}"
        for mapping in media_item.provider_mappings:
            if mapping.provider_instance == instance_id:
                return f"spotify:track:{mapping.item_id}"
        return None

    async def _play(
        self, spotify_uri: str, seek_position: int, client_ready: asyncio.Event
    ) -> _ItemAudio:
        """Activate the engine, start the requested item and wait until it is current."""
        client = self._client
        assert client is not None
        item = self._items[spotify_uri] = _ItemAudio(spotify_uri, self)
        self._current = item
        try:
            async with asyncio.timeout(_STARTUP_TIMEOUT_S):
                await client_ready.wait()
        except TimeoutError:
            self._raise_startup_error("did not publish its WebSocket endpoint", spotify_uri)
        # a fresh daemon is not the active Connect device yet, and play() on an
        # inactive device would start playback on whatever else is active
        await client.activate(await_result=True)
        await client.play(spotify_uri)
        await self._await_item_ready(item)
        if seek_position:
            await self._cold_seek(client, item, seek_position * 1000)
        return item

    async def _await_item_ready(self, item: _ItemAudio) -> None:
        """Wait until the engine reports the requested item as its current one."""
        proc = self._proc
        assert proc is not None
        try:
            async with asyncio.timeout(_STARTUP_TIMEOUT_S):
                exit_task = asyncio.ensure_future(proc.wait())
                item_task = asyncio.ensure_future(item.started.wait())
                try:
                    await asyncio.wait({exit_task, item_task}, return_when=asyncio.FIRST_COMPLETED)
                finally:
                    exit_task.cancel()
                    item_task.cancel()
        except TimeoutError:
            self._raise_startup_error("timed out waiting for playback to start", item.uri)
        if self._error or not item.started.is_set():
            if proc.returncode == EXIT_CODE_BUILD_EXPIRED:
                # an expired build exits with code 10 right at spawn
                await self._handle_expired_build()
            self._raise_startup_error("exited before playback started", item.uri)

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

    def _raise_startup_error(self, detail: str, spotify_uri: str) -> NoReturn:
        """Raise the most specific startup failure for the requested item."""
        if self._error:
            raise AudioError(f"Spotify Soloist: {self._error}")
        if self._logged_out:
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
        raise AudioError(f"Spotify Soloist {detail} for {spotify_uri}")

    async def _cold_seek(self, client: SoloistClient, item: _ItemAudio, target_ms: int) -> None:
        """
        Seek the engine to the target position before any PCM is released.

        The sink is still suspended, so no pre-seek audio enters the FIFO; PCM
        demand only starts once a position report confirms the seek landed.
        """
        item.seek_target_ms = target_ms
        # the engine silently drops a seek that arrives while the track is still
        # loading (verified via event trace), so re-send it until a position
        # anchor confirms it landed
        deadline = asyncio.get_running_loop().time() + _SEEK_CONFIRM_TIMEOUT_S
        while True:
            await client.seek(target_ms)
            with suppress(TimeoutError):
                async with asyncio.timeout(_SEEK_RETRY_INTERVAL_S):
                    await item.seek_confirmed.wait()
            if item.seek_confirmed.is_set():
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
            self.logger.debug("[soloist] %s", text)

    async def _read_capture(self) -> None:
        """
        Read the capture FIFO once for the whole session and route it to the current item.

        The pace is the session's clock: the pipe sink applies no rate limit of
        its own, so reading faster than the engine can deliver makes PulseAudio
        render silence instead of applying backpressure. Reading slightly above
        realtime banks the cushion that carries an item boundary.
        """
        reader = self._reader
        proc = self._proc
        assert reader is not None
        assert proc is not None
        loop = asyncio.get_running_loop()
        lead_skipped = 0
        bytes_read = 0
        # doubles as the "first audio byte of the session seen" marker
        pace_start: float | None = None
        stalled_for = 0.0
        while not self._stopped:
            self._expire_idle()
            if proc.returncode is not None:
                self._fail(f"the session exited with code {proc.returncode}")
                return
            try:
                chunk = await asyncio.wait_for(reader.read(_READ_CHUNK_SIZE), _READ_SLICE_S)
            except TimeoutError:
                # no data: the sink is suspended (the engine is rebuffering or
                # the run is over); bounded so a dead session cannot hang
                stalled_for += _READ_SLICE_S
                if stalled_for >= _STALL_TIMEOUT_S:
                    self._fail("audio stalled")
                    return
                continue
            stalled_for = 0.0
            if not chunk:
                # writer end closed: the capture sink is gone (pulse restart)
                self._fail("the capture sink was lost mid-stream")
                return
            if pace_start is None:
                chunk, skipped = _trim_lead_silence(chunk, lead_skipped)
                lead_skipped += skipped
                if not chunk:
                    continue
                pace_start = loop.time()
            bytes_read += len(chunk)
            if (item := self._current) is not None:
                item.write(chunk)
            del chunk
            # pace the whole session from its first audio byte, so the average
            # holds at _PACE_RATE across item boundaries
            resume_at = pace_start + bytes_read / (_BYTES_PER_SECOND * _PACE_RATE) - _PACE_BURST_S
            if (delay := resume_at - loop.time()) > 0:
                await asyncio.sleep(delay)

    def _expire_idle(self) -> None:
        """
        Fail a session no item stream reads from, so its daemon does not linger.

        A Spotify run ends without telling the provider: the queue simply stops
        asking for items. The grace period is what lets a follow-up item
        continue on the same session instead of paying a cold start.
        """
        if any(item.claimed for item in self._items.values()):
            self._idle_since = None
            return
        now = time.monotonic()
        if self._idle_since is None:
            self._idle_since = now
        elif now - self._idle_since >= _IDLE_TIMEOUT_S:
            self.logger.debug("Ending the idle soloist session")
            self._fail("the session went idle")

    async def _run_events(self, client: SoloistClient, client_ready: asyncio.Event) -> None:
        """Keep the WebSocket client connected and feed its events into the session state."""
        proc = self._proc
        assert proc is not None
        if not await client.wait_until_ready(_STARTUP_TIMEOUT_S):
            self._fail("the session did not publish its WebSocket endpoint")
            client_ready.set()
            return
        client_ready.set()
        while True:
            try:
                await client.listen_events(self._handle_event)
            except asyncio.CancelledError:
                raise
            except (TimeoutError, OSError, ClientError, SoloistError) as err:
                # ordinary connection drop; reconnect while the daemon is alive
                # so the item boundaries do not go unnoticed
                self.logger.debug("soloist events connection dropped: %s", err)
            except Exception as err:
                # a defect in event handling must surface loudly and fail the
                # session: continuing would deliver audio against stale state
                self.logger.exception("Unexpected error while handling soloist events")
                self._fail(f"event handling failed: {err}")
                return
            if proc.returncode is not None:
                return
            await asyncio.sleep(1)

    async def _handle_event(self, event: SoloistEvent) -> None:
        """Track what the engine is playing and gate the capture sink on its state."""
        data = event.data
        if isinstance(data, SoloistAuthState):
            if not data.logged_in:
                self._logged_out = True
                self._fail("the stored pairing no longer logs in")
            return
        if isinstance(data, SoloistTrackChanged):
            if data.item is not None and data.item.uri:
                await self._observe_current(data.item.uri, _decorated_duration_ms(data.item))
            return
        if isinstance(data, SoloistPositionSync):
            if (item := self._current) is not None:
                item.observe_position(data.position.position_ms)
            return
        if isinstance(data, SoloistVolumeChanged):
            await self._repin_volume(data.volume)
            return
        if isinstance(data, SoloistPlaybackState):
            await self._handle_playback_state(data)

    async def _handle_playback_state(self, data: SoloistPlaybackState) -> None:
        """Apply a playback_state snapshot: current item, position, volume and sink gating."""
        if data.item is not None and data.item.uri:
            await self._observe_current(data.item.uri, _decorated_duration_ms(data.item))
        item = self._current
        if item is not None:
            item.status = data.status
            if data.status == "playing":
                item.playing_seen = True
            if data.position is not None:
                item.observe_position(data.position.position_ms)
        if data.volume is not None:
            await self._repin_volume(data.volume)
        if not self._demand_started or item is None:
            return
        if data.status not in ("buffering", "playing", "paused"):
            return
        if data.status != "playing" and item.finishing:
            # the last item of a run: let its tail drain instead of cutting the
            # sink, the engine has nothing more to render anyway
            return
        # the pipe sink writes silence into the FIFO while the engine stalls on
        # rebuffering (or someone paused it from the Spotify app); suspending it
        # keeps that silence out of the delivered PCM
        sink = self._sink
        assert sink is not None
        try:
            if data.status == "playing":
                await sink.resume()
            else:
                await sink.suspend()
        except Exception as err:
            # fail closed: a sink with unknown suspend state would leak stall
            # silence into (or withhold audio from) the delivered PCM
            self._fail(f"capture sink control failed: {err}")
            return
        if data.status == "paused" and not item.finishing:
            # this session has no user-facing pause: someone paused it from the
            # Spotify app — resume playback (a persistently re-paused session
            # ends through the stall timeout)
            client = self._client
            if client is not None:
                with suppress(Exception):
                    await client.resume()

    async def _observe_current(self, uri: str, duration_ms: int | None) -> None:
        """
        Follow the engine to the item it reports as current, cutting the previous one.

        The cut lands wherever the engine says it moved on: an item's stream
        carries whatever was read up to that point (including the head of a
        crossfade) and the next item's stream continues from there, so the two
        together still reproduce the session's audio exactly.
        """
        current = self._current
        if current is not None and current.uri == uri:
            if duration_ms:
                current.duration_ms = duration_ms
            current.started.set()
            return
        item = self._items.get(uri)
        if item is None:
            # the engine moved on to something nobody asked for (its own
            # autoplay, or a track started from the Spotify app): give it a
            # channel so the reader has somewhere to put the audio, and let the
            # idle timeout end the session
            item = self._items[uri] = _ItemAudio(uri, self)
        if duration_ms:
            item.duration_ms = duration_ms
        with suppress(ValueError):
            self._pending.remove(uri)
        self._current = item
        item.started.set()
        if current is not None:
            current.close()
        if not item.claimed:
            self._signal_ready(uri)

    def _signal_ready(self, uri: str) -> None:
        """
        Tell the queue that this item's audio is live, so its buffer can start filling.

        This replaces the core's blind next-item trigger for realtime sources:
        the audio of a fed item does not exist until the session reaches it, and
        the item is identified by URI because a queue reorder may have moved it.
        """
        queue_id = self.queue_id
        queue = self.mass.player_queues.get(queue_id) if queue_id else None
        if queue is None or queue_id is None or (next_item := queue.next_item) is None:
            return
        if self._track_uri(next_item) != uri:
            return
        self.mass.player_queues.prepare_next_audio_buffer(queue_id)

    async def _repin_volume(self, volume: int) -> None:
        """
        Pin the engine back at unity volume when the Spotify app changed it.

        Off-unity volume would attenuate the captured PCM; the MA player owns
        the audible volume.
        """
        client = self._client
        if volume == 100 or self._pin_in_flight or client is None:
            return
        self._pin_in_flight = True
        try:
            with suppress(Exception):
                await client.set_volume(100)
        finally:
            self._pin_in_flight = False


class _ItemAudio:
    """The audio channel of one item within a session, plus what the engine said about it."""

    def __init__(self, uri: str, session: _SoloistSession) -> None:
        """
        Initialize an empty channel for one item.

        :param uri: The canonical Spotify URI of the item.
        :param session: The session this item is played by.
        """
        self.uri = uri
        self.session = session
        self.started = asyncio.Event()
        self.seek_confirmed = asyncio.Event()
        self.seek_target_ms: int | None = None
        self.duration_ms: int | None = None
        self.last_position_ms: int | None = None
        self.status: str | None = None
        self.playing_seen = False
        self.claimed = False
        # its stream was abandoned mid-item, so the channel can never be resumed
        self.broken = False
        self._chunks: deque[bytes] = deque()
        self._buffered = 0
        self._delivered = 0
        self._available = asyncio.Event()
        self._closed = False

    @property
    def finishing(self) -> bool:
        """Return whether this is the current item and nothing is queued behind it."""
        return self.session.current is self and not self.session.has_pending

    def claim(self) -> None:
        """Mark this channel as being read by an item stream."""
        self.claimed = True

    def release(self) -> None:
        """
        Release the channel after its stream ended (or was abandoned).

        An abandoned channel is marked broken: the audio it already handed over
        cannot be replayed, so a later stream for the same item has to restart
        the session instead of continuing here.
        """
        self.claimed = False
        if not self._closed:
            self.broken = True

    def write(self, chunk: bytes) -> None:
        """Append captured audio for this item."""
        if self._closed:
            return
        if not self.claimed and self._buffered >= int(_UNCLAIMED_LIMIT_S * _BYTES_PER_SECOND):
            # nobody is reading this item and nobody is going to: hold the
            # session's clock steady but stop growing
            return
        self._chunks.append(chunk)
        self._buffered += len(chunk)
        self._available.set()

    def close(self) -> None:
        """Close the channel: its stream ends once the buffered audio is drained."""
        self._closed = True
        self._available.set()

    def observe_position(self, position_ms: int) -> None:
        """Record a reported playback position (and confirm a pending seek)."""
        if self._closed:
            # positions reported after the cut describe the next item
            return
        # keep the furthest position: the engine's stop/idle snapshot at the end
        # of an item reports position 0 and must not erase the progress the
        # completeness validation relies on (verified live)
        self.last_position_ms = max(self.last_position_ms or 0, position_ms)
        # the floor of 1 keeps a pre-seek report of position 0 from confirming a
        # small seek target that falls inside the tolerance window
        if self.seek_target_ms is not None and position_ms >= max(
            1, self.seek_target_ms - _SEEK_TOLERANCE_MS
        ):
            self.seek_confirmed.set()

    async def read(self) -> AsyncGenerator[bytes]:
        """
        Yield this item's audio until the session moves on to the next one.

        The stream is not capped at the item's duration: with crossfade it
        legitimately carries the head of the next track, and the next item's
        stream begins exactly where this one stops.
        """
        session = self.session
        loop = asyncio.get_running_loop()
        overrun_bytes = self._overrun_limit()
        starving_for = 0.0
        while True:
            while self._chunks:
                starving_for = 0.0
                chunk = self._chunks.popleft()
                self._buffered -= len(chunk)
                self._delivered += len(chunk)
                yield chunk
                del chunk
                if overrun_bytes is not None and self._delivered >= overrun_bytes:
                    raise AudioError(
                        f"Spotify Soloist never moved on from {self.uri} "
                        f"({self._delivered // _BYTES_PER_SECOND}s delivered)"
                    )
            if self._closed:
                return
            if session._error:
                raise AudioError(f"Spotify Soloist: {session._error}")
            self._available.clear()
            deadline = loop.time() + _READ_SLICE_S
            with suppress(TimeoutError):
                async with asyncio.timeout_at(deadline):
                    await self._available.wait()
            if self._chunks or self._closed:
                continue
            if self.finishing:
                # the run's last item: the engine will not report a track change
                # to close this channel, so end it once its tail stops arriving
                await self._drain_tail()
                continue
            # the engine is playing something else entirely (skipped from the
            # Spotify app): this item is never going to get its audio
            starving_for += _READ_SLICE_S
            if starving_for >= _STALL_TIMEOUT_S:
                raise AudioError(f"Spotify Soloist delivered no audio for {self.uri}")

    async def _drain_tail(self) -> None:
        """Close the channel when the last item of a run has stopped producing audio."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + _DRAIN_TIMEOUT_S
        while loop.time() < deadline:
            self._available.clear()
            with suppress(TimeoutError):
                async with asyncio.timeout_at(deadline):
                    await self._available.wait()
            if self._chunks or self._closed:
                return
        self.close()

    def _overrun_limit(self) -> int | None:
        """Return the byte count past which this item is considered stuck."""
        if self.duration_ms is None:
            return None
        return int(
            (self.duration_ms / 1000 + self.session.crossfade_ms / 1000 + _ITEM_OVERRUN_S)
            * _BYTES_PER_SECOND
        )


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
            return b"", len(chunk)
        # budget exhausted: this is genuine silence content, not infrastructure
        return chunk, 0
    # keep sample-frame alignment when the audio starts mid-chunk
    offset = (len(chunk) - len(stripped)) // _FRAME_BYTES * _FRAME_BYTES
    return chunk[offset:], offset
