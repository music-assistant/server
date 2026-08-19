"""
Spotify Soloist backend for the Spotify Connect provider.

Soloist is Spotify's official headless Connect client for Linux. The daemon is
installed/updated through ``SoloistBinaryManager``, plays its audio into a
private PulseAudio capture sink (read back by MA as a named pipe) and is driven
over its local WebSocket API (``SoloistClient``).

SECURITY NOTE: the daemon takes the user's personal API key on its command
line; nothing in this module may ever log the process argv.
"""

from __future__ import annotations

import asyncio
import re
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from music_assistant_models.enums import ContentType, StreamType
from music_assistant_models.errors import AudioError
from music_assistant_models.media_items import AudioFormat

from music_assistant.helpers.process import AsyncProcess
from music_assistant.helpers.pulse_capture import (
    CAPTURE_CHANNELS,
    CAPTURE_SAMPLE_RATE,
    PipeSink,
    get_pulse_capture_server,
)
from music_assistant.providers.spotify_connect.base import SpotifyConnectBackend
from music_assistant.providers.spotify_connect.models import (
    BackendEvent,
    BackendEventType,
    BackendStreamSource,
    BackendTrackMetadata,
)

from .runtime import (
    EXIT_CODE_BUILD_EXPIRED,
    BuildExpiredError,
    SoloistAuthState,
    SoloistBinaryManager,
    SoloistClient,
    SoloistContextChanged,
    SoloistDeviceChanged,
    SoloistError,
    SoloistErrorMessage,
    SoloistPlaybackState,
    SoloistPositionSync,
    SoloistTrackChanged,
    SoloistVolumeChanged,
)

if TYPE_CHECKING:
    import logging

    from music_assistant.helpers.pulse_capture import PulseCaptureServer
    from music_assistant.mass import MusicAssistant
    from music_assistant.providers.spotify_connect.models import (
        AudioChunkReader,
        BackendEventCallback,
    )

    from .runtime import SoloistEntity, SoloistEvent

# How Spotify-side volume changes are handled (see set_volume).
VOLUME_MODE_PLAYER_ONLY: Final = "player_only"
VOLUME_MODE_SYNC_SPOTIFY: Final = "sync_spotify"

# Bounded soloist playback cache (docs: 0 = unlimited, otherwise at least 100 MB).
CACHE_SIZE_MB: Final = 512

# Supervised-restart policy: give up after this many consecutive daemon
# failures; the counter resets once the events websocket delivers again.
MAX_RESTART_ATTEMPTS: Final = 5
RESTART_DELAY_S: Final = 2

# Proactive binary refresh interval: soloist builds expire 90 days after their
# build date, so a daily check swaps in a fresh build long before a long-lived
# instance would hit the expiry.
BINARY_REFRESH_INTERVAL_S: Final = 24 * 3600

# How often the supervisor checks whether the pulse daemon restarted (which
# invalidates the capture sink), so the sink is replaced proactively instead
# of on the (side-effect-free) stream request.
GENERATION_WATCH_INTERVAL_S: Final = 5

# playback_state/playback_changed status values mapped to normalized events;
# undocumented values degrade to OTHER.
_STATUS_EVENTS: Final[dict[str, BackendEventType]] = {
    "playing": BackendEventType.PLAYING,
    "paused": BackendEventType.PAUSED,
    "buffering": BackendEventType.BUFFERING,
    "stopped": BackendEventType.STOPPED,
    "idle": BackendEventType.STOPPED,
}


