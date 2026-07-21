"""Unified AirPlay/RAOP stream session logic for AirPlay devices."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncGenerator
from contextlib import suppress
from typing import TYPE_CHECKING

from music_assistant_models.errors import PlayerCommandFailed

from music_assistant.constants import CONF_SYNC_ADJUST
from music_assistant.controllers.streams.audio_processing import get_media_session_id
from music_assistant.helpers.ffmpeg import FFMpeg

from .constants import AIRPLAY_LATE_JOIN_MIN_HEADROOM_MS, StreamingProtocol
from .helpers import get_final_output_format
from .stream import AirPlayStream

if TYPE_CHECKING:
    from music_assistant_models.media_items import AudioFormat

    from music_assistant.models.player import PlayerMedia

    from .player import AirPlayPlayer
    from .provider import AirPlayProvider


class AirPlayStreamSession:
    """Stream session (RAOP or AirPlay2) to one or more players."""

    def __init__(
        self,
        airplay_provider: AirPlayProvider,
        sync_clients: list[AirPlayPlayer],
        pcm_format: AudioFormat,
        media: PlayerMedia,
    ) -> None:
        """
        Initialize AirPlayStreamSession.

        :param airplay_provider: The AirPlay provider instance.
        :param sync_clients: List of AirPlay players to stream to.
        :param pcm_format: PCM format of the input stream.
        :param media: Queue media that owns the stream session.
        """
        assert sync_clients
        self.prov = airplay_provider
        self.mass = airplay_provider.mass
        self.pcm_format = pcm_format
        self.media = media
        self.sync_clients = sync_clients
        self._audio_source_task: asyncio.Task[None] | None = None
        self._stop_task: asyncio.Task[None] | None = None
        self._players = {player.player_id: player for player in sync_clients}
        self._player_streams: dict[str, AirPlayStream] = {}
        self._player_ffmpeg: dict[str, FFMpeg] = {}
        self._client_stop_locks: dict[str, asyncio.Lock] = {}
        self._starting_clients: dict[str, object] = {}
        self._lock = asyncio.Lock()
        self.start_unix_ms: int = 0
        self.start_time: float = 0.0
        self.wait_start: float = 0.0
        self.seconds_streamed: float = 0
        # Timing source for the whole session, decided once in start() and applied
        # identically to every native AirPlay 2 member (and any late joiner) so a
        # sync group can never mix shared-PTP and NTP members.
        self.use_shared_ptp: bool = False
        # Raw PCM ring buffer for late joiners. When a late joiner arrives we
        # send this buffer to prime its pipeline so it starts playing quickly
        # instead of waiting for the full pipeline to fill from scratch.
        # Must cover how far the feed runs ahead of the audible position
        # (the binary's ring cap of latency+2s plus the ~2s receiver buffer)
        # with enough slack left to prime the joiner.
        self._pcm_buffer = bytearray()
        self._pcm_buffer_max = pcm_format.pcm_sample_size * 10  # 10 seconds

    @property
    def accepting_clients(self) -> bool:
        """Return whether this session can accept a live late join."""
        return self._session_can_accept_client()

    async def start(
        self,
        audio_source: AsyncGenerator[bytes],
        expected_generations: dict[str, int] | None = None,
    ) -> None:
        """
        Initialize the stream session for all players.

        :param audio_source: PCM audio source for the session.
        :param expected_generations: Reserved player generations for this start.
        """
        player_generations = {
            player.player_id: (
                expected_generations[player.player_id]
                if expected_generations and player.player_id in expected_generations
                else player.reserve_stream_generation()
            )
            for player in self.sync_clients
        }
        start_tasks: list[asyncio.Task[None]] = []
        try:
            cur_time = time.time()
            wait_start = max(p.wait_start for p in self.sync_clients)
            wait_start_seconds = wait_start / 1000
            self.wait_start = wait_start_seconds
            self.start_time = cur_time + wait_start_seconds
            # group start as plain unix epoch milliseconds: every member of the
            # sync group receives the exact same value (--start-unix-ms)
            self.start_unix_ms = int(self.start_time * 1000)
            # Decide the PTP timing source ONCE for the whole session and apply it to
            # every member below, so a native AirPlay 2 group never mixes shared-PTP
            # and NTP members (which drift audibly against each other).
            self.use_shared_ptp = await self._resolve_shared_ptp()
            start_tasks = [
                asyncio.create_task(
                    self._start_client(
                        player,
                        self.start_unix_ms,
                        self.use_shared_ptp,
                        player_generations[player.player_id],
                    )
                )
                for player in self.sync_clients
            ]
            await asyncio.gather(*start_tasks)
            async with self._lock:
                if self._stop_task is not None or not self.sync_clients:
                    raise PlayerCommandFailed("AirPlay stream session was superseded")
                self._audio_source_task = asyncio.create_task(self._audio_streamer(audio_source))
            await asyncio.gather(
                *[
                    self._wait_for_client_connection(player, stream)
                    for player in self.sync_clients
                    if (stream := self._player_streams.get(player.player_id))
                ]
            )
            async with self._lock:
                if self._stop_task is not None or not any(
                    self._player_streams.get(player.player_id) for player in self.sync_clients
                ):
                    raise PlayerCommandFailed("All AirPlay clients were removed during startup")
        except asyncio.CancelledError:
            await self._cleanup_failed_start(audio_source, start_tasks)
            raise
        except Exception as err:
            await self._cleanup_failed_start(audio_source, start_tasks)
            raise PlayerCommandFailed("Playback failed to start") from err

    async def stop(self) -> None:
        """Stop playback and cleanup."""
        if self._stop_task is None:
            self._stop_task = asyncio.create_task(self._stop())
        stop_task = self._stop_task
        try:
            await asyncio.shield(stop_task)
        except asyncio.CancelledError:
            await stop_task
            raise

    async def remove_client(
        self,
        airplay_player: AirPlayPlayer,
        reason: str = "client removed",
        expected_stream: AirPlayStream | None = None,
        reservation: object | None = None,
        preserve_reservation: bool = False,
    ) -> None:
        """
        Remove a sync client from the session.

        :param airplay_player: The player to remove from the session.
        :param reason: Short human-readable reason for the removal, used in teardown logs.
        :param expected_stream: Only remove the client if this stream is still session-owned.
        :param reservation: Only remove the client if this pending generation is still current.
        :param preserve_reservation: Keep a newer pending generation for this session.
        """
        cleanup_task = asyncio.create_task(
            self._remove_client(
                airplay_player,
                reason,
                expected_stream,
                reservation,
                preserve_reservation,
            )
        )
        try:
            await asyncio.shield(cleanup_task)
        except asyncio.CancelledError:
            await cleanup_task
            raise

    async def stop_client(
        self,
        airplay_player: AirPlayPlayer,
        reason: str = "stop_client called",
        expected_stream: AirPlayStream | None = None,
        constrain_stream: bool = False,
    ) -> None:
        """
        Stop a client's stream and ffmpeg.

        :param airplay_player: The player to stop.
        :param reason: Short human-readable reason for the teardown, used in debug logs.
        :param expected_stream: Only stop this exact session stream generation.
        :param constrain_stream: Require the current stream mapping to match
            ``expected_stream``, including when both are None.
        """
        self.prov.logger.debug(
            "AirPlay session teardown: session=%s client=%s reason=%s",
            id(self),
            airplay_player.player_id,
            reason,
        )
        cleanup_task = asyncio.create_task(
            self._stop_client_locked(
                airplay_player,
                expected_stream,
                constrain_stream or expected_stream is not None,
            )
        )
        try:
            await asyncio.shield(cleanup_task)
        except asyncio.CancelledError:
            await cleanup_task
            raise

    async def add_client(
        self,
        airplay_player: AirPlayPlayer,
        expected_generation: int | None = None,
    ) -> bool:
        """
        Add a sync client to the session as a late joiner.

        Uses the PCM ring buffer to prime the late joiner's pipeline so it
        starts playing quickly. All work happens under the lock to ensure
        ``seconds_streamed`` and the buffer are consistent.

        Devices generally cannot honour a start anchor that is in the
        past — they just play whatever the pipe gives them, trailing the
        group by the deficit. To stay in sync we therefore push the late
        joiner's ``start_at`` into the future (at least ``wait_start`` ahead
        of now) and trim the corresponding amount from the head of the
        buffered PCM, so the first sample we send maps to the correct future
        stream position.

        1. Snapshot the ring buffer and calculate how many seconds it holds.
        2. Map the buffer's first byte to its stream position; if the
           resulting start anchor is in the past, shift it forward to
           ``now + min_headroom`` and trim that many seconds from the buffer
           head so timing stays aligned with the rest of the group.
        3. Start ffmpeg+CLI, write the (possibly trimmed) buffer into ffmpeg
           to prime the pipe while cliairplay is still connecting, then add to
           sync_clients so the audio streamer continues seamlessly.

        :param expected_generation: Player stream generation reserved for this join.
        :return: True if this generation joined the running session.
        """
        player_generation = (
            airplay_player.reserve_stream_generation()
            if expected_generation is None
            else expected_generation
        )
        async with self._lock:
            if not self._session_can_accept_client():
                return False
            reservation = object()
            self._starting_clients[airplay_player.player_id] = reservation

        stream: AirPlayStream | None = None
        try:
            stream = self._prepare_client_stream(airplay_player)
            try:
                replacement = airplay_player.stream_replacement(
                    stream,
                    is_current=lambda: self._client_start_is_current(
                        airplay_player,
                        reservation,
                    ),
                    expected_generation=player_generation,
                    before_replace=lambda: self._prepare_client_replacement(airplay_player),
                )
                async with replacement:
                    now = await self._start_late_joiner(
                        airplay_player,
                        stream,
                        reservation,
                    )
                    if now is None:
                        await stream.stop(force=True)
                        if airplay_player.stream is stream:
                            airplay_player.stream = None
                        await self._release_client_reservation(
                            airplay_player,
                            reservation,
                        )
                        return False
            except PlayerCommandFailed:
                self.prov.logger.debug(
                    "Late join for %s was superseded before startup",
                    airplay_player.player_id,
                )
                await self._finalize_late_join(
                    airplay_player,
                    stream,
                    reservation,
                    player_generation,
                    force_failure=True,
                )
                return False

            # Wait for device connection outside the lock.
            if self._player_streams.get(airplay_player.player_id) is stream:
                try:
                    await stream.wait_for_connection()
                    self.prov.logger.debug(
                        "Late joiner %s: device connected after %.2fs",
                        airplay_player.player_id,
                        time.time() - now,
                    )
                except asyncio.CancelledError:
                    raise
                except (TimeoutError, PlayerCommandFailed) as err:
                    self.prov.logger.warning(
                        "Late joiner %s: device connection failed after %.2fs: %s",
                        airplay_player.player_id,
                        time.time() - now,
                        err,
                    )
                    await self._finalize_late_join(
                        airplay_player,
                        stream,
                        reservation,
                        player_generation,
                        force_failure=True,
                    )
                    return False
            return await self._finalize_late_join(
                airplay_player,
                stream,
                reservation,
                player_generation,
            )
        except BaseException:
            if stream is None:
                await self._release_client_reservation(airplay_player, reservation)
            else:
                cleanup_task = asyncio.create_task(
                    self._finalize_late_join(
                        airplay_player,
                        stream,
                        reservation,
                        player_generation,
                        force_failure=True,
                    )
                )
                try:
                    await asyncio.shield(cleanup_task)
                except asyncio.CancelledError:
                    await cleanup_task
            raise

    async def _remove_client(
        self,
        airplay_player: AirPlayPlayer,
        reason: str,
        expected_stream: AirPlayStream | None,
        reservation: object | None,
        preserve_reservation: bool,
    ) -> None:
        """Remove and tear down one client as a single cancellation-safe transaction."""
        async with self._lock:
            current_stream = self._player_streams.get(airplay_player.player_id)
            if expected_stream is not None and current_stream is not expected_stream:
                return
            if (
                reservation is not None
                and self._starting_clients.get(airplay_player.player_id) is not reservation
            ):
                return
            if (
                airplay_player not in self.sync_clients
                and airplay_player.player_id not in self._players
                and airplay_player.player_id not in self._starting_clients
            ):
                return
            if airplay_player in self.sync_clients:
                self.sync_clients.remove(airplay_player)
            if not preserve_reservation:
                self._starting_clients.pop(airplay_player.player_id, None)
        await self._cleanup_after_removal(
            airplay_player,
            reason=reason,
            expected_stream=expected_stream or current_stream,
            constrain_stream=True,
        )

    def _session_can_accept_client(self) -> bool:
        """Return whether this session can start another client."""
        if not self.sync_clients or self._stop_task is not None:
            return False
        if self._audio_source_task is None or self._audio_source_task.done():
            return False
        first_client = self.sync_clients[0]
        first_stream = self._player_streams.get(first_client.player_id)
        return bool(first_stream and first_stream.running)

    def _client_start_is_current(
        self,
        airplay_player: AirPlayPlayer,
        reservation: object | None,
    ) -> bool:
        """Return whether a queued client start still belongs to this session."""
        if self._stop_task is not None:
            return False
        if reservation is None:
            return airplay_player in self.sync_clients
        return self._starting_clients.get(airplay_player.player_id) is reservation

    async def _release_client_reservation(
        self,
        airplay_player: AirPlayPlayer,
        reservation: object,
    ) -> None:
        """Release a pending reservation if this generation still owns it."""
        async with self._lock:
            if self._starting_clients.get(airplay_player.player_id) is reservation:
                self._starting_clients.pop(airplay_player.player_id)
            should_stop = not self.sync_clients and not self._starting_clients
        if should_stop:
            await self._cancel_audio_source()

    async def _finalize_late_join(
        self,
        airplay_player: AirPlayPlayer,
        stream: AirPlayStream,
        reservation: object,
        player_generation: int,
        force_failure: bool = False,
    ) -> bool:
        """Atomically commit or tear down a completed late-join generation."""
        async with self._lock:
            owns_reservation = self._starting_clients.get(airplay_player.player_id) is reservation
            owns_stream = self._player_streams.get(airplay_player.player_id) is stream
            is_member = airplay_player in self.sync_clients
            succeeded = (
                not force_failure
                and self._stop_task is None
                and owns_reservation
                and owns_stream
                and is_member
                and airplay_player.is_stream_generation_current(player_generation)
            )
            if owns_reservation:
                self._starting_clients.pop(airplay_player.player_id)
            cleanup_stream = not succeeded and owns_stream
            if cleanup_stream and is_member:
                self.sync_clients.remove(airplay_player)
            should_stop = not self.sync_clients and not self._starting_clients

        if cleanup_stream:
            await self._cleanup_after_removal(
                airplay_player,
                reason="late join superseded during finalization",
                expected_stream=stream,
                constrain_stream=True,
            )
        elif should_stop:
            await self._cancel_audio_source()
        return succeeded

    async def _wait_for_client_connection(
        self,
        airplay_player: AirPlayPlayer,
        stream: AirPlayStream,
    ) -> None:
        """Wait for a client unless that exact session stream was removed."""
        try:
            await stream.wait_for_connection()
        except Exception:
            if (
                airplay_player not in self.sync_clients
                or self._player_streams.get(airplay_player.player_id) is not stream
            ):
                return
            raise

    async def _start_late_joiner(
        self,
        airplay_player: AirPlayPlayer,
        stream: AirPlayStream,
        reservation: object,
    ) -> float | None:
        """Start and prime a late joiner while holding the session lock."""
        async with self._lock:
            if not self._session_can_accept_client():
                return None
            if self._starting_clients.get(airplay_player.player_id) is not reservation:
                return None

            self._players[airplay_player.player_id] = airplay_player
            now = time.time()
            pcm_sample_size = self.pcm_format.pcm_sample_size
            frame_size = (self.pcm_format.bit_depth // 8) * self.pcm_format.channels
            # Snapshot the buffer and map its first byte to the session timeline.
            buffered_pcm = bytes(self._pcm_buffer)
            buffer_seconds = len(buffered_pcm) / pcm_sample_size
            first_byte_pos = self.seconds_streamed - buffer_seconds
            start_at = self.start_time + first_byte_pos

            # Keep enough lead for cliairplay to buffer before the audible start.
            min_headroom = max(
                self.wait_start,
                airplay_player.wait_start / 1000,
                AIRPLAY_LATE_JOIN_MIN_HEADROOM_MS / 1000,
            )
            target_start_at = now + min_headroom
            trim_seconds = 0.0
            if start_at < target_start_at:
                # Shift the anchor and trim the same duration so the first sample
                # remains aligned with the rest of the group.
                trim_seconds = target_start_at - start_at
                trim_bytes = int(trim_seconds * pcm_sample_size)
                trim_bytes -= trim_bytes % frame_size
                if trim_bytes >= len(buffered_pcm):
                    # The next live chunk will start the joiner when no buffer remains.
                    buffered_pcm = b""
                    start_at = self.start_time + self.seconds_streamed
                else:
                    buffered_pcm = buffered_pcm[trim_bytes:]
                    start_at += trim_bytes / pcm_sample_size
                start_at = max(start_at, target_start_at)

            start_unix_ms = int(start_at * 1000)
            self.prov.logger.debug(
                "Late joiner %s: sending %.2fs of buffered audio, "
                "stream_pos=%.2fs, first_byte_pos=%.2fs, "
                "start_at is %.2fs from now (min_headroom=%.2fs, trimmed=%.2fs)",
                airplay_player.player_id,
                len(buffered_pcm) / pcm_sample_size,
                self.seconds_streamed,
                first_byte_pos,
                start_at - now,
                min_headroom,
                trim_seconds,
            )

            # Start both processes and prime ffmpeg before exposing the player to
            # the audio streamer, which keeps the buffered and live data contiguous.
            self._player_streams[airplay_player.player_id] = stream
            try:
                await self._start_client_processes(
                    airplay_player,
                    start_unix_ms,
                    self.use_shared_ptp,
                    stream,
                    reservation,
                )
                if buffered_pcm:
                    await self._write_chunk_to_player(airplay_player, buffered_pcm)
            except asyncio.CancelledError:
                await self.stop_client(
                    airplay_player,
                    reason="late joiner start/prime cancelled",
                    expected_stream=stream,
                )
                raise
            except Exception as err:
                self.prov.logger.warning(
                    "Late joiner %s: failed to start/prime pipeline: %s",
                    airplay_player.player_id,
                    err,
                )
                await self.stop_client(
                    airplay_player,
                    reason="late joiner start/prime failed",
                    expected_stream=stream,
                )
                return None

            # The next audio-streamer chunk continues where the priming buffer ended.
            if airplay_player not in self.sync_clients:
                self.sync_clients.append(airplay_player)
            return now

    async def _resolve_shared_ptp(self) -> bool:
        """
        Decide, once per session, whether members attach to the shared PTP daemon.

        The decision is session-wide and gated on the daemon actually being ready
        (bound to 319/320 with its control channel open), not merely spawned, so a
        group start cannot race the daemon and end up mixing PTP and NTP members.

        :return: True if every native AirPlay 2 member should use the shared PTP
            clock; False to degrade the whole session consistently (no member
            attaches to the daemon).
        """
        if await self.prov.wait_ptp_daemon_ready():
            return True
        # Daemon not ready: keep the group coherent by attaching no one to the
        # shared clock. A lone native AP2 player can still self-bind its own PTP
        # with no partner to drift against; only a real multi-room group loses
        # tight sync, so warn just for that case.
        ap2_members = sum(1 for p in self.sync_clients if p.protocol == StreamingProtocol.AIRPLAY2)
        if ap2_members > 1:
            self.prov.logger.warning(
                "Shared PTP clock daemon not ready - native AirPlay 2 multi-room sync "
                "for this group of %d players is degraded and members may drift. The "
                "server likely cannot bind the privileged PTP ports (UDP 319/320); "
                "running without root or CAP_NET_BIND_SERVICE is the common cause.",
                ap2_members,
            )
        return False

    async def _cleanup_after_removal(
        self,
        airplay_player: AirPlayPlayer,
        reason: str = "client removed",
        expected_stream: AirPlayStream | None = None,
        constrain_stream: bool = False,
    ) -> None:
        """
        Clean up processes and state after a client has been removed from sync_clients.

        :param airplay_player: The player whose processes should be stopped.
        :param reason: Short human-readable reason, forwarded to stop_client for logging.
        :param expected_stream: Only stop this exact session stream generation.
        :param constrain_stream: Require the stream mapping to match ``expected_stream``.
        """
        try:
            await self.stop_client(
                airplay_player,
                reason=reason,
                expected_stream=expected_stream,
                constrain_stream=constrain_stream,
            )
        finally:
            # Re-check sync_clients under the lock to avoid racing with add_client.
            async with self._lock:
                should_stop = not self.sync_clients and not self._starting_clients
            if should_stop:
                await self._cancel_audio_source()

    async def _stop_client_processes(
        self,
        ffmpeg: FFMpeg | None,
        stream: AirPlayStream | None,
    ) -> None:
        """Stop captured client processes without losing their session handles."""
        if ffmpeg:
            # Kill instead of closing gracefully because ffmpeg may take a long time to exit.
            await ffmpeg.kill()
        if stream:
            await stream.stop(force=True)

    async def _stop_client_locked(
        self,
        airplay_player: AirPlayPlayer,
        expected_stream: AirPlayStream | None,
        constrain_stream: bool,
    ) -> None:
        """Serialize and complete teardown for one session client."""
        player_id = airplay_player.player_id
        stop_lock = self._client_stop_locks.setdefault(player_id, asyncio.Lock())
        async with stop_lock:
            ffmpeg = self._player_ffmpeg.get(player_id)
            stream = self._player_streams.get(player_id)
            if constrain_stream and stream is not expected_stream:
                return
            if (
                stream is None
                and not constrain_stream
                and airplay_player.stream
                and airplay_player.stream.session == self
            ):
                stream = airplay_player.stream
            await self._stop_client_processes(ffmpeg, stream)
            if ffmpeg is not None and self._player_ffmpeg.get(player_id) is ffmpeg:
                self._player_ffmpeg.pop(player_id)
            if stream is not None and self._player_streams.get(player_id) is stream:
                self._player_streams.pop(player_id)
            if airplay_player.stream is stream:
                airplay_player.stream = None

    async def _audio_streamer(self, audio_source: AsyncGenerator[bytes]) -> None:
        """Stream audio to all players."""
        stream_error: BaseException | None = None
        try:
            async for chunk in audio_source:
                if not self.sync_clients:
                    break

                has_running_clients = await self._write_chunk_to_all_players(chunk)
                if not has_running_clients:
                    self.prov.logger.debug("No running clients remaining, stopping audio streamer")
                    break
        except asyncio.CancelledError:
            self.prov.logger.debug("Audio streamer cancelled after %.1fs", self.seconds_streamed)
            raise
        except Exception as err:
            stream_error = err
            self.prov.logger.error(
                "Audio source error after %.1fs of streaming: %s",
                self.seconds_streamed,
                err,
                exc_info=err,
            )
        finally:
            if stream_error:
                self.prov.logger.warning(
                    "Stream ended prematurely due to error - notifying players"
                )
        async with self._lock:
            await asyncio.gather(
                *[
                    self._write_eof_to_player(player)
                    for player in self.sync_clients
                    if (stream := self._player_streams.get(player.player_id)) and stream.running
                ],
                return_exceptions=True,
            )

    async def _write_chunk_to_all_players(self, chunk: bytes) -> bool:
        """
        Write a chunk to all connected players.

        :return: True if there are still running clients, False otherwise.
        """
        async with self._lock:
            sync_clients = [
                player
                for player in self.sync_clients
                if (stream := self._player_streams.get(player.player_id)) and stream.running
            ]
            if not sync_clients:
                return False

            # Update seconds_streamed and ring buffer under the lock so
            # add_client always reads consistent values.
            self.seconds_streamed += len(chunk) / self.pcm_format.pcm_sample_size
            self._pcm_buffer.extend(chunk)
            overflow = len(self._pcm_buffer) - self._pcm_buffer_max
            if overflow > 0:
                del self._pcm_buffer[:overflow]

            # Write chunk to all players
            write_tasks = [self._write_chunk_to_player(player, chunk) for player in sync_clients]
            results = await asyncio.gather(*write_tasks, return_exceptions=True)

            # Check for write errors or timeouts
            players_to_remove: list[tuple[AirPlayPlayer, str]] = []
            for i, result in enumerate(results):
                if i >= len(sync_clients):
                    continue
                player = sync_clients[i]

                if isinstance(result, TimeoutError):
                    self.prov.logger.warning(
                        "Removing player %s from session: stopped reading data (write timeout)",
                        player.player_id,
                    )
                    players_to_remove.append((player, "audio write timeout"))
                elif isinstance(result, Exception):
                    self.prov.logger.warning(
                        "Removing player %s from session due to write error: %s",
                        player.player_id,
                        result,
                    )
                    players_to_remove.append((player, f"audio write error: {result}"))

            # Remove failed players from sync_clients immediately under the lock
            # so they are excluded from future write cycles. Only defer process
            # cleanup (_cleanup_after_removal) — this prevents fire-and-forget
            # remove_client calls from racing with a subsequent add_client when
            # a player is being moved between groups.
            for player, removal_reason in players_to_remove:
                if player in self.sync_clients:
                    self.sync_clients.remove(player)
                owned_stream = self._player_streams.get(player.player_id)
                self.mass.create_task(
                    self._cleanup_after_removal(
                        player,
                        reason=removal_reason,
                        expected_stream=owned_stream,
                    )
                )

            remaining_clients = len(sync_clients) - len(players_to_remove)
            return remaining_clients > 0

    async def _write_chunk_to_player(self, airplay_player: AirPlayPlayer, chunk: bytes) -> None:
        """Write audio chunk to a player's ffmpeg process."""
        player_id = airplay_player.player_id
        if ffmpeg := self._player_ffmpeg.get(player_id):
            if ffmpeg.closed:
                return
            await asyncio.wait_for(ffmpeg.write(chunk), timeout=35.0)

    async def _write_eof_to_player(self, airplay_player: AirPlayPlayer) -> None:
        """Write EOF to a specific player."""
        player_id = airplay_player.player_id
        stop_lock = self._client_stop_locks.setdefault(player_id, asyncio.Lock())
        async with stop_lock:
            if not (ffmpeg := self._player_ffmpeg.get(player_id)):
                return
            try:
                await ffmpeg.write_eof()
                await ffmpeg.wait_with_timeout(30)
                if stream := self._player_streams.get(player_id):
                    await stream.write_audio_eof()
            except BaseException:
                kill_task = asyncio.create_task(ffmpeg.kill())
                try:
                    await asyncio.shield(kill_task)
                except asyncio.CancelledError:
                    await kill_task
                if stream := self._player_streams.get(player_id):
                    await stream.stop(force=True)
                    if self._player_streams.get(player_id) is stream:
                        self._player_streams.pop(player_id)
                    if airplay_player.stream is stream:
                        airplay_player.stream = None
                raise
            finally:
                if self._player_ffmpeg.get(player_id) is ffmpeg:
                    self._player_ffmpeg.pop(player_id)

    async def _start_client(
        self,
        airplay_player: AirPlayPlayer,
        start_unix_ms: int,
        use_shared_ptp: bool,
        expected_generation: int | None = None,
    ) -> None:
        """
        Start CLI process and ffmpeg for a single client.

        :param airplay_player: The player to start streaming to.
        :param start_unix_ms: The audible-start instant in unix epoch ms.
        :param use_shared_ptp: The session-wide shared-PTP decision applied to
            this member so the whole group shares one timing source.
        :param expected_generation: Player generation captured when startup was requested.
        """
        if expected_generation is None:
            expected_generation = airplay_player.reserve_stream_generation()
        stream = self._prepare_client_stream(airplay_player)
        replacement = airplay_player.stream_replacement(
            stream,
            is_current=lambda: self._client_start_is_current(airplay_player, None),
            expected_generation=expected_generation,
            before_replace=lambda: self._prepare_client_replacement(airplay_player),
        )
        async with replacement:
            self._player_streams[airplay_player.player_id] = stream
            await self._start_client_processes(
                airplay_player,
                start_unix_ms,
                use_shared_ptp,
                stream,
            )

    def _prepare_client_stream(self, airplay_player: AirPlayPlayer) -> AirPlayStream:
        """Create an unstarted stream for a session client."""
        airplay_player.sync_volume_level()
        stream_pcm_format = airplay_player.get_stream_pcm_format(self.pcm_format)
        stream = AirPlayStream(airplay_player, pcm_format=stream_pcm_format)
        stream.session = self
        return stream

    async def _prepare_client_replacement(self, airplay_player: AirPlayPlayer) -> None:
        """Release bridge and session resources before a validated replacement."""
        await self.prov.bridge_manager.stop_streaming(airplay_player.player_id)
        if (
            airplay_player.player_id in self._player_streams
            or airplay_player.player_id in self._player_ffmpeg
        ):
            await self.stop_client(airplay_player, reason="client stream restart")

    async def _start_client_processes(
        self,
        airplay_player: AirPlayPlayer,
        start_unix_ms: int,
        use_shared_ptp: bool,
        stream: AirPlayStream,
        reservation: object | None = None,
    ) -> None:
        """Start cliairplay and ffmpeg for a reserved player stream."""
        ffmpeg: FFMpeg | None = None
        stop_lock = self._client_stop_locks.setdefault(airplay_player.player_id, asyncio.Lock())
        try:
            async with stop_lock:
                if not self._client_start_is_current(airplay_player, reservation):
                    raise PlayerCommandFailed("AirPlay stream session was superseded")
                sync_adjust = airplay_player.config.get_value(CONF_SYNC_ADJUST, 0)
                assert isinstance(sync_adjust, int)
                if sync_adjust != 0:
                    start_unix_ms += sync_adjust
                await stream.start(start_unix_ms, use_shared_ptp)
                # Start ffmpeg to feed audio to CLI stdin
                if previous_ffmpeg := self._player_ffmpeg.pop(airplay_player.player_id, None):
                    ffmpeg = previous_ffmpeg
                    await previous_ffmpeg.close()
                handoff_format = stream.pcm_format
                output_plan = self.mass.streams.audio.get_player_output_plan(
                    airplay_player.player_id,
                    input_format=self.pcm_format,
                    output_format=get_final_output_format(handoff_format, airplay_player),
                    handoff_format=handoff_format,
                    queue_id=self.media.source_id,
                    session_id=get_media_session_id(self.media),
                )
                cli_proc = stream._cli_proc
                assert cli_proc
                assert cli_proc.proc
                assert cli_proc.proc.stdin
                stdin_transport = cli_proc.proc.stdin.transport
                audio_output: str | int = stdin_transport.get_extra_info("pipe").fileno()
                ffmpeg = FFMpeg(
                    audio_input="-",
                    input_format=self.pcm_format,
                    output_format=handoff_format,
                    filter_params=output_plan.filter_params,
                    audio_output=audio_output,
                )
                self._player_ffmpeg[airplay_player.player_id] = ffmpeg
                ffmpeg_start_task = asyncio.create_task(ffmpeg.start())
                try:
                    await asyncio.shield(ffmpeg_start_task)
                except BaseException:
                    with suppress(asyncio.CancelledError, Exception):
                        await ffmpeg_start_task
                    raise
                if (
                    self._player_streams.get(airplay_player.player_id) is not stream
                    or airplay_player.stream is not stream
                ):
                    raise PlayerCommandFailed("AirPlay client start was superseded")
        except BaseException:
            if ffmpeg:
                if self._player_ffmpeg.get(airplay_player.player_id) is ffmpeg:
                    self._player_ffmpeg.pop(airplay_player.player_id)
                await ffmpeg.kill()
            await self.stop_client(
                airplay_player,
                reason="client start interrupted",
                expected_stream=stream,
            )
            raise

    async def _stop(self) -> None:
        """Stop the audio source and every process owned by this session."""
        await self._cancel_audio_source()
        async with self._lock:
            self.sync_clients.clear()
            self._starting_clients.clear()
            players = list(self._players.values())
        await asyncio.gather(
            *(self.stop_client(player, reason="session stop") for player in players)
        )
        self._pcm_buffer.clear()

    async def _cancel_audio_source(self) -> None:
        """Cancel the session audio source task if it is still running."""
        audio_source_task = self._audio_source_task
        if (
            audio_source_task
            and audio_source_task is not asyncio.current_task()
            and not audio_source_task.done()
        ):
            cleanup_task = asyncio.create_task(self._cancel_audio_source_task(audio_source_task))
            try:
                await asyncio.shield(cleanup_task)
            except asyncio.CancelledError:
                await cleanup_task
                raise

    async def _cancel_audio_source_task(
        self,
        audio_source_task: asyncio.Task[None],
    ) -> None:
        """Cancel and consume the session audio task."""
        audio_source_task.cancel()
        with suppress(asyncio.CancelledError):
            await audio_source_task

    async def _cleanup_failed_start(
        self,
        audio_source: AsyncGenerator[bytes],
        start_tasks: list[asyncio.Task[None]],
    ) -> None:
        """Stop partially started clients and release an unused audio source."""
        for task in start_tasks:
            if not task.done():
                task.cancel()
        if start_tasks:
            await asyncio.gather(*start_tasks, return_exceptions=True)
        if self._audio_source_task is None:
            with suppress(Exception):
                await audio_source.aclose()
        await self.stop()
