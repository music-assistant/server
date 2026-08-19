"""
Spotify Soloist playback backend for the Spotify music provider.

Fetches Spotify audio track by track through ``soloist --single-track``,
Spotify's official headless client. Each item spawns one short-lived daemon
that plays into a private PulseAudio capture sink; the sink's FIFO is read back
at realtime pace and yielded as raw PCM. The daemon is driven/observed over its
local WebSocket API (cold seek, buffering, completion).

SECURITY NOTE: the daemon takes the user's personal API key on its command
line; nothing in this module may ever log the process argv.

Shared infrastructure (binary manager, WebSocket client, pulse capture) is
owned by the Spotify Connect provider / core helpers and reused here.
"""

from __future__ import annotations

import asyncio
import os
import shutil
from contextlib import suppress
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Final, NoReturn

from aiohttp import ClientError
from music_assistant_models.enums import ContentType
from music_assistant_models.errors import AudioError, LoginFailed
from music_assistant_models.media_items import AudioFormat

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

from .base import SpotifyPlaybackBackend

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from music_assistant.helpers.json import SerializableType
    from music_assistant.helpers.pulse_capture import PulseCaptureServer
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
# Infrastructure silence precedes the first decoded sample; trim at most this
# much. Kept small on purpose: the trim cannot tell capture pre-roll from a
# genuinely digitally-silent intro, so the budget bounds what an intro can lose
# while still covering the measured pre-roll (~140 ms).
_MAX_LEAD_TRIM_S: Final[float] = 0.5
# how far the last observed playback position may fall short of the item's
# duration before the delivered PCM is rejected as incomplete
_INCOMPLETE_TOLERANCE_MS: Final[int] = 10000
# after the item ends, how long to keep draining the FIFO toward the duration cap
_DRAIN_TIMEOUT_S: Final[float] = 2.0