class SoloistBackend(SpotifyConnectBackend):
    """
    Spotify Connect backend wrapping a supervised Spotify Soloist daemon.

    The daemon plays into a private PulseAudio capture sink whose FIFO is
    consumed by the streams controller as a named pipe; control and state flow
    over the daemon's local WebSocket API.
    """

    def __init__(
        self,
        mass: MusicAssistant,
        *,
        instance_id: str,
        publish_name: str,
        name: str,
        logger: logging.Logger,
        event_callback: BackendEventCallback,
        api_key: str,
        consent: bool,
        volume_mode: str = VOLUME_MODE_PLAYER_ONLY,
    ) -> None:
        """
        Initialize the backend (cheap; the daemon is launched in ``start``).

        :param mass: The MusicAssistant instance.
        :param instance_id: The owning provider's instance id; keys the
            data/cache dirs and the capture sink name.
        :param publish_name: Device name advertised to the Spotify app.
        :param name: Display name of the owning provider instance (log messages).
        :param logger: Logger to use for diagnostics.
        :param event_callback: Awaited with a normalized BackendEvent for every
            state change the daemon reports.
        :param api_key: The user's personal Spotify Soloist API key (secret,
            kept out of all logs).
        :param consent: Whether the user consented to downloading the soloist
            binary from Spotify's CDN.
        :param volume_mode: VOLUME_MODE_PLAYER_ONLY to let MA/the player own the
            volume exclusively, VOLUME_MODE_SYNC_SPOTIFY to mirror the Spotify
            app's volume onto the MA player.
        """
        self.mass = mass
        self.logger = logger
        self.name = name
        self._publish_name = publish_name
        self._event_callback = event_callback
        self._api_key = api_key
        self._consent = consent
        self._volume_mode = volume_mode
        self._data_dir = Path(mass.storage_path) / "spotify_connect" / instance_id / "soloist-data"
        self._cache_dir = Path(mass.cache_path) / instance_id / "soloist-cache"
        # PA sink names end up in space-delimited module arguments and env vars
        self._sink_prefix = re.sub(r"[^A-Za-z0-9_.-]", "_", instance_id)
        self._binary: Path | None = None
        # digest of the build the running daemon was spawned from; the shared
        # install can move ahead of it when a sibling instance updates first
        self._build_sha: str | None = None
        self._server: PulseCaptureServer | None = None
        self._sink: PipeSink | None = None
        self._sink_generation: int = -1
        # serializes sink (re)creation against teardown in stop()
        self._sink_lock = asyncio.Lock()
        self._client: SoloistClient | None = None
        self._stop_called: bool = False
        self._daemon_task: asyncio.Task[None] | None = None
        self._events_task: asyncio.Task[None] | None = None
        self._refresh_task: asyncio.Task[None] | None = None
        self._watcher_task: asyncio.Task[None] | None = None
        self._proc: AsyncProcess | None = None
        self._restart_error_count = 0
        # set when a daemon close is intentional (sink replaced), so the
        # supervisor respawns immediately instead of counting a failure
        self._respawn_requested: bool = False
        # serializes all volume handling (sink compensation + event forwarding)
        self._volume_lock = asyncio.Lock()
        # last volume reported by the daemon (None until the first event)
        self._spotify_volume: int | None = None
        # guards the player_only 100%-pin so overlapping resets are not issued
        self._pin_in_flight: bool = False
        # whether an auth_state ever reported a completed login; a fresh daemon
        # reports logged_in=False while advertising for pairing, which is normal
        self._was_logged_in: bool = False
        self._last_context_uri: str | None = None
        self._last_track_uri: str | None = None
        # Soloist decodes internally and does not expose the source codec or
        # quality, so the display format reports an unknown source — we never
        # claim a lossless source or a specific source bit depth.
        self._audio_format = AudioFormat(
            content_type=ContentType.UNKNOWN,
            sample_rate=CAPTURE_SAMPLE_RATE,
            channels=CAPTURE_CHANNELS,
        )
        # the capture sink delivers fixed s32le/44.1kHz/2ch PCM (the pulse
        # capture format) — that is what actually arrives on the named pipe
        self._capture_format = AudioFormat(
            content_type=ContentType.PCM_S32LE,
            codec_type=ContentType.PCM_S32LE,
            sample_rate=CAPTURE_SAMPLE_RATE,
            bit_depth=32,
            channels=CAPTURE_CHANNELS,
        )

    @property
    def audio_format(self) -> AudioFormat:
        """Return the source audio format (advertised to clients for display)."""
        return self._audio_format

    @property
    def decoded_audio_format(self) -> AudioFormat:
        """Return the PCM format the capture sink's named pipe delivers."""
        return self._capture_format

    @property
    def stream_ends_on_pause(self) -> bool:
        """The pipe sink delivers silence on pause; the provider stops the player."""
        return False

    async def start(self) -> None:
        """Start the backend and its supervised soloist daemon."""
        # setup errors (unsupported platform, missing consent, download failure,
        # expired build) propagate so the provider load fails with a clear error
        manager = SoloistBinaryManager(self.mass)
        self._binary = await manager.ensure_fresh(self._consent)
        self._build_sha = manager.diagnostics().get("sha256")
        self._server = await get_pulse_capture_server(self.mass).acquire()
        try:
            await self._ensure_fresh_sink()

            def _prepare_dirs() -> None:
                self._data_dir.mkdir(parents=True, exist_ok=True)
                # the data dir persists the Spotify device identity and login
                # session; keep it readable by the MA user only
                self._data_dir.chmod(0o700)
                self._cache_dir.mkdir(parents=True, exist_ok=True)

            await asyncio.to_thread(_prepare_dirs)
            self._client = SoloistClient(self.mass, self._data_dir, self.logger)
            # Two self-healing supervisors: one keeps the daemon process alive,
            # the other keeps the events websocket connected (reconnecting
            # across daemon restarts).
            self._daemon_task = self.mass.create_task(self._daemon_runner())
            self._events_task = self.mass.create_task(self._events_runner())
            # Two housekeeping loops: a daily binary refresh (builds expire 90
            # days after their build date) and a watcher replacing the capture
            # sink after a pulse daemon restart.
            self._refresh_task = self.mass.create_task(self._binary_refresh_loop())
            self._watcher_task = self.mass.create_task(self._generation_watcher())
        except BaseException:
            # a failed startup aborts the provider load before unload() would
            # ever run — release everything acquired so far ourselves
            with suppress(Exception):
                await self.stop()
            raise

    async def stop(self) -> None:
        """Stop the daemon, its supervisors and the capture resources (idempotent)."""
        self._stop_called = True
        for task in (self._events_task, self._daemon_task, self._refresh_task, self._watcher_task):
            if task and not task.done():
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
        self._events_task = None
        self._daemon_task = None
        self._refresh_task = None
        self._watcher_task = None
        if (proc := self._proc) is not None:
            self._proc = None
            await proc.close()
        # the lock lets an in-flight _ensure_fresh_sink finish before the
        # capture resources go away (later callers fail on the stop flag)
        async with self._sink_lock:
            if (sink := self._sink) is not None:
                self._sink = None
                await sink.unload()
            if (server := self._server) is not None:
                self._server = None
                await server.release()

    async def get_stream_source(self) -> BackendStreamSource:
        """
        Return the NAMED_PIPE stream source delivering the capture sink's PCM.

        Side-effect-free (it also runs from queue preload): the sink is only
        read here — (re)creation is owned by the daemon supervisor and the
        generation watcher.

        ffmpeg reads the FIFO directly and is the single pacing owner:
        ``-readrate 1`` paces the read to realtime with a small initial burst
        as jitter headroom — nothing else may sleep-pace this audio path.

        :raises AudioError: No usable capture sink is currently available.
        """
        sink = self._sink
        server = self._server
        if sink is None or server is None or self._sink_generation != server.generation:
            raise AudioError("Spotify Connect capture sink is not available")
        return BackendStreamSource(
            stream_type=StreamType.NAMED_PIPE,
            path=str(sink.fifo_path),
            extra_input_args=["-readrate", "1", "-readrate_initial_burst", "0.5"],
        )

    def get_audio_reader(self) -> AudioChunkReader | None:
        """Return None: the audio is delivered through the NAMED_PIPE stream source."""
        return None

    async def play(self, uri: str, *, skip_to_uri: str | None = None) -> None:
        """
        Start playing a Spotify URI/context, making this device the active one.

        :param uri: Spotify URI (track, album, playlist, ...) — typically a context.
        :param skip_to_uri: Ignored — soloist's play command has no skip-to-track
            option, so playback starts at the beginning of the context.
        """
        assert self._client is not None
        # a bare play starts local playback without a Connect transfer, leaving
        # the Spotify apps unaware; claim active device status first
        await self._client.activate(await_result=True)
        await self._client.play(uri)
        if skip_to_uri:
            self.logger.debug(
                "skip_to_uri is not supported by soloist; starting %s from the beginning", uri
            )

    async def resume(self) -> None:
        """Resume playback on the active session."""
        assert self._client is not None
        await self._client.resume()

    async def pause(self) -> None:
        """Pause playback on the active session."""
        assert self._client is not None
        await self._client.pause()

    async def next(self) -> None:
        """Skip to the next track."""
        assert self._client is not None
        await self._client.skip_next()

    async def previous(self) -> None:
        """Skip to the previous track (or rewind the current one)."""
        assert self._client is not None
        await self._client.skip_prev()

    async def seek(self, position_ms: int) -> None:
        """
        Seek to an absolute position in the current track.

        :param position_ms: Target position in milliseconds.
        """
        assert self._client is not None
        await self._client.seek(position_ms)

    async def set_volume(self, volume: int) -> None:
        """
        Set the Spotify-side playback volume.

        :param volume: Absolute volume as a 0-100 percentage. In player-only
            mode the value is ignored and the daemon stays pinned at 100%.
        """
        assert self._client is not None
        if self._volume_mode == VOLUME_MODE_SYNC_SPOTIFY:
            # the daemon echoes the change back as a volume_changed event; the
            # provider's dedupe keeps that echo from bouncing back to the player
            async with self._volume_lock:
                await self._client.set_volume(volume)
            return
        # player_only: the MA player owns the volume, so the daemon is pinned at
        # 100% (unity PCM into the sink); only (re)send the pin when it is known
        # (or possibly) off
        async with self._volume_lock:
            if self._spotify_volume == 100:
                return
            await self._client.set_volume(100)

    async def _ensure_fresh_sink(self) -> PipeSink:
        """
        Return the capture sink, replacing it when the pulse daemon restarted.

        Only called from the supervisor paths (daemon spawn), never from the
        side-effect-free stream request.

        :raises AudioError: The backend is (being) stopped.
        """
        async with self._sink_lock:
            # checked under the lock: a concurrent stop() sets the flag before
            # it waits for the lock to tear down the capture resources
            if self._stop_called or self._server is None:
                raise AudioError("Spotify Connect backend is stopping")
            if self._sink is not None and self._sink_generation == self._server.generation:
                return self._sink
            if self._sink is not None:
                # sinks do not survive a daemon restart; this only cleans up the FIFO
                await self._sink.unload()
            self._sink = await PipeSink.create(self._server, self._sink_prefix)
            self._sink_generation = self._server.generation
            # the daemon plays into the sink named in its spawn env (PULSE_SINK),
            # so a running process must respawn against the recreated sink
            await self._close_daemon_for_respawn()
            return self._sink

    async def _recover_sink(self) -> None:
        """
        Fail-closed recovery: drop the sink and daemon so the supervisor rebuilds both.

        Used when the current sink can no longer be trusted (its compensation
        state is unknown after a failed volume call, or the pulse daemon
        restarted underneath it): audio through such a sink risks a stale
        reciprocal gain of up to 100x, so both the sink and the daemon are
        torn down and recreated by the daemon supervisor.
        """
        async with self._sink_lock:
            if self._stop_called:
                return
            if (sink := self._sink) is not None:
                self._sink = None
                with suppress(Exception):
                    await sink.unload()
            await self._close_daemon_for_respawn()

    async def _close_daemon_for_respawn(self) -> None:
        """Close a running daemon so its supervisor respawns it right away (not a failure)."""
        if (proc := self._proc) is not None:
            self._respawn_requested = True
            await proc.close()

    async def _generation_watcher(self) -> None:
        """Proactively replace the capture sink when the pulse daemon restarted."""
        while True:
            await asyncio.sleep(GENERATION_WATCH_INTERVAL_S)
            server = self._server
            if server is None or self._sink is None:
                continue
            if self._sink_generation != server.generation:
                self.logger.info("Pulse daemon restart detected; recreating the capture sink")
                await self._recover_sink()

    def _daemon_args(self) -> list[str]:
        """
        Build the soloist daemon's argv.

        SECURITY: the argv carries the user's API key — it must never be logged
        or end up in any error message.
        """
        assert self._binary is not None
        return [
            str(self._binary),
            "--device-name",
            self._publish_name,
            "--api-key",
            self._api_key,
            "--data-dir",
            str(self._data_dir),
            "--cache-dir",
            str(self._cache_dir),
            # bounded playback cache (0 would be unlimited)
            "--cache-size",
            str(CACHE_SIZE_MB),
            # start at 100%: MA (or the sink compensation) owns the real volume
            "--initial-volume",
            "100",
            # local WebSocket API on a free loopback port; the daemon publishes
            # the actual endpoint in its data dir where SoloistClient finds it
            "--ws",
            "127.0.0.1:0",
        ]

    async def _daemon_runner(self) -> None:
        """Run and supervise the soloist daemon, restarting (and refreshing) as needed."""
        # Loop forever; stop() cancels this task and the explicit stop-check below
        # handles a graceful exit without a restart.
        while True:
            proc: AsyncProcess | None = None
            returncode: int | None = None
            try:
                # the sink may have been replaced (pulse restart) while the
                # daemon was down; never spawn against a stale sink
                sink = await self._ensure_fresh_sink()
                server = self._server
                assert server is not None  # guaranteed by _ensure_fresh_sink
                # the explicit process name keeps AsyncProcess logging free of
                # the argv (which carries the API key)
                self._proc = proc = AsyncProcess(
                    self._daemon_args(),
                    stderr=True,
                    name=f"soloist[{self.name}]",
                    env=server.child_env(sink.sink_name),
                )
                await proc.start()
                self.logger.info("Started Spotify Connect background daemon [%s]", self.name)
                await self._reset_volume_state(sink)
                async for line in proc.iter_stderr():
                    # the third-party binary's own output may echo argv (which
                    # carries the api key), so redact it before logging
                    text = line.replace(self._api_key, "<redacted>") if self._api_key else line
                    self.logger.debug("[%s] %s", self.name, text)
            except asyncio.CancelledError:
                raise
            except Exception as err:
                self.logger.warning("soloist daemon error [%s]: %s", self.name, err)
            finally:
                if proc:
                    await proc.close()
                    returncode = proc.returncode
                # The daemon — and thus the Spotify session — is gone. Tell the
                # provider so a dead/restarting daemon isn't treated as active and
                # controllable; a fresh 'active' event re-establishes it on reconnect.
                self._proc = None
                try:
                    await self._event_callback(BackendEvent(BackendEventType.CONNECTION_LOST))
                except Exception:
                    # never let a callback error replace a propagating
                    # cancellation or kill the daemon supervisor
                    self.logger.exception("Error while handling daemon exit")
            if self._stop_called:
                break
            if self._respawn_requested:
                # intentional close (the sink was replaced): respawn right away,
                # this is not a daemon failure
                self._respawn_requested = False
                continue
            self.logger.info("Spotify Connect background daemon stopped for %s", self.name)
            if returncode == EXIT_CODE_BUILD_EXPIRED and not await self._refresh_expired_binary():
                return
            self._restart_error_count += 1
            if self._restart_error_count >= MAX_RESTART_ATTEMPTS:
                await self._event_callback(
                    BackendEvent(
                        BackendEventType.FATAL_ERROR,
                        # fatal errors are plain (non-localized) strings for now,
                        # matching the go-librespot backend
                        error="soloist daemon failed to start multiple times.",
                    )
                )
                return
            await asyncio.sleep(RESTART_DELAY_S)

    async def _reset_volume_state(self, sink: PipeSink) -> None:
        """
        Realign the volume bookkeeping with a freshly spawned daemon.

        :param sink: The capture sink the daemon plays into.
        """
        # the daemon always starts at --initial-volume 100; without this reset a
        # stale reciprocal sink compensation from before a crash would amplify
        # and clip the captured audio (sync_spotify mode)
        async with self._volume_lock:
            self._spotify_volume = 100
            try:
                await sink.set_volume(100)
            except Exception as err:
                # fail closed: a stale reciprocal gain may still be active on
                # the sink — recreate sink and daemon rather than clipping
                self.logger.warning(
                    "Failed to reset capture sink volume (%s); recreating capture sink", err
                )
                await self._recover_sink()

    async def _refresh_expired_binary(self) -> bool:
        """
        Replace the expired soloist build before the next daemon restart.

        :return: True when the supervisor may restart the daemon, False when a
            fatal error was reported and the supervisor must stop.
        """
        self.logger.warning("soloist build expired; looking for a replacement build")
        try:
            # force: the daemon itself reported expiry, so the recently-verified
            # fast path must not hand back the same binary
            manager = SoloistBinaryManager(self.mass)
            self._binary = await manager.ensure_fresh(self._consent, force=True)
            self._build_sha = manager.diagnostics().get("sha256")
        except BuildExpiredError:
            await self._event_callback(
                BackendEvent(
                    BackendEventType.FATAL_ERROR,
                    error=(
                        "The Spotify Soloist build expired and no replacement could be "
                        "installed. Check the server's internet connection and reload "
                        "this provider."
                    ),
                )
            )
            return False
        except SoloistError as err:
            # transient refresh problem: keep restarting, bounded by the failure cap
            self.logger.warning("Unable to refresh the expired soloist build: %s", err)
        return True

    async def _binary_refresh_loop(self) -> None:
        """Periodically refresh the soloist binary ahead of its 90-day build expiry."""
        while True:
            await asyncio.sleep(BINARY_REFRESH_INTERVAL_S)
            try:
                manager = SoloistBinaryManager(self.mass)
                # a replaced build keeps the same install path, so compare the
                # install metadata's digest against the build this daemon runs
                # (a sibling instance may have updated the shared install)
                binary = await manager.ensure_fresh(self._consent)
                new_sha = manager.diagnostics().get("sha256")
                if binary == self._binary and new_sha == self._build_sha:
                    continue
                self.logger.info("A fresh soloist build was installed; restarting the daemon")
                self._binary = binary
                self._build_sha = new_sha
                await self._close_daemon_for_respawn()
            except asyncio.CancelledError:
                raise
            except Exception as err:
                self.logger.warning("Periodic soloist binary refresh failed: %s", err)

    async def _events_runner(self) -> None:
        """Keep the soloist events websocket connected, reconnecting as needed."""
        assert self._client is not None
        while not self._stop_called:
            try:
                if not await self._client.wait_until_ready():
                    await asyncio.sleep(RESTART_DELAY_S)
                    continue
                await self._client.listen_events(self._handle_event)
            except asyncio.CancelledError:
                raise
            except Exception as err:
                self.logger.debug("soloist events websocket dropped: %s", err)
            if not self._stop_called:
                await asyncio.sleep(RESTART_DELAY_S)

    async def _handle_event(self, event: SoloistEvent) -> None:
        """Adapt a raw soloist event and emit its normalized counterpart."""
        self.logger.debug("Received %s event [%s]", event.type, self.name)
        # A delivered event means the websocket — and thus the daemon — is
        # healthy: reset the restart backoff counter the daemon supervisor uses.
        # (The endpoint files the events runner polls can be stale leftovers
        # from a previous run, so the reset cannot happen on wait_until_ready.)
        self._restart_error_count = 0
        if isinstance(event.data, SoloistVolumeChanged):
            await self._handle_volume_changed(event.data.volume)
            return
        if (
            isinstance(event.data, SoloistPlaybackState)
            and event.data.volume is not None
            and event.data.volume != self._spotify_volume
        ):
            # a playback_state snapshot (e.g. right after a websocket reconnect)
            # carries the daemon's current volume; resync the pin/compensation
            # before forwarding the playback event itself
            await self._handle_volume_changed(event.data.volume)
            if self._sink is None:
                # fail-closed recovery tore down the capture path; drop this
                # stale snapshot — the respawned daemon reports fresh state
                return
        if (
            isinstance(event.data, SoloistPlaybackState)
            and (item := event.data.item) is not None
            and item.uri != self._last_track_uri
        ):
            # the snapshot describes a track we have no metadata for yet (an
            # already-playing session at (re)connect); emit its metadata so it
            # does not stay stale until the next track_changed
            await self._event_callback(
                self._make_event(BackendEventType.METADATA, metadata=_entity_metadata(item))
            )
        await self._event_callback(self._translate_event(event))

    async def _handle_volume_changed(self, volume: int) -> None:
        """
        Apply a Spotify-side volume change according to the configured volume mode.

        :param volume: The reported volume as a 0-100 percentage.
        """
        async with self._volume_lock:
            self._spotify_volume = volume
            if self._volume_mode != VOLUME_MODE_SYNC_SPOTIFY:
                # player_only: MA/the player owns the volume. Keep the daemon
                # pinned at 100% so the captured PCM stays at unity gain, and
                # never forward VOLUME events (they would fight the MA volume).
                if volume != 100 and self._client is not None and not self._pin_in_flight:
                    self._pin_in_flight = True
                    try:
                        await self._client.set_volume(100)
                    except Exception as err:
                        self.logger.debug("Failed to reset soloist volume: %s", err)
                        # mark the volume unknown so the next snapshot
                        # reporting the same value still retries the pin
                        self._spotify_volume = None
                    finally:
                        self._pin_in_flight = False
                return
            # sync_spotify: the daemon attenuates the PCM it plays into the sink
            # with Spotify's cubic volume curve; undo that with the reciprocal
            # raw sink gain (sink_pct = 10000 / spotify_pct) so the FIFO always
            # carries unity-gain audio, then forward the volume for the MA
            # player to apply (subject to the provider's dedupe/grace policy).
            # NOTE: the percentage reciprocal is the exact linear inverse
            # because pulse's software volume is cubic in the percentage too
            # (pa_sw_volume_to_linear(p) = (p/100)^3) — validated by capture
            # measurement, do not "fix" this to an explicit cube.
            if self._sink is not None:
                # explicit zero handling: no reciprocal exists, silence the sink
                sink_pct = 0.0 if volume <= 0 else round(10000 / volume, 2)
                try:
                    await self._sink.set_volume(sink_pct)
                except Exception as err:
                    # fail closed: the compensation state is now unknown, so
                    # recreate sink and daemon and do NOT forward the volume —
                    # the player must not adopt a value whose compensation
                    # never applied
                    self.logger.warning(
                        "Failed to set capture sink volume (%s); recreating capture sink", err
                    )
                    await self._recover_sink()
                    return
            await self._event_callback(
                self._make_event(BackendEventType.VOLUME, volume=max(0, volume))
            )

    def _translate_event(self, event: SoloistEvent) -> BackendEvent:
        """Map a raw soloist event onto the normalized BackendEvent model."""
        data = event.data
        if isinstance(data, SoloistAuthState):
            if not data.logged_in:
                if self._was_logged_in:
                    # an established login was lost mid-session: real auth loss
                    self._was_logged_in = False
                    return self._make_event(BackendEventType.AUTH_REQUIRED)
                # a fresh daemon reports logged_in=False while advertising for
                # pairing; that is the normal pre-pairing state, not an auth loss
                return self._make_event(BackendEventType.SESSION_INACTIVE)
            self._was_logged_in = True
            return self._make_event(
                BackendEventType.SESSION_ACTIVE
                if data.is_active
                else BackendEventType.SESSION_INACTIVE
            )
        if isinstance(data, SoloistDeviceChanged):
            return self._make_event(
                BackendEventType.SESSION_ACTIVE
                if data.is_active
                else BackendEventType.SESSION_INACTIVE
            )
        if isinstance(data, SoloistPlaybackState):
            # covers both the playback_state snapshot and the playback_changed delta
            self._cache_uris(track=data.item, context=data.context)
            return self._make_event(_STATUS_EVENTS.get(data.status, BackendEventType.OTHER))
        if isinstance(data, SoloistTrackChanged) and data.item is not None:
            self._cache_uris(track=data.item)
            return self._make_event(BackendEventType.METADATA, metadata=_entity_metadata(data.item))
        if isinstance(data, SoloistPositionSync):
            return self._make_event(
                BackendEventType.POSITION, position=data.position.position_ms // 1000
            )
        if isinstance(data, SoloistErrorMessage):
            return self._make_event(BackendEventType.ERROR, error=data.message)
        if isinstance(data, SoloistContextChanged):
            self._cache_uris(context=data.context)
        # queue/options/context changes, command acks and unknown events
        return self._make_event(BackendEventType.OTHER)

    def _make_event(self, event_type: BackendEventType, **fields: Any) -> BackendEvent:
        """Build a BackendEvent carrying the latest known context/track uris."""
        return BackendEvent(
            event_type,
            context_uri=self._last_context_uri,
            track_uri=self._last_track_uri,
            **fields,
        )

    def _cache_uris(
        self, *, track: SoloistEntity | None = None, context: SoloistEntity | None = None
    ) -> None:
        """Remember the latest context/track uris seen on the event stream."""
        if track is not None and track.uri:
            self._last_track_uri = track.uri
        if context is not None and context.uri:
            self._last_context_uri = context.uri


def _entity_metadata(item: SoloistEntity) -> BackendTrackMetadata:
    """
    Extract normalized track metadata from an entity's decorations.

    Mapped from the observed 1.3.7 wire format: the artist lives under
    ``creators[].entity``, the album under ``parent.entity`` and artwork
    under ``visual_identity.cover[]`` — all traversed defensively since the
    decorations bag is extensible.

    :param item: The soloist entity (track) to extract the metadata from.
    """
    decorations = item.decorations or {}
    identity = _as_dict(decorations.get("identity"))
    playback = _as_dict(decorations.get("playback"))
    duration_ms = playback.get("duration_ms")
    return BackendTrackMetadata(
        track_uri=item.uri,
        title=_as_str(identity.get("name")) or _as_str(identity.get("title")),
        artist=_creator_name(decorations.get("creators")),
        album=_nested_entity_name(decorations.get("parent")),
        image_url=_cover_url(decorations),
        duration=int(duration_ms) // 1000 if isinstance(duration_ms, int | float) else None,
    )


def _as_dict(value: Any) -> dict[str, Any]:
    """Return the value if it is a dict, an empty dict otherwise."""
    return value if isinstance(value, dict) else {}


def _as_str(value: Any) -> str | None:
    """Return the value if it is a non-empty string, None otherwise."""
    return value if isinstance(value, str) and value else None