class SoloistSingleTrackBackend(SpotifyPlaybackBackend):
    """
    Fetches Spotify audio through ``soloist --single-track``, one process per item.

    Requires a stored paired session in the per-instance data directory
    (provisioned by the setup flow via ``soloist --pair``).
    """

    _server: PulseCaptureServer | None = None
    _binary: Path | None = None

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
        """One: a Spotify account supports a single active Soloist session."""
        return 1

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
        """Release the capture server."""
        if (server := self._server) is not None:
            self._server = None
            await server.release()

    async def stream_spotify_uri(
        self, spotify_uri: str, seek_position: int = 0
    ) -> AsyncGenerator[bytes]:
        """
        Yield the PCM audio for one Spotify URI.

        :param spotify_uri: Canonical Spotify URI (``spotify:track:<id>`` or
            ``spotify:episode:<id>``).
        :param seek_position: Position in seconds to start from (cold seek via
            the daemon's WebSocket API before any PCM is released).
        """
        server = self._server
        if server is None or self._binary is None:
            raise AudioError("Spotify Soloist backend is not started")
        # cheap thanks to the shared verify cache; swaps in a fresh build when
        # the installed one is nearing its 90-day expiry
        try:
            self._binary = await SoloistBinaryManager(self.mass).ensure_fresh(self._consent)
        except SoloistError as err:
            raise AudioError(f"Spotify Soloist binary unavailable: {err}") from err
        await asyncio.to_thread(self._prepare_data_dir)
        state = _TrackState(spotify_uri)
        sink = await PipeSink.create(server, self._sink_prefix)
        proc: AsyncProcess | None = None
        events_task: asyncio.Task[None] | None = None
        transport: asyncio.ReadTransport | None = None
        try:
            # unity gain so the FIFO carries the daemon's PCM unaltered; the
            # sink stays suspended until the (seeked) item is ready so no
            # infrastructure silence accumulates
            await sink.set_volume(100)
            await sink.suspend()
            proc = AsyncProcess(
                self._single_track_args(spotify_uri),
                stderr=True,
                # the explicit process name keeps AsyncProcess logging free of
                # the argv (which carries the API key)
                name=f"soloist-track[{self.provider.name}]",
                env=server.child_env(sink.sink_name),
            )
            await proc.start()
            proc.attach_stderr_reader(asyncio.create_task(self._log_stderr(proc)))
            client = SoloistClient(self.mass, self._data_dir, self.logger)
            events_task = asyncio.create_task(self._run_events(client, state, sink, proc))
            await self._await_item_ready(state, proc)
            if seek_position:
                await self._cold_seek(client, state, seek_position * 1000)
            # the reader must be attached before the sink starts producing, or
            # the sink's first writes go to a reader-less FIFO and are dropped
            reader, transport = await _open_fifo_reader(sink.fifo_path)
            # hand sink control to the event handler first, then resume only
            # when playback is actually running — resuming while soloist still
            # buffers would capture the buffering silence
            state.demand_started = True
            if state.last_status == "playing":
                await sink.resume()
            async for chunk in self._read_pcm(state, reader, proc, seek_position):
                yield chunk
            await self._evaluate_result(state, proc)
        finally:
            if events_task is not None:
                events_task.cancel()
                with suppress(asyncio.CancelledError):
                    await events_task
            if transport is not None:
                transport.close()
            if proc is not None:
                await proc.close()
            with suppress(Exception):
                await sink.unload()

    async def get_diagnostics(self) -> dict[str, SerializableType]:
        """Return diagnostic details about the backend (never any secret)."""
        return {
            "soloist": SoloistBinaryManager(self.mass).diagnostics(),
            "paired": await asyncio.to_thread(self._has_stored_session),
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

    def _prepare_data_dir(self) -> None:
        """Prepare the data dir for a fresh single-track spawn (blocking)."""
        self._data_dir.mkdir(parents=True, exist_ok=True)
        self._data_dir.chmod(0o700)
        # endpoint files from a previous run would point the client at a dead port
        for endpoint_file in (WS_ADDR_FILE, WS_PORT_FILE):
            (self._data_dir / endpoint_file).unlink(missing_ok=True)
        self._disable_engine_normalization()

    def _disable_engine_normalization(self) -> None:
        """
        Switch off soloist's own loudness normalization via its prefs store (blocking).

        The engine applies loudness normalization per the account's setting and
        exposes no CLI/WS control for it; it does honor the classic desktop
        prefs file in its data dir. Disabling it there keeps MA's own volume
        normalization the single loudness authority (no double normalization).
        Best-effort: the prefs file only exists after pairing, and an engine
        build that ignores the key simply keeps its account default.
        """
        for prefs_file in self._data_dir.glob("settings/Users/*-user/prefs"):
            try:
                lines = prefs_file.read_text(encoding="utf-8").splitlines()
                filtered = [line for line in lines if not line.startswith("audio.normalize_v2=")]
                filtered.append("audio.normalize_v2=false")
                if filtered != lines:
                    prefs_file.write_text("\n".join(filtered) + "\n", encoding="utf-8")
            except OSError as err:
                self.logger.debug("Unable to update soloist prefs %s: %s", prefs_file, err)

    def _single_track_args(self, spotify_uri: str) -> list[str]:
        """
        Build the soloist single-track argv.

        SECURITY: the argv carries the user's API key — it must never be logged
        or end up in any error message.
        """
        assert self._binary is not None
        return [
            str(self._binary),
            "--single-track",
            spotify_uri,
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

    async def _log_stderr(self, proc: AsyncProcess) -> None:
        """Log the daemon's stderr with the API key redacted."""
        api_key = self._api_key
        async for line in proc.iter_stderr():
            # the third-party binary's own output may echo argv (which carries
            # the api key), so redact it before logging
            text = line.replace(api_key, "<redacted>") if api_key else line
            self.logger.debug("[soloist] %s", text)

    async def _run_events(
        self, client: SoloistClient, state: _TrackState, sink: PipeSink, proc: AsyncProcess
    ) -> None:
        """Keep the WebSocket client connected and feed events into the track state."""
        if not await client.wait_until_ready(_STARTUP_TIMEOUT_S):
            state.fail("soloist did not publish its WebSocket endpoint")
            return
        state.client_ready.set()
        while True:
            try:
                await client.listen_events(partial(self._handle_event, state, sink))
            except asyncio.CancelledError:
                raise
            except (TimeoutError, OSError, ClientError, SoloistError) as err:
                # ordinary connection drop; reconnect while the daemon is alive
                # so the completion state does not go stale mid-track
                self.logger.debug("soloist events connection dropped: %s", err)
            except Exception as err:
                # a defect in event handling must surface loudly and fail the
                # stream: continuing would validate against stale state
                self.logger.exception("Unexpected error while handling soloist events")
                state.fail(f"event handling failed: {err}")
                return
            if proc.returncode is not None:
                return
            await asyncio.sleep(1)

    async def _handle_event(self, state: _TrackState, sink: PipeSink, event: SoloistEvent) -> None:
        """Track playback/auth state for the current item and gate the sink on buffering."""
        data = event.data
        if isinstance(data, SoloistAuthState):
            state.logged_out = not data.logged_in
            return
        if isinstance(data, SoloistTrackChanged):
            if data.item is not None and data.item.uri:
                self._observe_item(state, data.item.uri, None)
                await self._suspend_after_item_end(state, sink)
            return
        if isinstance(data, SoloistPositionSync):
            state.observe_position(data.position.position_ms)
            return
        if isinstance(data, SoloistPlaybackState):
            state.last_status = data.status
            if data.item is not None and data.item.uri:
                playback = data.item.decorations.get("playback")
                duration_ms = playback.get("duration_ms") if isinstance(playback, dict) else None
                self._observe_item(
                    state,
                    data.item.uri,
                    int(duration_ms) if isinstance(duration_ms, int | float) else None,
                )
                if await self._suspend_after_item_end(state, sink):
                    return
            if data.position is not None:
                state.observe_position(data.position.position_ms)
            if data.status == "playing":
                state.playing_seen = True
            # the pipe sink writes silence into the FIFO while soloist stalls
            # on rebuffering; suspending it keeps that silence out of the
            # delivered PCM (the paced reader simply waits)
            if state.demand_started and data.status in ("buffering", "playing"):
                try:
                    if data.status == "buffering":
                        await sink.suspend()
                    else:
                        await sink.resume()
                except Exception as err:
                    # fail closed: a sink with unknown suspend state would leak
                    # stall silence into (or withhold audio from) the PCM
                    state.fail(f"capture sink control failed: {err}")

    async def _suspend_after_item_end(self, state: _TrackState, sink: PipeSink) -> bool:
        """
        Suspend the sink once the requested item is over.

        This keeps the autoplayed next item from rendering into the capture
        path while the tail drains.

        :return: True when the item has ended (callers stop processing the event).
        """
        if not state.ended.is_set():
            return False
        if state.demand_started:
            # best effort: on failure the duration cap still bounds the drain
            with suppress(Exception):
                await sink.suspend()
        return True

    def _observe_item(self, state: _TrackState, uri: str, duration_ms: int | None) -> None:
        """Record whether the reported current item is (still) the requested one."""
        if uri == state.requested_uri:
            if duration_ms:
                state.duration_ms = duration_ms
            state.item_seen.set()
        elif state.item_seen.is_set():
            # Spotify moved on to another item (autoplay next-track right
            # before shutdown): everything for the requested item is delivered
            state.ended.set()

    async def _await_item_ready(self, state: _TrackState, proc: AsyncProcess) -> None:
        """Wait until the daemon reports the requested item as current."""
        try:
            async with asyncio.timeout(_STARTUP_TIMEOUT_S):
                exit_task = asyncio.ensure_future(proc.wait())
                item_task = asyncio.ensure_future(state.item_seen.wait())
                try:
                    await asyncio.wait({exit_task, item_task}, return_when=asyncio.FIRST_COMPLETED)
                finally:
                    exit_task.cancel()
                    item_task.cancel()
        except TimeoutError:
            self._raise_startup_error(state, proc, "timed out waiting for playback to start")
        if state.error or not state.item_seen.is_set():
            if proc.returncode == EXIT_CODE_BUILD_EXPIRED:
                # an expired build exits with code 10 right at spawn
                await self._handle_expired_build()
            self._raise_startup_error(state, proc, "exited before playback started")

    async def _handle_expired_build(self) -> NoReturn:
        """Replace the expired soloist build and fail the item with an accurate message."""
        try:
            # bypass the verify cache — it would hand back the same expired binary
            self._binary = await SoloistBinaryManager(self.mass).ensure_fresh(
                self._consent, force=True
            )
        except SoloistError as err:
            raise AudioError(
                "Spotify Soloist build expired and no replacement could be installed"
            ) from err
        raise AudioError("Spotify Soloist build expired; a replacement was installed, retry")

    def _raise_startup_error(self, state: _TrackState, proc: AsyncProcess, detail: str) -> None:
        """Raise the most specific startup failure for the requested item."""
        if state.error:
            raise AudioError(f"Spotify Soloist: {state.error}")
        if state.logged_out:
            # the stored session no longer logs in: route the user through the
            # setup flow instead of failing every item (mirrors librespot's
            # INVALID_CREDENTIALS handling)
            error = LoginFailed(
                "Spotify Soloist pairing lost",
                translation_key="soloist_pairing_required",
                translation_owner="provider.spotify",
            )
            if self.provider.available:
                self.provider.unload_with_error(error)
            raise error
        raise AudioError(f"Spotify Soloist {detail} for {state.requested_uri}")

    async def _cold_seek(self, client: SoloistClient, state: _TrackState, target_ms: int) -> None:
        """
        Seek the daemon to the target position before any PCM is released.

        The sink is still suspended, so no pre-seek audio enters the FIFO; PCM
        demand only starts once a position report confirms the seek landed.
        """
        state.seek_target_ms = target_ms
        # commands travel over the events connection
        if not state.client_ready.is_set():
            raise AudioError("Spotify Soloist WebSocket connection not ready for seek")
        # the engine silently drops a seek that arrives while the track is
        # still loading (verified via event trace), so re-send it until a
        # position anchor confirms it landed
        deadline = asyncio.get_running_loop().time() + _SEEK_CONFIRM_TIMEOUT_S
        while True:
            await client.seek(target_ms)
            with suppress(TimeoutError):
                async with asyncio.timeout(_SEEK_RETRY_INTERVAL_S):
                    await state.seek_confirmed.wait()
            if state.seek_confirmed.is_set():
                return
            if asyncio.get_running_loop().time() >= deadline:
                raise AudioError(f"Spotify Soloist did not confirm seeking to {target_ms}ms")

    async def _read_pcm(
        self,
        state: _TrackState,
        reader: asyncio.StreamReader,
        proc: AsyncProcess,
        seek_position: int,
    ) -> AsyncGenerator[bytes]:
        """
        Read the sink's FIFO at realtime pace and yield the item's PCM.

        Leading infrastructure silence is trimmed (bounded), delivery is capped
        at the item's known duration, and the read stops on item change or
        process exit — the FIFO itself never signals an end (the sink keeps
        rendering silence), so WebSocket state and the process delimit the item.
        """
        loop = asyncio.get_running_loop()
        lead_skipped = 0
        bytes_out = 0
        # doubles as the "first audio byte seen" marker
        pace_start: float | None = None
        stalled_for = 0.0
        drain_deadline: float | None = None
        while True:
            if state.error:
                raise AudioError(f"Spotify Soloist: {state.error}")
            # the duration may only arrive with a later playback_state event
            expected_bytes = state.expected_bytes(seek_position)
            if state.ended.is_set() or proc.returncode is not None:
                # the item is over (or the daemon exited), but its audio tail
                # may still sit in the FIFO/reader buffers — drain briefly
                # toward the duration cap, then stop (the FIFO itself keeps
                # delivering silence and never ends)
                if drain_deadline is None:
                    drain_deadline = loop.time() + _DRAIN_TIMEOUT_S
                if expected_bytes is None or loop.time() >= drain_deadline:
                    break
            try:
                chunk = await asyncio.wait_for(reader.read(_READ_CHUNK_SIZE), _READ_SLICE_S)
            except TimeoutError:
                # no data: the sink is suspended (rebuffering) or the
                # process is gone; bounded so a dead stream cannot hang
                stalled_for += _READ_SLICE_S
                if stalled_for >= _STALL_TIMEOUT_S:
                    raise AudioError(
                        f"Spotify Soloist audio stalled for {state.requested_uri}"
                    ) from None
                continue
            stalled_for = 0.0
            if not chunk:
                # writer end closed: the capture sink is gone (pulse restart)
                raise AudioError("Spotify Soloist capture sink was lost mid-stream")
            if pace_start is None:
                chunk, skipped = _trim_lead_silence(chunk, lead_skipped)
                lead_skipped += skipped
                if not chunk:
                    continue
                pace_start = loop.time()
            if expected_bytes is not None and bytes_out + len(chunk) > expected_bytes:
                chunk = chunk[: expected_bytes - bytes_out]
            bytes_out += len(chunk)
            yield chunk
            if expected_bytes is not None and bytes_out >= expected_bytes:
                break
            # pace the read at _PACE_RATE with an initial burst (no pacing
            # needed once the item is over)
            if pace_start is not None and proc.returncode is None and not state.ended.is_set():
                resume_at = (
                    pace_start + bytes_out / (_BYTES_PER_SECOND * _PACE_RATE) - _PACE_BURST_S
                )
                if (delay := resume_at - loop.time()) > 0:
                    await asyncio.sleep(delay)

    async def _evaluate_result(self, state: _TrackState, proc: AsyncProcess) -> None:
        """
        Validate that the delivered PCM covers the requested item.

        Exit code 0 is no proof of complete PCM (a starved stream pads with
        silence), so the last observed playback position is checked against the
        item's duration as well.
        """
        # a daemon still running once the item is delivered is closed right
        # away: with autoplay context it never exits on its own, and waiting
        # would starve the player at the track boundary (measured live) —
        # delivery integrity is validated by the position check below instead
        forced_close = proc.returncode is None
        await proc.close()
        returncode = proc.returncode
        if returncode == EXIT_CODE_BUILD_EXPIRED:
            await self._handle_expired_build()
        if not forced_close and returncode != 0:
            raise AudioError(
                f"Spotify Soloist exited with code {returncode} for {state.requested_uri}"
            )
        if not state.playing_seen:
            raise AudioError(f"Spotify Soloist never started playing {state.requested_uri}")
        # a missing final position is treated as incomplete: without a position
        # report there is no evidence the item played to its end; the tolerance
        # scales down for short items so it can never span the whole duration
        tolerance_ms = min(
            _INCOMPLETE_TOLERANCE_MS, state.duration_ms // 2 if state.duration_ms else 0
        )
        if state.duration_ms is not None and (
            state.last_position_ms is None
            or state.last_position_ms + tolerance_ms < state.duration_ms
        ):
            raise AudioError(
                f"Spotify Soloist delivered incomplete audio for {state.requested_uri} "
                f"(reached {state.last_position_ms or 0}ms of {state.duration_ms}ms)"
            )


class _TrackState:
    """Mutable WebSocket-observed state for one single-track run."""

    def __init__(self, requested_uri: str) -> None:
        """
        Initialize the state for one requested URI.

        :param requested_uri: The canonical Spotify URI being fetched.
        """
        self.requested_uri = requested_uri
        self.client_ready = asyncio.Event()
        # the requested uri was reported as the current item
        self.item_seen = asyncio.Event()
        # the requested item is over (item changed away)
        self.ended = asyncio.Event()
        self.seek_confirmed = asyncio.Event()
        self.seek_target_ms: int | None = None
        self.duration_ms: int | None = None
        self.last_position_ms: int | None = None
        self.last_status: str | None = None
        self.playing_seen = False
        self.logged_out = False
        self.demand_started = False
        self.error: str | None = None

    def fail(self, message: str) -> None:
        """Record a fatal error and unblock all waiters."""
        self.error = message
        self.item_seen.set()
        self.ended.set()

    def observe_position(self, position_ms: int) -> None:
        """Record a reported playback position (and confirm a pending seek)."""
        if self.ended.is_set():
            # positions reported after the item ended describe the (autoplay)
            # next item, not the requested one
            return
        # keep the furthest position: the daemon's stop/idle snapshot at the
        # end of the item reports position 0 and must not erase the progress
        # the completeness validation relies on (verified live)
        self.last_position_ms = max(self.last_position_ms or 0, position_ms)
        # the floor of 1 keeps a pre-seek report of position 0 from confirming
        # a small seek target that falls inside the tolerance window
        if self.seek_target_ms is not None and position_ms >= max(
            1, self.seek_target_ms - _SEEK_TOLERANCE_MS
        ):
            self.seek_confirmed.set()

    def expected_bytes(self, seek_position: int) -> int | None:
        """Return the PCM byte count the item should produce, when its duration is known."""
        if self.duration_ms is None:
            return None
        remaining_ms = max(0, self.duration_ms - seek_position * 1000)
        return remaining_ms * CAPTURE_SAMPLE_RATE // 1000 * _FRAME_BYTES


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
    Drop leading infrastructure silence from the head of the stream (bounded).

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