def _entity_name(value: Any) -> str | None:
    """Return the display name of a nested entity-like decoration value."""
    if isinstance(value, str):
        return value or None
    if isinstance(value, dict):
        return _as_str(value.get("name")) or _as_str(_as_dict(value.get("identity")).get("name"))
    return None


def _nested_entity_name(value: Any) -> str | None:
    """Return the identity name of a ``{"entity": {...}}`` decoration (album parent)."""
    entity = _as_dict(_as_dict(value).get("entity"))
    return _entity_name(_as_dict(entity.get("decorations"))) or _entity_name(entity)


def _creator_name(value: Any) -> str | None:
    """Return the name of the first creator credit (``creators[].entity``)."""
    if isinstance(value, list) and value:
        return _nested_entity_name(value[0])
    return None


def _cover_url(decorations: dict[str, Any]) -> str | None:
    """
    Return the artwork url from ``visual_identity.cover[]``.

    Prefers the large rendition; falls back to the last (largest) entry.
    """
    covers = _as_dict(decorations.get("visual_identity")).get("cover")
    if not isinstance(covers, list) or not covers:
        return None
    for entry in covers:
        if isinstance(entry, dict) and entry.get("size") == "large":
            if url := _as_str(entry.get("url")):
                return url
    for entry in reversed(covers):
        if isinstance(entry, dict) and (url := _as_str(entry.get("url"))):
            return url
    return None
