"""
AirPlay audio streaming using the cliairplay binary.

Handles both RAOP (AirPlay 1) and AirPlay 2 protocols through a single
unified binary. Audio is fed via stdin, commands via a named pipe,
status is reported on stderr in normalized [STATUS] format.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from contextlib import suppress
from dataclasses import dataclass
from http import HTTPStatus
from typing import TYPE_CHECKING, Any, Final, cast
from uuid import uuid4

from music_assistant_models.enums import PlaybackState
from music_assistant_models.errors import PlayerCommandFailed

from music_assistant.constants import VERBOSE_LOG_LEVEL
from music_assistant.helpers.images import get_image_thumb_path
from music_assistant.helpers.named_pipe import AsyncNamedPipeWriter
from music_assistant.helpers.process import AsyncProcess
from music_assistant.providers.airplay.constants import (
    AIRPLAY_ARTWORK_RENDER_TIMEOUT,
    AIRPLAY_ARTWORK_SIZE,
    AIRPLAY_CONTENT_CUT_TOLERANCE_MS,
    AIRPLAY_JOIN_START_ACK_TIMEOUT_MS,
    AIRPLAY_PCM_FORMAT,
    AIRPLAY_START_ACK_TIMEOUT_MS,
    CLI_PROBLEM_MARKERS,
    CONF_AIRPLAY_CREDENTIALS,
    CONF_BUFFER_DEPTH,
    CONF_ENCRYPTION,
    CONF_PASSWORD,
    CONF_RAOP_CREDENTIALS,
    AirPlayRemoteCommand,
    ClockReadiness,
    StreamingProtocol,
)
from music_assistant.providers.airplay.helpers import (
    default_buffer_depth,
    generate_active_remote_id,
    get_cli_binary,
    get_decoded_property,
    serialize_txt_records,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from music_assistant_models.media_items import AudioFormat
    from music_assistant_models.player import PlayerMedia

    from music_assistant.providers.airplay.player import AirPlayPlayer
    from music_assistant.providers.airplay.provider import AirPlayProvider
    from music_assistant.providers.airplay.stream_session import AirPlayStreamSession

# Slugs of the machine-readable failure line the binary emits:
#   [STATUS] error code=<slug> http=<int> detail="<short text>"
# The auth/connect slugs are terminal - the binary gives up and exits. The
# command slugs are not: the connection survives a rejected transport command
# and only the pending ack is answered with a failure.
CLI_ERROR_AUTH_REQUIRED: Final[str] = "auth_required"
CLI_ERROR_AUTH_FAILED: Final[str] = "auth_failed"
CLI_ERROR_START_FAILED: Final[str] = "start_failed"
CLI_ERROR_FLUSH_FAILED: Final[str] = "flush_failed"

_CLI_ERROR_CODE_RE = re.compile(r"\bcode=(\S+)")
_CLI_ERROR_HTTP_RE = re.compile(r"\bhttp=(\d+)")
_CLI_ERROR_DETAIL_RE = re.compile(r'\bdetail="([^"]*)"')

# Seconds to wait for the binary's command pipe reader. It attaches right after
# the connection is reported, so this only bridges the reader thread starting up.
_COMMAND_PIPE_READER_TIMEOUT: Final[float] = 2.0


@dataclass
class CliError:
    """Structured failure as reported by the cliairplay binary."""

    code: str
    http_status: int = 0
    detail: str = ""


class AirPlayStream:
    """AirPlay audio streamer using the unified cliairplay binary."""

    _cli_proc: AsyncProcess | None
    session: AirPlayStreamSession | None = None

    def __init__(self, player: AirPlayPlayer, pcm_format: AudioFormat | None = None) -> None:
        """
        Initialize AirPlay stream.

        :param player: The player to stream to.
        :param pcm_format: The PCM format fed to the binary's stdin
            (defaults to 44.1kHz/16-bit).
        """
        self.prov = player.provider
        self.mass = player.provider.mass
        self.player = player
        self.pcm_format = pcm_format or AIRPLAY_PCM_FORMAT
        mac_address = self.player.device_info.mac_address or self.player.player_id
        self.active_remote_id: str = generate_active_remote_id(mac_address)
        self._stream_id = uuid4().hex
        self.prevent_playback: bool = False
        self._cli_proc: AsyncProcess | None = None
        self.commands_pipe = AsyncNamedPipeWriter(
            f"/tmp/{self.player.protocol.value}-{self.player.player_id}-"  # noqa: S108
            f"{self.active_remote_id}-{self._stream_id}-cmd",
        )
        self._stopped = False
        self._stopping = False
        self._cleanup_complete = False
        self._stop_lock = asyncio.Lock()
        # Both wake a track-change metadata push waiting out its artwork render
        # budget inside the metadata lock: the first is set permanently the
        # moment stop() begins, the second while start() is waiting on the lock
        # (a pending START is time-critical — the anchor lead is a few hundred
        # ms). Either makes the bounded wait yield so the teardown or START
        # proceeds and the artwork follows asynchronously.
        self._teardown_started, self._start_waiting = asyncio.Event(), asyncio.Event()
        self._connected = asyncio.Event()
        # Set when the stderr reader ends, i.e. the binary is gone. A connect
        # wait watches it so a process that died (for example on a rejected
        # password) fails right away instead of running out its timeout.
        self._process_ended = asyncio.Event()
        # Whether the binary reported the end of the stream itself ([STATUS] eof
        # or its idle timeout) instead of dying. Both leave the process gone and
        # `running` reading False, so this is what tells the two apart.
        self.ended_cleanly: bool = False
        # Structured fatal failure the binary reported before exiting; stays
        # None when it exited without reporting one.
        self._connect_error: CliError | None = None
        # Set when the binary answers an in-place FLUSH, either acknowledging it
        # ([STATUS] flushed) or reporting that it rejected the command, which
        # fills _flush_error. Each answer clears the other.
        self._flushed = asyncio.Event()
        self._flush_error: CliError | None = None
        # Set when the binary acks a START ([STATUS] started); carries the
        # (requested, actual) unix-ms pair. The binary corrects an infeasible
        # instant FORWARD and always reports the truth, so a mismatch here is
        # the self-verification signal for the start contract. A reported start
        # failure also sets it, filling _start_error instead of the ack; each
        # answer clears the other.
        self._started = asyncio.Event()
        self._start_ack: tuple[int, int] | None = None
        self._start_error: CliError | None = None
        # Whether the last commanded START was a late-join start: routes the
        # post-commit correction log level (a corrected join is the routine
        # landing path, a corrected origin start is a loud signal).
        self._start_was_join = False
        # Receiver storm guard state: last-honored monotonic time per remote
        # transport command, plus a counter of suppressed repeats.
        self._remote_command_last: dict[str, float] = {}
        self._remote_commands_suppressed: int = 0
        # Set when the binary reports the first audio bytes of the current
        # start cycle arriving on its stdin ([STATUS] audio). Together with
        # `connected` this makes readiness fully event-driven, so START can
        # use a short re-anchor lead instead of a guessed setup time.
        self._audio_present = asyncio.Event()
        # Set once the binary settled the receiver's clock readiness ([STATUS]
        # clock_ready): either it projected when the clock becomes usable, or it
        # reported that there is nothing to wait for. The projected instant
        # (unix ms) stays 0 in the latter case, and the readiness below says
        # which of the reasons it was.
        self._clock_ready = asyncio.Event()
        self._clock_ready_at_unix_ms: int = 0
        self._clock_readiness: ClockReadiness = ClockReadiness.UNREPORTED
        self._metadata_text_checksum = ""
        # Artwork identity (the source image url) whose rendered bytes were
        # last delivered to the binary. Settles on the first successful
        # delivery, independent of the metadata generation: media updates
        # around a track transition keep bumping the generation, and a settle
        # tied to it would re-render and re-send the same art on every update
        # until the churn stops.
        self._metadata_artwork_checksum = ""
        self._pending_metadata_checksum = ""
        self._metadata_generation = 0
        self._metadata_lock = asyncio.Lock()
        self._artwork_render_generations: set[int] = set()
        self._last_progress_sent: int | None = None
        # Media position (seconds) mapped to the first sample of the current
        # START anchor. The binary reports "playing elapsed_ms" relative to that
        # anchor (resetting to ~0 at each START), so elapsed is this base plus
        # the reported delta.
        self._start_position: float = 0.0
        # Content cut (ms) a post-commit anchor correction asked for and that is
        # already folded into the base above, until the binary reports what it
        # actually managed to take. 0 when no cut is outstanding.
        self._pending_content_cut_ms: int = 0
        self._stdout_reader_task: asyncio.Task[None] | None = None
        # Device latency info reported by the binary after connect (0 = unreported)
        self.latency_lead_ms: int = 0
        self.device_min_frames: int = 0
        self.device_max_frames: int = 0
        # Minimum lead (ms) a warm commanded START needs for exact placement.
        # Nonzero on the Apple splice timeline, where the receiver's queued
        # audio plays out before the new content can begin; a warm group
        # anchor must sit beyond the largest member value. 0 = no constraint.
        self.warm_lead_ms: int = 0
        # Audible instant (unix ms) of the delivery head frozen by the latest
        # warm flush, from the flushed ack (0 = none/no constraint). The warm
        # START anchor must land beyond it for the splice skip to engage.
        self.flushed_head_unix_ms: int = 0
        # Route the binary resolved for this stream (empty until reported),
        # e.g. "AirPlay 2 (native, PTP)" or "RAOP"
        self.active_route: str = ""
        # Cumulative playout shift (seconds) this process reported after PCM
        # starvation re-anchors (AP2 only). The stream session adds the
        # reference member's shift so a late joiner anchors to the group's real
        # timeline. Reset per process (and on every re-anchoring START/resume);
        # a new cliairplay re-anchors from scratch.
        self.cumulative_shift_seconds: float = 0.0
        # Set once a stalled receiver clock has been reported, so the warning
        # stays a single support signal instead of repeating with every
        # clock_ready update of this stream session.
        self._clock_stall_warned: bool = False

    @property
    def running(self) -> bool:
        """Return boolean if this stream is running."""
        return (
            not self._stopped
            and not self._stopping
            and self._cli_proc is not None
            and not self._cli_proc.closed
        )

    @property
    def connected(self) -> bool:
        """Return boolean if the device connection has been established."""
        return self._connected.is_set()

    async def connect(
        self,
        use_shared_ptp: bool | None = None,
    ) -> None:
        """
        Spawn cliairplay and connect to the receiver.

        Establishes the process, command pipe and device connection that persist
        for the whole stream lifetime. Playback itself is anchored separately with
        :meth:`start` once audio is being fed on the persistent stdin.

        :param use_shared_ptp: Session-wide decision on whether native AirPlay 2
            members attach to the shared PTP clock daemon. The stream session
            passes the same value to every member so a group never mixes PTP and
            NTP timing. None lets the stream decide from the daemon's readiness.
        """
        self._check_password_preflight()
        # A fresh cliairplay process re-anchors from scratch, so drop any shift
        # carried on this stream object.
        self.reset_reanchor_shift()
        args = await self._build_cli_args(use_shared_ptp)
        self.player.logger.debug("Starting cliairplay for player %s", self.player.player_id)
        self._cli_proc = AsyncProcess(args, stdin=True, stdout=True, stderr=True, name="cliairplay")
        try:
            await self.commands_pipe.create()
            await self._cli_proc.start()
            self._cli_proc.attach_stderr_reader(self.mass.create_task(self._stderr_reader()))
            self._stdout_reader_task = self.mass.create_task(self._stdout_reader())
        except BaseException:
            try:
                await self._cleanup_failed_start()
            except Exception as err:
                self.player.logger.warning("Failed to clean up cliairplay startup: %s", err)
            raise

    async def wait_for_connection(self) -> None:
        """
        Wait for device connection to be established.

        Also waits for the binary's command pipe to open, so the first commands
        are not dropped.

        :raises PlayerCommandFailed: If the binary reported that the device needs
            a password, rejected the configured one, or never opened the command
            pipe that carries playback commands.
        :raises TimeoutError: If the connection was not established for any other
            reason (including an unreported one).
        """
        if not self._cli_proc:
            raise RuntimeError("cliairplay process is not running")
        await self._await_connected()
        # The binary attaches to its command pipe only once it is connected, so
        # the first command waits for that reader instead of being dropped. A
        # pipe that never opens leaves the stream unable to be anchored at all,
        # so it fails the connection rather than letting the caller command a
        # START nothing can receive. A stream torn down while connecting takes
        # its pipe along, which is not a fault worth reporting.
        if not await self.commands_pipe.wait_for_reader(_COMMAND_PIPE_READER_TIMEOUT):
            if self.running:
                raise PlayerCommandFailed(
                    f"cliairplay did not open its command pipe for "
                    f"{self.player.display_name}; playback commands cannot be delivered"
                )
        # Nothing has reached this binary yet, so clear the delivery state to
        # make sure the pushes below are really sent.
        async with self._metadata_lock:
            self._metadata_text_checksum = ""
            self._metadata_artwork_checksum = ""
            self._pending_metadata_checksum = ""
            self._metadata_generation += 1
        # Push track metadata before START. Some receivers (notably Sonos) hold
        # back audio rendering until they receive track metadata; deferring it
        # can keep them silent past the commanded start.
        await self._send_current_metadata(send_artwork=False)
        # Send the mute-aware volume right away — audio can start within a
        # second now that metadata goes out immediately — and repeat it after
        # 2 seconds because some players ignore the first volume command
        # (https://github.com/music-assistant/support/issues/3330). The repeat reads
        # the level when it fires, so it never replays a value that changed since.
        await self._send_current_volume()
        self.mass.call_later(2, self._send_current_volume)
        # settle artwork and the position on top of the identity push above
        self.player._on_player_media_updated()

    async def stop(self, force: bool = False) -> None:
        """
        Stop playback and cleanup.

        :param force: If True, immediately kill the process without graceful shutdown.
        """
        async with self._stop_lock:
            if self._cleanup_complete:
                return
            self._stopping = True
            self._teardown_started.set()
            async with self._metadata_lock:
                self._metadata_generation += 1
                try:
                    await self._write_cli_command("ACTION=STOP")
                finally:
                    self._stopped = True
                    try:
                        await self.commands_pipe.remove()
                    finally:
                        # stop the stdout reader first so process close can drain the pipe
                        stdout_reader_task = self._stdout_reader_task
                        if stdout_reader_task and not stdout_reader_task.done():
                            stdout_reader_task.cancel()
                            with suppress(asyncio.CancelledError):
                                await stdout_reader_task
                        try:
                            if force:
                                if self._cli_proc and not self._cli_proc.closed:
                                    await self._cli_proc.kill()
                            else:
                                if self._cli_proc:
                                    await self._cli_proc.write_eof()
                                if self._cli_proc and not self._cli_proc.closed:
                                    await self._cli_proc.close()
                        finally:
                            self.player.set_state_from_stream(
                                state=PlaybackState.IDLE,
                                elapsed_time=0,
                                stream=self,
                            )
                            self._cleanup_complete = True

    async def write_audio(self, data: bytes) -> None:
        """
        Write raw audio data to the CLI process stdin.

        :param data: Raw audio bytes to send to the streaming process.
        """
        if self._stopped or self._stopping or not self._cli_proc or self._cli_proc.closed:
            return
        await self._cli_proc.write(data)

    async def write_audio_eof(self) -> None:
        """Signal end-of-stream to the CLI process stdin."""
        if self._stopped or self._stopping or not self._cli_proc or self._cli_proc.closed:
            return
        await self._cli_proc.write_eof()

    async def send_cli_command(self, command: str) -> bool:
        """
        Send an interactive command to the running CLI binary.

        :param command: Command to send.
        :return: True when the complete command is delivered, False when the
            command is ignored, the CLI is unavailable, or the write is dropped.
        """
        if self._stopped or self._stopping:
            return False
        return await self._write_cli_command(command)

    async def flush(self, timeout: float = 2.0) -> bool:
        """
        Flush the live stream in place and wait for the binary's acknowledgement.

        Sends ``ACTION=FLUSH`` — the binary stops sending audio, flushes the
        receiver, discards its input ring and drains stdin, then reports
        ``[STATUS] flushed`` while keeping the connection and stdin reader alive.
        The caller must have stopped feeding old audio before calling this so the
        drain removes exactly the pre-flush bytes.

        :param timeout: Seconds to wait for the flushed acknowledgement.
        :return: True once the flush is acknowledged; False on a delivery
            failure, a flush the binary reports it rejected, or a timeout, so
            the caller can fall back to a cold restart.
        """
        if not self.running or not self.connected:
            return False
        self._arm_flush_answer()
        # The flush drain re-arms the binary's one-shot audio signal; the next
        # [STATUS] audio belongs to the new track.
        self._audio_present.clear()
        if not await self._write_cli_command("ACTION=FLUSH"):
            return False
        try:
            await asyncio.wait_for(self._flushed.wait(), timeout)
        except TimeoutError:
            return False
        if (error := self._flush_error) is not None:
            self.player.logger.warning(
                "cliairplay rejected the flush for %s (%s); falling back to a cold restart",
                self.player.display_name,
                error.detail or error.code,
            )
            return False
        return True

    async def wait_audio_present(self, timeout: float = 5.0) -> bool:
        """
        Wait until the binary reports the current start cycle's audio arriving.

        The binary emits a one-shot ``[STATUS] audio`` when the first bytes of
        a start cycle land on its stdin (re-armed by each flush). Waiting for
        it before commanding START removes source/transcoder spin-up from the
        start lead.

        :param timeout: Seconds to wait for the signal.
        :return: True once audio is flowing; False on timeout.
        """
        try:
            await asyncio.wait_for(self._audio_present.wait(), timeout)
        except TimeoutError:
            return False
        return True

    async def wait_clock_ready(self, timeout: float = 2.5) -> tuple[ClockReadiness, int]:
        """
        Wait for the binary to project when the receiver's clock becomes usable.

        A receiver starts probing its clock as soon as it is connected, so the
        projection is available well before any anchor is announced and resolves
        from the receiver's first probe rather than from its full servo lock.

        :param timeout: Seconds to wait for the projection.
        :return: How the readiness resolved, and the projected instant (unix ms)
            at which the receiver's clock is usable — possibly already in the
            past when it is locked. Only :attr:`ClockReadiness.PROJECTED` carries
            an instant; every other outcome pairs with 0 and leaves the caller
            anchoring on its lead alone, but for reasons that differ enough to
            act on: a stalled receiver will render silence, NTP timing has no
            clock to wait for, and an unreported one is worth retrying.
        """
        try:
            await asyncio.wait_for(self._clock_ready.wait(), timeout)
        except TimeoutError:
            return (ClockReadiness.UNREPORTED, 0)
        return (self._clock_readiness, self._clock_ready_at_unix_ms)

    async def start(
        self, start_unix_ms: int = 0, position_ms: int = 0, *, join: bool = False
    ) -> int:
        """
        Anchor playback so the first pending stdin sample is audible at an instant.

        The first call begins playback (connection already established); a call
        after :meth:`flush` re-bases the frozen anchor and resumes from the ring.

        :param start_unix_ms: Unix-epoch milliseconds at which the first pending
            stdin sample must be audible. 0 means as soon as possible (the binary
            clamps to its minimum lead).
        :param position_ms: Media position mapped to that first sample, used as
            the base for elapsed reporting.
        :param join: This start must land on an already-live group timeline (a
            late joiner): the binary then enforces receiver clock readiness and
            holds its ack until that resolves, so the returned instant is the one
            the caller must map the joiner's content onto. Group/solo origin
            starts leave it False.
        :return: The true scheduled audible instant (unix ms) from the binary's
            started ack — the commanded instant when it was feasible, the
            corrected-forward one otherwise.
        :raises PlayerCommandFailed: If the START command cannot be delivered,
            the binary reports that it scheduled no instant, or it never
            acknowledged the start.
        """
        if not self.running or not self.connected:
            raise RuntimeError("Cannot start playback without a connected cliairplay process")
        # A START re-anchors playout from scratch — the binary zeroes its own
        # re-anchor total on start/resume — so drop any shift accumulated against
        # the previous anchor (this also covers the warm-seek FLUSH->refill->START
        # path) to keep the server and binary baselines aligned.
        self.reset_reanchor_shift()
        self._start_position = position_ms / 1000
        # This base is absolute, so a cut still outstanding against the previous
        # anchor is no longer part of it and must not be reconciled into it.
        self._pending_content_cut_ms = 0
        # Stamp the player's elapsed onto the new anchor's base right away: until
        # the binary's first status arrives, interpolation would otherwise keep
        # extending the previous anchor's clock, briefly mapping a bogus position.
        self.player.set_state_from_stream(elapsed_time=self._start_position, stream=self)
        self._arm_start_answer()
        self._start_was_join = join
        start_cmd = f"START_UNIX_MS={start_unix_ms}\nACTION=START"
        if join:
            start_cmd = f"START_UNIX_MS={start_unix_ms}\nSTART_JOIN=1\nACTION=START"
        self._start_waiting.set()
        try:
            await self._metadata_lock.acquire()
        finally:
            self._start_waiting.clear()
        try:
            if not await self._write_cli_command(start_cmd):
                # Surfacing the dropped delivery lets the session fall back to a
                # cold restart instead of waiting on an anchor that never happens.
                raise PlayerCommandFailed(
                    f"Could not deliver START to AirPlay player {self.player.player_id}"
                )
            # Supersede an in-flight pre-transition artwork render; the task
            # below then sends only what actually changed (a track change's
            # text/artwork). Unchanged title/artwork stay deduped — re-pushing
            # identical metadata around every anchor makes an Apple TV
            # re-render its Now Playing popup on each seek, and a re-anchor
            # cannot lose artwork anyway now that the binary carries the
            # artwork bytes in every now-playing push. The progress correction
            # is deliberately NOT sent here: every now-playing push visibly
            # refreshes the Apple TV screen, so the single settled correction
            # from the post-anchor media-updated nudge (+1s) does the job with
            # one refresh instead of two.
            self._metadata_generation += 1
        finally:
            self._metadata_lock.release()
        self.mass.create_task(
            self._send_current_metadata_without_progress,
            task_id=f"airplay_metadata_after_start_{self._stream_id}",
            abort_existing=True,
        )
        # The binary always acks with the TRUE scheduled instant (correcting an
        # infeasible one forward), so the caller can verify the contract and
        # re-align a group. A join's ack is held back until the receiver clock
        # verification resolves and therefore gets a much wider window than a
        # plain start, which acks within the command round-trip. A reported
        # failure answers the wait immediately.
        ack_timeout = (
            AIRPLAY_JOIN_START_ACK_TIMEOUT_MS if join else AIRPLAY_START_ACK_TIMEOUT_MS
        ) / 1000
        try:
            await asyncio.wait_for(self._started.wait(), ack_timeout)
        except TimeoutError as err:
            # Nothing confirmed the instant, so nothing may be mapped onto it:
            # an anchor the caller records but the receiver never played is a
            # timeline every later joiner then aligns itself against.
            raise PlayerCommandFailed(
                f"AirPlay player {self.player.display_name} did not acknowledge its "
                f"start within {ack_timeout:.1f}s (commanded instant {start_unix_ms})"
            ) from err
        if (error := self._start_error) is not None:
            # Nothing was anchored, so there is no instant to map content onto.
            # Failing here costs the caller the round-trip instead of the whole
            # ack timeout, and tells it apart from an unacknowledged start.
            raise PlayerCommandFailed(
                f"cliairplay could not start playback on {self.player.display_name}"
                + (f": {error.detail}" if error.detail else "")
            )
        # A malformed ack still answered the START, so the commanded instant is
        # what the binary applied (see the parse fallback in _handle_status_line).
        return self._start_ack[1] if self._start_ack else start_unix_ms

    def rebase_position(self, position_ms: int) -> None:
        """
        Re-map reported progress onto a start instant that moved after the command.

        A join's START is acked with the instant the receiver can actually seat,
        which may be later than the commanded one. The caller maps its content
        onto that instant and reports the position that lands there.

        :param position_ms: Media position of the first sample the binary
            renders at the acked instant.
        """
        self._start_position = position_ms / 1000
        self._pending_content_cut_ms = 0
        self.player.set_state_from_stream(elapsed_time=self._start_position, stream=self)

    def reset_reanchor_shift(self) -> None:
        """Clear the accumulated re-anchor shift."""
        self.cumulative_shift_seconds = 0.0

    async def send_metadata(  # noqa: PLR0915
        self,
        progress: int | None,
        metadata: PlayerMedia | None,
        send_artwork: bool = True,
    ) -> None:
        """
        Send metadata to player.

        :param progress: Current playback position in seconds.
        :param metadata: Media metadata to send.
        :param send_artwork: Whether artwork should be rendered and sent.
        """
        metadata_checksum: str | None = None
        text_checksum: str | None = None
        artwork_checksum = ""
        duration = 0
        title = ""
        artist = ""
        album = ""
        item_id = ""
        if metadata:
            duration = self._full_media_duration(metadata)
            title = metadata.title or ""
            artist = metadata.artist or ""
            album = metadata.album or ""
            item_id = metadata.queue_item_id or ""
            # The identity deliberately excludes duration and image url: a
            # value that shifts per seek or per media-update would re-send the
            # full metadata each time — which makes an Apple TV re-render its
            # Now Playing popup.
            text_checksum = f"{item_id}|{title}|{artist}|{album}"
            artwork_checksum = metadata.image_url or ""
            metadata_checksum = f"{text_checksum}|{metadata.image_url}"

        artwork_url: str | None = None
        artwork_render: asyncio.Task[str | None] | None = None
        metadata_generation = 0
        async with self._metadata_lock:
            if self._stopped or self._stopping:
                return
            if metadata_checksum is not None:
                if metadata_checksum != self._pending_metadata_checksum:
                    self._pending_metadata_checksum = metadata_checksum
                    self._metadata_generation += 1
                metadata_generation = self._metadata_generation
            needs_artwork = artwork_checksum != self._metadata_artwork_checksum
            if (
                metadata
                and metadata_checksum is not None
                and text_checksum is not None
                and (needs_artwork or text_checksum != self._metadata_text_checksum)
            ):
                if text_checksum != self._metadata_text_checksum:
                    artwork_file: str | None = None
                    if (
                        send_artwork
                        and metadata.image_url
                        and needs_artwork
                        and metadata_generation not in self._artwork_render_generations
                    ):
                        # Budgeted pre-render so metadata and artwork ride ONE
                        # SENDMETA push: back-to-back now-playing rewrites (a
                        # bare replace followed by the artwork) intermittently
                        # wedge the Apple TV now-playing screen. A render that
                        # misses the budget keeps running and delivers through
                        # the ARTWORK command instead.
                        self._artwork_render_generations.add(metadata_generation)
                        artwork_url = metadata.image_url
                        artwork_file, artwork_render = await self._render_artwork_bounded(
                            artwork_url, metadata_generation
                        )
                    # ITEMID gives the binary a stable per-track identity, so
                    # a later tag refinement for the same queue item (library
                    # enrichment can settle after playback starts) updates the
                    # receiver's now-playing item in place instead of
                    # presenting as a new track.
                    cmd = f"TITLE={title}\nARTIST={artist}\nALBUM={album}\n"
                    cmd += f"DURATION={duration}\nITEMID={item_id}\n"
                    if artwork_file:
                        cmd += f"ARTWORKFILE={artwork_file}\n"
                    cmd += "ACTION=SENDMETA\n"
                    if not await self.send_cli_command(cmd):
                        if artwork_render is not None:
                            # the identity push never went out, so drop the
                            # render and let the next update retry from scratch
                            artwork_render.cancel()
                            self._artwork_render_generations.discard(metadata_generation)
                        return
                    self._metadata_text_checksum = text_checksum
                    # the push resets a changed track to position zero (a
                    # same-item refinement carries the current position), so
                    # the correction below only follows when playback is
                    # actually elsewhere (mid-track start, tag refinement)
                    self._last_progress_sent = 0
                    if artwork_file:
                        # the bundle delivered the artwork: settle it and stand
                        # down the ARTWORK follow-up
                        self._metadata_artwork_checksum = artwork_checksum
                        self._artwork_render_generations.discard(metadata_generation)
                        needs_artwork = False
                        artwork_url = None
                        artwork_render = None
                if metadata_generation != self._metadata_generation:
                    return
                if (
                    send_artwork
                    and metadata.image_url
                    and needs_artwork
                    and metadata_generation not in self._artwork_render_generations
                ):
                    self._artwork_render_generations.add(metadata_generation)
                    artwork_url = metadata.image_url
                elif not metadata.image_url or not needs_artwork:
                    self._metadata_artwork_checksum = artwork_checksum
            if progress is not None and (
                self._last_progress_sent is None or abs(progress - self._last_progress_sent) >= 2
            ):
                # duration rides along so the seek-rebased remaining time
                # reaches the device without re-sending the metadata identity
                duration_cmd = f"DURATION={duration}\n" if metadata else ""
                if await self.send_cli_command(f"{duration_cmd}PROGRESS={progress}"):
                    self._last_progress_sent = progress

        if artwork_url is not None:
            await self._render_and_send_artwork(artwork_url, metadata_generation, artwork_render)

    def _full_media_duration(self, metadata: PlayerMedia) -> int:
        """
        Return the media's full track duration in seconds.

        The queue rewrites ``PlayerMedia.duration`` to the REMAINING time on a
        seek (the stream-restart convention for players whose position resets
        to zero). The spliced AirPlay timeline reports absolute positions, so
        the device needs the real total — and a total that changes on every
        seek also makes an Apple TV re-lay-out its Now Playing screen each
        time, which shows as a brief artwork/screen flash.

        :param metadata: Media whose duration is resolved.
        """
        if metadata.source_id and metadata.queue_item_id:
            queue_item = self.mass.player_queues.get_item(
                metadata.source_id, metadata.queue_item_id
            )
            if queue_item:
                streamdetails = queue_item.streamdetails
                full_duration = (
                    streamdetails.duration if streamdetails else None
                ) or queue_item.duration
                if full_duration:
                    return min(int(full_duration), 3600)
        return min(metadata.duration or 0, 3600)

    async def _render_artwork_bounded(
        self, artwork_url: str, metadata_generation: int
    ) -> tuple[str | None, asyncio.Task[str | None]]:
        """
        Start the artwork render and wait for it within the bundling budget.

        Called with the metadata lock held. A render that misses the budget is
        never cancelled: the returned task keeps running so the caller can hand
        it to :meth:`_render_and_send_artwork` for the ARTWORK delivery. The
        wait also yields early to a teardown or a pending START.

        :param artwork_url: The cover-art URL to render.
        :param metadata_generation: Generation the render belongs to.
        :return: The rendered artwork path (None when the budget was missed or
            the render failed) and the render task itself.
        """
        artwork_render = asyncio.create_task(
            self._prepare_artwork(artwork_url, metadata_generation)
        )
        # Neither a teardown nor a time-critical START must sit out the render
        # budget behind the metadata lock, so both release this wait early: a
        # teardown's doomed bundle write is then dropped by send_cli_command,
        # and a START's bundle goes out without artwork (the render delivers
        # through the ARTWORK command once it completes).
        teardown = asyncio.ensure_future(self._teardown_started.wait())
        start_waiting = asyncio.ensure_future(self._start_waiting.wait())
        waiters: set[asyncio.Future[Any]] = {artwork_render, teardown, start_waiting}
        try:
            await asyncio.wait(
                waiters,
                timeout=AIRPLAY_ARTWORK_RENDER_TIMEOUT,
                return_when=asyncio.FIRST_COMPLETED,
            )
        except asyncio.CancelledError:
            artwork_render.cancel()
            self._artwork_render_generations.discard(metadata_generation)
            raise
        finally:
            teardown.cancel()
            start_waiting.cancel()
        artwork_file = artwork_render.result() if artwork_render.done() else None
        return artwork_file, artwork_render

    async def _render_and_send_artwork(
        self,
        artwork_url: str,
        metadata_generation: int,
        render: asyncio.Task[str | None] | None = None,
    ) -> None:
        """
        Render and apply artwork for the current metadata generation.

        :param artwork_url: Source URL for the artwork; settles as the
            delivered artwork identity once the binary accepts the command.
        :param metadata_generation: Generation that must still be current before apply.
        :param render: Render already under way for this generation to deliver,
            instead of starting a new one.
        """
        try:
            if render is not None:
                artwork = await render
            else:
                artwork = await self._prepare_artwork(artwork_url, metadata_generation)
        except asyncio.CancelledError:
            if render is not None:
                render.cancel()
            async with self._metadata_lock:
                self._artwork_render_generations.discard(metadata_generation)
            raise
        async with self._metadata_lock:
            self._artwork_render_generations.discard(metadata_generation)
            if (
                artwork
                and not self._stopped
                and not self._stopping
                and metadata_generation == self._metadata_generation
                and await self.send_cli_command(f"ARTWORK={artwork}")
            ):
                self._metadata_artwork_checksum = artwork_url

    async def _build_cli_args(  # noqa: PLR0915
        self,
        use_shared_ptp: bool | None = None,
    ) -> list[str]:
        """
        Assemble the cliairplay argument list for this stream.

        :param use_shared_ptp: Whether a native AirPlay 2 stream attaches to the
            shared PTP clock daemon. The stream session passes an explicit
            group-wide decision so members never mix PTP and NTP timing; None
            reads the daemon's current readiness and decides from that.
        """
        cli_binary = await get_cli_binary()
        prov = cast("AirPlayProvider", self.prov)
        airplay_info = self.player.airplay_discovery_info
        raop_info = self.player.raop_discovery_info
        target_protocol = self.player.protocol_override or self.player.protocol
        if self.player.protocol_override == StreamingProtocol.RAOP:
            protocol_arg = "raop"
        elif target_protocol == StreamingProtocol.AIRPLAY2 and not raop_info:
            # With no RAOP fallback, force AirPlay 2 because featureless AP2-only
            # receivers cannot be identified by the binary's TXT-bit test.
            protocol_arg = "airplay2"
        else:
            protocol_arg = "auto"

        args: list[str] = [
            cli_binary,
            "--protocol",
            protocol_arg,
            "--volume",
            str(self.player.volume_level),
            "--dacp",
            prov.dacp_id,
            "--activeremote",
            self.active_remote_id,
            "--cmdpipe",
            self.commands_pipe.path,
            "--samplerate",
            str(self.pcm_format.sample_rate),
            "--bitdepth",
            str(self.pcm_format.bit_depth),
        ]

        # The binary owns the playback lead/buffer (2000 ms default, clamped to
        # the device-reported window); there is no user override for it.

        # The endpoint must follow the same capability decision as the binary:
        # legacy RAOP uses _raop, while native and RAOP-compatible AP2 use _airplay.
        if target_protocol == StreamingProtocol.AIRPLAY2 and airplay_info:
            args += ["--port", str(airplay_info.port)]
            args += ["--name", self.player.display_name]
            args += ["--hostname", str(airplay_info.server)]
        elif raop_info:
            args += ["--port", str(raop_info.port)]

        # mDNS properties from the RAOP service (needed by the RAOP-based flows)
        if raop_info:
            args += ["--udn", raop_info.name]
            for prop in ("et", "md", "am", "pk", "pw", "cn"):
                if prop_value := raop_info.decoded_properties.get(prop):
                    args += [f"--{prop}", prop_value]
        if target_protocol == StreamingProtocol.RAOP and self.player.config.get_value(
            CONF_ENCRYPTION, True
        ):
            args += ["--encrypt"]

        # Full _airplay._tcp TXT for the binary's automatic route selection.
        # Some receivers advertise their AP2 feature bits only on _raop.ft.
        txt_records = serialize_txt_records(airplay_info) if airplay_info else ""
        if (
            airplay_info
            and not (
                airplay_info.decoded_properties.get("features")
                or airplay_info.decoded_properties.get("ft")
            )
            and raop_info
            and (raop_features := raop_info.decoded_properties.get("ft"))
        ):
            txt_records = f"{txt_records} ft={raop_features}".strip()
        if txt_records:
            args += ["--txt", txt_records]

        # HAP credentials (triggers native AP2 flow when present)
        if creds := self.player.get_setup_value(CONF_AIRPLAY_CREDENTIALS):
            creds_str = str(creds)
            if len(creds_str) == 192:
                args += ["--auth", creds_str]
            else:
                self.player.logger.warning(
                    "Invalid credentials length: %d (expected 192)", len(creds_str)
                )

        # Legacy Apple TV RAOP pairing secret
        if raop_creds := self.player.get_setup_value(CONF_RAOP_CREDENTIALS):
            # Credentials format is "client_id:auth_secret", the binary expects the secret
            creds_str = str(raop_creds)
            auth_secret = creds_str.split(":", 1)[1] if ":" in creds_str else creds_str
            args += ["--secret", auth_secret]

        # Device password
        if password := self.player.config.get_value(CONF_PASSWORD):
            args += ["--password", str(password)]

        # Shared PTP daemon clock (multi-room sync for native AP2 streams). The
        # decision is made once per session and passed in, so every native AP2
        # member of a sync group uses the same timing source and cannot drift.
        # Without a caller-supplied decision (use_shared_ptp is None) the stream
        # gates on the daemon serving rather than merely running: between spawn and
        # the daemon publishing its clock there is nothing to attach to, and a
        # stream that asks anyway silently takes its own timing instead.
        if target_protocol == StreamingProtocol.AIRPLAY2:
            # Deeper receiver queue for devices whose pipeline starves at the
            # stock depth. The stored value wins; Automatic (0) resolves
            # through the same device-family table that seeds the config
            # entry default, so selecting it never downgrades a device.
            depth_ms = cast("int", self.player.config.get_value(CONF_BUFFER_DEPTH) or 0)
            if not depth_ms:
                depth_ms = default_buffer_depth(
                    self.player.device_info.manufacturer or "",
                    self.player.device_info.model or "",
                    get_decoded_property(airplay_info, "fv") if airplay_info else None,
                )
            if depth_ms:
                args += ["--latency", str(depth_ms)]
            shared_ptp = prov.ptp_daemon_ready if use_shared_ptp is None else use_shared_ptp
            if shared_ptp:
                args += ["--ptp-shared"]

        # Local interface binding
        target_ip = str(self.player.device_info.ip_address)
        if_arg = await self.mass.streams.get_source_ip(target_ip)
        if if_arg:
            args += ["--if", if_arg]

        # Address advertised inside the protocol (timing peers) for hosts where the
        # reachable address differs from the bind address (e.g. containers). The binary
        # treats this as authoritative for the receiver's clock-source filter and it
        # outranks its own connection-derived fallback, so only pass an address the user
        # actually configured: an auto-detected one would silence a receiver whenever it
        # names an interface the timing packets do not leave from.
        publish_arg = self.mass.streams.get_publish_ip(target_ip)
        if publish_arg and publish_arg != if_arg:
            args += ["--publish-ip", publish_arg]

        # The addressing the stream ends up with is the first thing needed when triaging
        # a connection or timing issue from a user's log, and it is invisible otherwise:
        # both flags are dropped silently when no value applies here.
        self.player.logger.debug(
            "cliairplay network binding for player %s: if=%s publish_ip=%s",
            self.player.player_id,
            if_arg or "<all interfaces>",
            publish_arg or "<not configured>",
        )

        # Debug level
        if self.prov.logger.isEnabledFor(VERBOSE_LOG_LEVEL):
            args += ["--debug", "10"]
        elif self.prov.logger.isEnabledFor(logging.DEBUG):
            args += ["--debug", "5"]

        # Audio is fed continuously on the process stdin; the binary reads it into
        # a single ring buffer for the whole session lifetime (flushed and
        # refilled in place on a seek, never reconnected).
        args.append(self.player.address)
        return args

    async def _stdout_reader(self) -> None:
        """
        Monitor stdout for the running cliairplay process.

        The binary reports its resolved route at startup, the effective lead
        plus receiver-reported buffering window after connect, the audio formats
        the receiver advertises, and the result of MediaRemote now-playing
        pushes (Apple devices):
          [STATUS] route protocol=<raop|airplay2> flow=<...> timing=<ntp|ptp> buffered=<0|1>
          [STATUS] latency lead_ms=<int> device_min_frames=<int> device_max_frames=<int>
          [STATUS] capabilities requested=<hex> realtime_formats=<hex> realtime_known=<0|1>
            buffered_formats=<hex> buffered_known=<0|1>
          [STATUS] mrp path=<command> status=<http status>
          [EVENT] remote command=<play|pause|play_pause|next|previous>
        """
        if not self._cli_proc:
            return
        buffer = b""
        while chunk := await self._cli_proc.read(1024):
            buffer += chunk
            while b"\n" in buffer:
                raw_line, buffer = buffer.split(b"\n", 1)
                line = raw_line.decode("utf-8", errors="ignore").strip()
                if not line:
                    continue
                if "[STATUS] route" in line:
                    self._parse_route_status(line)
                elif "[STATUS] mrp" in line:
                    self._parse_mrp_status(line)
                elif "[STATUS] latency" in line:
                    self._parse_latency_status(line)
                elif "[STATUS] capabilities" in line:
                    self._parse_capabilities_status(line)
                elif line.startswith("[EVENT] remote command="):
                    self._parse_remote_event(line)
                self.player.logger.log(
                    VERBOSE_LOG_LEVEL, "cliairplay for %s: %s", self.player.display_name, line
                )

    def _parse_remote_event(self, line: str) -> None:
        """Dispatch a normalized remote command reported by cliairplay."""
        command_value = line.removeprefix("[EVENT] remote command=").strip()
        try:
            command = AirPlayRemoteCommand(command_value)
        except ValueError:
            self.player.logger.warning(
                "Ignoring unknown cliairplay remote command: %s", command_value
            )
            return
        # Receiver storm guard: MediaRemote re-sends an unfulfilled transport
        # command at ~10 Hz (measured on tvOS: a next-track storm at end of
        # item drowned the whole server in seeks until playback collapsed).
        # No human intends repeats that fast, so only one command per window
        # is honored; the suppressed repeats are counted and reported loudly
        # as the support signal.
        window = (
            2.0 if command in (AirPlayRemoteCommand.NEXT, AirPlayRemoteCommand.PREVIOUS) else 0.5
        )
        now = time.monotonic()
        last = self._remote_command_last.get(command_value, 0.0)
        if now - last < window:
            self._remote_commands_suppressed += 1
            suppressed = self._remote_commands_suppressed
            if suppressed in (1, 10) or suppressed % 100 == 0:
                self.player.logger.warning(
                    "Storm guard: suppressed %d repeated remote '%s' "
                    "command(s) from %s within %.1fs",
                    suppressed,
                    command_value,
                    self.player.display_name,
                    window,
                )
            return
        self._remote_command_last[command_value] = now
        self._remote_commands_suppressed = 0
        prov = cast("AirPlayProvider", self.prov)
        prov.handle_remote_command(self.player, command)

    def _parse_mrp_status(self, line: str) -> None:
        """
        Parse a [STATUS] mrp line and report how the now-playing push landed.

        A push the device accepted is routine bookkeeping and stays at debug.
        A rejection is not: it is why a now-playing screen stays blank or keeps
        the previous track's art, with nothing else about the session looking
        wrong, so it is reported.

        :param line: The status line, in one of the shapes
            ``[STATUS] mrp path=<command|channel> status=<int>`` or
            ``[STATUS] mrp artwork=<posted|rejected> ...``.
        """
        fields = dict(part.split("=", 1) for part in line.split() if "=" in part)
        display_name = self.player.display_name
        if artwork := fields.get("artwork"):
            # The artwork variants carry no path=, and a rejection reports its
            # reason plus a clear_status rather than a status of its own.
            if artwork == "rejected":
                self.player.logger.warning(
                    "%s rejected the now-playing artwork (%s, %s bytes); its screen keeps "
                    "whatever art it had",
                    display_name,
                    fields.get("reason", "no reason given"),
                    fields.get("bytes", "?"),
                )
            else:
                self.player.logger.debug(
                    "MRP now-playing artwork accepted by %s (%s bytes, HTTP %s)",
                    display_name,
                    fields.get("bytes", "?"),
                    fields.get("status", "?"),
                )
            return
        if fields.get("path") == "channel":
            # Not an HTTP status: 0 = the opt-in data channel was attempted and
            # did not come up, 1 = established.
            self.player.logger.debug(
                "MRP data channel for %s: %s",
                display_name,
                "established" if fields.get("status") == "1" else "not established",
            )
            return
        # Only a 2xx means the device took the push. Nothing else on this
        # control channel does - a redirect is as much "not accepted" as a 4xx.
        # A status of 0 is the "unreported" reading (the field is missing or
        # unusable), which says nothing about how the push landed, so only a
        # reported status is judged - warning about a field the binary never
        # sent would report a rejection that nothing observed.
        status = _status_int(fields, "status")
        rejected = bool(status) and not (HTTPStatus.OK <= status < HTTPStatus.MULTIPLE_CHOICES)
        self.player.logger.log(
            logging.WARNING if rejected else logging.DEBUG,
            "MRP now-playing push (%s path) for %s: HTTP %s",
            fields.get("path", "?"),
            display_name,
            fields.get("status", "?"),
        )

    def _parse_capabilities_status(self, line: str) -> None:
        """Parse the [STATUS] capabilities line and refresh the player's audio formats."""
        # The binary reports the format tables it read from the receiver's /info,
        # which corrects a device that was unreachable when it was discovered.
        # Only the native AirPlay 2 flow reads them; the other routes report
        # zeroes with the known flags unset. A change applies to the next stream.
        fields = dict(part.split("=", 1) for part in line.split() if "=" in part)
        formats = 0
        for mask_field, known_field in (
            ("realtime_formats", "realtime_known"),
            ("buffered_formats", "buffered_known"),
        ):
            if fields.get(known_field) != "1":
                continue
            try:
                formats |= int(fields[mask_field], 16)
            except KeyError, ValueError:
                continue
        if formats and formats != self.player.advertised_audio_formats:
            self.player.logger.debug(
                "Audio formats advertised by %s changed to 0x%x",
                self.player.display_name,
                formats,
            )
            self.player.advertised_audio_formats = formats

    def _parse_route_status(self, line: str) -> None:
        """Parse the [STATUS] route line and log which route this stream took."""
        fields = dict(part.split("=", 1) for part in line.split() if "=" in part)
        protocol = fields.get("protocol", "")
        if protocol == "airplay2":
            flow = fields.get("flow", "")
            timing = fields.get("timing", "")
            details = "buffered" if fields.get("buffered") == "1" else flow
            self.active_route = f"AirPlay 2 ({details}, {timing.upper()})"
        else:
            self.active_route = "RAOP"
        self.player.logger.info(
            "Streaming to %s via %s", self.player.display_name, self.active_route
        )

    def _parse_latency_status(self, line: str) -> None:
        """Parse and store the [STATUS] latency line reported by the binary."""
        fields = dict(part.split("=", 1) for part in line.split() if "=" in part)
        # Read field by field: one unusable value used to abandon the rest of
        # the line, leaving whatever came after it stale from the previous
        # report while the values before it had already moved. Every field here
        # means "unreported" at 0, so a missing or malformed one lands there.
        self.latency_lead_ms = _status_int(fields, "lead_ms")
        self.device_min_frames = _status_int(fields, "device_min_frames")
        self.device_max_frames = _status_int(fields, "device_max_frames")
        self.warm_lead_ms = _status_int(fields, "warm_lead_ms")
        self.player.logger.debug(
            "Device latency for %s: lead=%dms, warm lead=%dms, "
            "buffer window=%d-%d frames (0=unreported)",
            self.player.display_name,
            self.latency_lead_ms,
            self.warm_lead_ms,
            self.device_min_frames,
            self.device_max_frames,
        )

    async def _stderr_reader(self) -> None:
        """
        Monitor stderr for the running cliairplay process.

        The binary emits normalized [STATUS] messages:
          [STATUS] connected
          [STATUS] playing elapsed_ms=<ms>
          [STATUS] paused
          [STATUS] eof
          [STATUS] error code=<slug> http=<int> detail="<short text>"
            (auth_required/auth_failed/connect_failed are terminal; the
             start_failed/flush_failed command slugs answer a pending ack)
          [ERROR] <message>
        """
        player = self.player
        logger = player.logger
        if not self._cli_proc:
            return
        async for line in self._cli_proc.iter_stderr():
            if self._stopped:
                break
            if self._handle_status_line(line):
                self.ended_cleanly = True
                break
            # Routine binary output is verbose-only so it never floods a user's log, but
            # its own diagnostics (a failed socket bind, a missing receiver clock) are the
            # first thing needed when triaging silent playback, so those stay visible.
            # Every line names its speaker: the provider logger is shared, so the output
            # of several concurrent streams is otherwise impossible to tell apart.
            level = (
                logging.WARNING
                if any(marker in line.lower() for marker in CLI_PROBLEM_MARKERS)
                else VERBOSE_LOG_LEVEL
            )
            logger.log(level, "cliairplay for %s: %s", player.display_name, line)
            await asyncio.sleep(0)

        logger.debug("cliairplay stderr reader ended for %s", player.display_name)
        self._process_ended.set()
        if not self._stopped and not self._stopping:
            self._stopped = True
            try:
                if not self.ended_cleanly:
                    logger.warning(
                        "cliairplay process stopped unexpectedly for %s", player.display_name
                    )
                    # Candidates for the automatic re-join: the leader this member
                    # was synced to (plus its other members, in case leadership
                    # transfers while the backoff runs), or - when this was the
                    # leader itself - the members that survive it. Captured before
                    # the ungroup below mutates the group state (create_task
                    # starts eagerly).
                    was_leader = bool(player.group_members)
                    if player.synced_to:
                        rejoin_candidates = [player.synced_to]
                        if leader := self.mass.players.get_player(player.synced_to):
                            rejoin_candidates += [
                                member_id
                                for member_id in leader.group_members
                                if member_id not in (player.player_id, player.synced_to)
                            ]
                    else:
                        rejoin_candidates = [
                            m for m in player.group_members if m != player.player_id
                        ]
                    # Hand off to the player controller so it drops just this member, or
                    # transfers leadership to a healthy member, instead of dissolving the
                    # whole group over a single dead transport. A sync leader is left in
                    # its current state here on purpose: the controller only transfers
                    # leadership while the queue still looks active, and transfer_queue or
                    # dissolve sets the final state.
                    # One exception: a member that is a STATIC member of an active group
                    # player must not go through cmd_ungroup - the controller interprets
                    # unjoining a static member as releasing the whole group (HA unjoin
                    # semantics), which would silence every room over one dead transport.
                    # Its membership is configuration; drop only this member from the
                    # leader's live session instead. The set_members call cannot bounce
                    # back to the group player: the controller only redirects it when
                    # the group advertises SET_MEMBERS, which a static group never does.
                    static_member_of = self._static_group_membership(player)
                    if static_member_of and player.synced_to:
                        self.mass.create_task(
                            self.mass.players.cmd_set_members(
                                player.synced_to, player_ids_to_remove=[player.player_id]
                            )
                        )
                    else:
                        self.mass.create_task(self.mass.players.cmd_ungroup(player.player_id))
                    if rejoin_candidates:
                        # the group (or its successor) may still be playing:
                        # schedule bounded attempts to re-join it
                        player.schedule_group_rejoin(rejoin_candidates)
                    if was_leader:
                        return
                player.set_state_from_stream(state=PlaybackState.IDLE, elapsed_time=0, stream=self)
            finally:
                await self.commands_pipe.remove()

    def _static_group_membership(self, player: AirPlayPlayer) -> str | None:
        """Return the active group player id the player is a static member of, if any."""
        active_group_id = player.state.active_group
        if not active_group_id:
            return None
        group_player = self.mass.players.get_player(active_group_id)
        if group_player and player.player_id in group_player.static_group_members:
            return active_group_id
        return None

    def _handle_status_line(self, line: str) -> bool:  # noqa: PLR0915
        """Dispatch one cliairplay status line; True ends the stderr loop."""
        player = self.player
        if "[STATUS] connected" in line:
            self._connected.set()
            # whatever the device accepted just now is a working password
            player.set_password_invalid(False)
        elif "[STATUS] playing elapsed_ms=" in line:
            try:
                millis = int(line.split("elapsed_ms=")[1])
            except ValueError, IndexError:
                pass
            else:
                self._update_elapsed(millis / 1000)
        elif "[STATUS] paused" in line:
            player.set_state_from_stream(state=PlaybackState.PAUSED, stream=self)
        elif "[STATUS] started " in line:
            try:
                fields = dict(part.split("=", 1) for part in line.split() if "=" in part)
                requested = int(fields.get("requested_unix_ms", 0))
                actual = int(fields.get("at_unix_ms", 0))
            except ValueError, IndexError:
                # Malformed ack: leave _start_ack unset so the caller falls
                # back to trusting the commanded instant, without waiting out
                # the ack timeout.
                pass
            else:
                # A line missing at_unix_ms parses as 0, which is never a real
                # instant: treat it like the malformed ack above rather than
                # handing the caller 0 as the scheduled instant.
                if actual:
                    self._start_ack = (requested, actual)
                if requested and actual and abs(actual - requested) > 2:
                    # A correction on its own is self-healed: a join lands on it
                    # by design, and a solo start simply adopts it as the anchor.
                    # Only the session knows when one costs something - a group
                    # that has to be re-anchored to converge - and it raises that
                    # to a warning itself.
                    player.logger.info(
                        "AirPlay start corrected by %+d ms on %s (requested %d, scheduled %d)",
                        actual - requested,
                        player.display_name,
                        requested,
                        actual,
                    )
            # An ack and a failure are the two mutually exclusive answers to one
            # START, so each clears the other: the caller then reads whichever
            # answer released its wait, with nothing left from the previous one.
            self._start_error = None
            self._started.set()
        elif "[STATUS] anchor_corrected " in line:
            self._parse_anchor_corrected(line)
        elif "[STATUS] content_cut " in line:
            self._parse_content_cut(line)
        elif "[STATUS] clock_ready " in line:
            self._parse_clock_ready(line)
        elif "[STATUS] clock_verified" in line:
            self._parse_clock_verified(line)
        elif "[STATUS] flushed" in line:
            # A splice-timeline member reports the audible instant of its
            # frozen delivery head; the warm START must anchor beyond it (a
            # commanded instant at or behind the head splices at the head,
            # silently breaking the shared instant).
            if "head_unix_ms=" in line:
                try:
                    self.flushed_head_unix_ms = int(
                        line.split("head_unix_ms=")[1].split(maxsplit=1)[0]
                    )
                except ValueError, IndexError:
                    self.flushed_head_unix_ms = 0
            else:
                self.flushed_head_unix_ms = 0
            self._flush_error = None
            self._flushed.set()
        elif "[STATUS] audio " in line:
            self._audio_present.set()
        elif "[STATUS] mrp" in line:
            # The artwork reports arrive on stderr; the now-playing push
            # status arrives on stdout. One parser serves both shapes, so
            # each reader dispatches the mrp lines its own pipe carries.
            self._parse_mrp_status(line)
        elif "[STATUS] idle_timeout" in line:
            # a parked (paused) session outlived the binary's idle cap;
            # treat it as a normal end of stream
            player.logger.debug("cliairplay idle timeout reached")
            return True
        elif "[STATUS] eof" in line:
            player.logger.debug("End of stream reached")
            return True
        elif "[STATUS] REANCHOR" in line:
            self._parse_reanchor_status(line)
        elif "[STATUS] error " in line:
            self._parse_error_status(line)
        elif "[ERROR]" in line:
            player.logger.error("cliairplay: %s", line.strip())
        return False

    def _update_elapsed(self, elapsed_time: float) -> None:
        """Update elapsed time against the current start anchor's media position."""
        # the binary's elapsed restarts at each START; report against its base
        elapsed_time += self._start_position
        # The binary only emits this status while actually playing, so it is
        # also the signal that drives the player into the PLAYING state.
        self.player.set_state_from_stream(
            state=PlaybackState.PLAYING, elapsed_time=elapsed_time, stream=self
        )

    def _parse_reanchor_status(self, line: str) -> None:
        """
        Parse the machine-readable [STATUS] REANCHOR line.

        The binary reports the shift cumulative since the last start/resume
        directly, so this SETS the tracked shift, using the sample rate carried
        on the line when present.

        :param line: The status line, e.g. ``[STATUS] REANCHOR shifted_frames=67870
            total_shifted_frames=135740 sample_rate=44100``.
        """
        fields = dict(part.split("=", 1) for part in line.split() if "=" in part)
        try:
            total_frames = int(fields["total_shifted_frames"])
        except KeyError, ValueError:
            return
        self.cumulative_shift_seconds = total_frames / self._reanchor_sample_rate(
            fields.get("sample_rate")
        )
        self.player.logger.debug(
            "cliairplay re-anchored %s after PCM starvation: cumulative shift %.3fs",
            self.player.display_name,
            self.cumulative_shift_seconds,
        )

    def _reanchor_sample_rate(self, reported: str | None) -> int:
        """Return the frame->seconds rate, preferring a valid rate reported on the line."""
        if reported is not None:
            try:
                rate = int(reported)
            except ValueError:
                rate = 0
            if rate > 0:
                return rate
        return self.pcm_format.sample_rate or 44100

    async def _prepare_artwork(self, image_url: str, _generation: int) -> str | None:
        """
        Return a cached JPEG path for the binary to embed.

        The binary consumes artwork as a local file only; it does not fetch
        URLs. The image is flattened to JPEG and stored in the shared thumbnail
        cache.

        :param image_url: The (imageproxy or remote) cover-art URL.
        :param _generation: Metadata generation associated with the render request.
        """
        try:
            return await get_image_thumb_path(
                self.mass,
                image_url,
                AIRPLAY_ARTWORK_SIZE,
                "",
                image_format="JPEG",
                flatten_transparency=True,
            )
        except Exception as err:
            self.player.logger.debug("Could not prepare artwork: %s", err)
            return None

    async def _send_current_metadata(self, send_artwork: bool = True) -> None:
        """
        Send metadata for the media owned by the active stream.

        :param send_artwork: Whether artwork should be rendered and sent.
        """
        metadata = self.session.media if self.session else self.player.current_media
        if not metadata:
            return
        progress = int(metadata.corrected_elapsed_time or 0)
        await self.send_metadata(progress, metadata, send_artwork=send_artwork)

    async def _send_current_metadata_without_progress(self) -> None:
        """
        Send only the identity metadata for the active stream's media.

        Used right after a commanded START: the anchor may still be settling,
        so the position correction is left to the post-anchor media-updated
        nudge — one settled now-playing refresh on the device instead of two.
        """
        metadata = self.session.media if self.session else self.player.current_media
        if not metadata:
            return
        await self.send_metadata(None, metadata)

    async def _send_current_volume(self) -> None:
        """Send the player's current volume level to the device, muted as zero."""
        volume = 0 if self.player.volume_muted else self.player.volume_level
        await self.send_cli_command(f"VOLUME={volume}")

    async def _cleanup_failed_start(self) -> None:
        """Release all resources owned by a cliairplay process that failed to start."""
        self._stopping = True
        self._stopped = True
        stdout_reader_task = self._stdout_reader_task
        if stdout_reader_task and not stdout_reader_task.done():
            stdout_reader_task.cancel()
            try:
                await stdout_reader_task
            except asyncio.CancelledError:
                pass
            except Exception as err:
                self.player.logger.debug("cliairplay stdout reader cleanup failed: %s", err)
        try:
            if self._cli_proc and not self._cli_proc.closed:
                await self._cli_proc.kill()
        finally:
            await self.commands_pipe.remove()
            self._cleanup_complete = True
            self._cli_proc = None

    def _arm_start_answer(self) -> None:
        """Clear the slots the binary answers a START in, so only this one's answer is read."""
        # The ack and the failure are filled by the stderr reader while start()
        # waits, so both slots have to be emptied at the command itself: a
        # rejection left over from the previous START would otherwise be read
        # as this one's answer the moment an ack releases the wait.
        self._started.clear()
        self._start_ack = None
        self._start_error = None

    def _arm_flush_answer(self) -> None:
        """Clear the slots the binary answers a FLUSH in, so only this one's answer is read."""
        self._flushed.clear()
        self._flush_error = None

    async def _write_cli_command(self, command: str) -> bool:
        """Write an interactive command regardless of stream teardown state."""
        if not self._cli_proc or self._cli_proc.closed:
            return False
        if not command.endswith("\n"):
            command += "\n"
        command_delivered = await self.commands_pipe.write(command.encode("utf-8"))
        if command_delivered:
            self.player.last_command_sent = time.time()
        return command_delivered

    def _check_password_preflight(self) -> None:
        """
        Refuse a native AirPlay 2 connect that has nothing to authenticate with.

        A password-protected receiver answers the RTSP setup with a 401 unless the
        binary can present the device password or stored pairing credentials, so
        without either there is no point in spawning the process at all. A player
        in this state is already blocked from playback by ``needs_setup``, leaving
        this as the backstop for a device that only announced its password
        protection after the player was resolved as a playback target.

        :raises PlayerCommandFailed: If the device password is missing.
        """
        target_protocol = self.player.protocol_override or self.player.protocol
        if target_protocol != StreamingProtocol.AIRPLAY2 or not self.player.password_required:
            return
        if self.player.config.get_value(CONF_PASSWORD):
            return
        # stored credentials keep the binary's pair-verify leg viable, and its own
        # failure report guides the user when that leg is rejected after all
        if self.player.get_setup_value(CONF_AIRPLAY_CREDENTIALS) or self.player.get_setup_value(
            CONF_RAOP_CREDENTIALS
        ):
            return
        raise self._password_required_error()

    async def _await_connected(self, timeout: float = 10) -> None:
        """
        Wait for the binary to confirm the device connection.

        :param timeout: Seconds to wait for the confirmation.
        """
        waiters = [
            asyncio.ensure_future(self._connected.wait()),
            asyncio.ensure_future(self._process_ended.wait()),
        ]
        try:
            await asyncio.wait(waiters, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for waiter in waiters:
                waiter.cancel()
        if self._connected.is_set():
            return
        raise self._connect_failed_error()

    def _connect_failed_error(self) -> Exception:
        """
        Return the error for a connection that was never established.

        A binary that reported why it gave up produces a specific, actionable
        error. Everything else - including an unreported reason - keeps the plain
        timeout the callers already handle.
        """
        error = self._connect_error
        if error and error.code == CLI_ERROR_AUTH_REQUIRED:
            return self._password_required_error()
        if error and error.code == CLI_ERROR_AUTH_FAILED:
            return PlayerCommandFailed(
                f"{self.player.display_name} rejected the saved password. "
                "Run the setup for this player to enter it again.",
                translation_key="authentication_failed",
                translation_owner=self.player.translation_owner,
            )
        reason = f": {error.detail}" if error and error.detail else ""
        return TimeoutError(f"cliairplay did not connect to {self.player.display_name}{reason}")

    def _password_required_error(self) -> PlayerCommandFailed:
        """Return the error that points the user at the player's setup flow."""
        return PlayerCommandFailed(
            f"{self.player.display_name} requires a password. "
            "Run the setup for this player to enter it.",
            translation_key="password_required",
        )

    def _parse_error_status(self, line: str) -> None:
        """Parse the structured failure the binary reports and route it to its waiter."""
        payload = line.split("[STATUS] error ", 1)[-1]
        code_match = _CLI_ERROR_CODE_RE.search(payload)
        http_match = _CLI_ERROR_HTTP_RE.search(payload)
        detail_match = _CLI_ERROR_DETAIL_RE.search(payload)
        error = CliError(
            code=code_match.group(1) if code_match else "",
            http_status=int(http_match.group(1)) if http_match else 0,
            detail=detail_match.group(1) if detail_match else "",
        )
        self.player.logger.debug(
            "cliairplay reported an error for %s: code=%s http=%s detail=%s",
            self.player.display_name,
            error.code,
            error.http_status,
            error.detail,
        )
        # A rejected transport command leaves the connection alive, so it
        # answers only the ack it failed - never the connect error, which
        # decides how a NEW connection is reported to the user.
        if error.code == CLI_ERROR_START_FAILED:
            self._start_error = error
            self._started.set()
            return
        if error.code == CLI_ERROR_FLUSH_FAILED:
            self._flush_error = error
            self._flushed.set()
            return
        self._connect_error = error
        if error.code in (CLI_ERROR_AUTH_FAILED, CLI_ERROR_AUTH_REQUIRED):
            # The stored password is wrong, or the device demanded one we could
            # not supply (devices can enforce a password without announcing it -
            # e.g. an Apple TV with stale TXT records after the password was
            # enabled). Persist that so the player keeps offering its setup
            # action (across restarts) until a working password is entered,
            # instead of only failing at the next play attempt.
            self.player.set_password_invalid(True)

    def _parse_anchor_corrected(self, line: str) -> None:
        """
        Parse a post-commit [STATUS] anchor_corrected line and re-base the position.

        The binary emits this at most once per START, when a receiver clock
        exchange that only resumed after the START ack finds the committed
        instant infeasible: it moves the anchor forward and advances the queued
        content by the same amount (``content_cut_ms``), so the member still
        lands on the group timeline — only the reported media position shifts.
        That amount is what the correction ASKED for; :meth:`_parse_content_cut`
        reconciles it with what the cut managed to take.

        :param line: The status line, e.g. ``[STATUS] anchor_corrected
            requested_unix_ms=1750000000000 from_unix_ms=1750000000400
            at_unix_ms=1750000000900 content_cut_ms=500``.
        """
        try:
            fields = dict(part.split("=", 1) for part in line.split() if "=" in part)
            requested_unix_ms = int(fields.get("requested_unix_ms", 0))
            from_unix_ms = int(fields.get("from_unix_ms", 0))
            at_unix_ms = int(fields.get("at_unix_ms", 0))
            content_cut_ms = int(fields.get("content_cut_ms", 0))
        except ValueError, IndexError:
            # Malformed line: drop it rather than react to bogus numbers.
            return
        # The binary's elapsed counts only the retained content, so the
        # position base moves by the cut to keep reported progress exact.
        self._start_position += content_cut_ms / 1000
        # Track the cut owed the same way the base above tracks it: both
        # accumulate over an anchor and both are zeroed at every anchor
        # boundary. Overwriting here would settle only the newest correction
        # against a base that carries all of them.
        self._pending_content_cut_ms += content_cut_ms
        # Routine for a join start: the low join headroom defers to this
        # correction, which lands the anchor at exact receiver readiness. A
        # post-commit correction on any other START stays loud.
        self.player.logger.log(
            logging.INFO if self._start_was_join else logging.WARNING,
            "AirPlay anchor for %s corrected %+d ms after commit "
            "(requested %d, at %d, content advanced %d ms to stay in sync)",
            self.player.display_name,
            at_unix_ms - from_unix_ms,
            requested_unix_ms,
            at_unix_ms,
            content_cut_ms,
        )

    def _parse_content_cut(self, line: str) -> None:
        """
        Settle a corrected anchor's content cut against the cut it asked for.

        ``anchor_corrected`` reports the cut arithmetic on two instants demands,
        which :meth:`_parse_anchor_corrected` folds into the position base right
        away. This line reports what the cut actually took once the last byte is
        discarded, and the two disagree when the cut ended short — the input ran
        out inside it, or a teardown settled it. Every ms it fell short is a ms
        the reported position stays over-advanced by for the rest of the anchor,
        so the base is corrected back and the shortfall reported.

        :param line: The status line, e.g. ``[STATUS] content_cut
            requested_ms=500 cut_ms=180 cut_bytes=31752 drain_ms=210``.
        """
        try:
            fields = dict(part.split("=", 1) for part in line.split() if "=" in part)
            cut_ms = int(fields["cut_ms"])
            requested_ms = int(fields.get("requested_ms", 0))
            cut_bytes = int(fields.get("cut_bytes", 0))
            drain_ms = int(fields.get("drain_ms", 0))
        except KeyError, ValueError, IndexError:
            # Malformed line: drop it rather than react to bogus numbers.
            return
        applied_ms = self._pending_content_cut_ms
        self._pending_content_cut_ms = 0
        if not applied_ms:
            # The cut settled against an anchor whose base this stream no longer
            # reports on (a START or a join re-base landed in between), so there
            # is nothing of it left to correct.
            self.player.logger.debug(
                "AirPlay content cut on %s settled after the anchor it belonged to "
                "(requested %d ms, cut %d ms)",
                self.player.display_name,
                requested_ms,
                cut_ms,
            )
            return
        # A cut can miss the amount it was asked for in either direction, and
        # both leave the base wrong by the difference, so the magnitude decides
        # whether it settled cleanly. Re-basing by the signed difference then
        # corrects either one; only the report differs.
        shortfall_ms = applied_ms - cut_ms
        if abs(shortfall_ms) < AIRPLAY_CONTENT_CUT_TOLERANCE_MS:
            self.player.logger.debug(
                "AirPlay content cut on %s took the full %d ms (%d bytes in %d ms)",
                self.player.display_name,
                cut_ms,
                cut_bytes,
                drain_ms,
            )
            return
        self._start_position -= shortfall_ms / 1000
        if shortfall_ms > 0:
            self.player.logger.warning(
                "AirPlay content cut on %s fell %d ms short: the corrected anchor asked "
                "for %d ms and the cut took %d ms (%d bytes in %d ms). Reported position "
                "re-based by -%d ms; playback is that much ahead of the group timeline.",
                self.player.display_name,
                shortfall_ms,
                applied_ms,
                cut_ms,
                cut_bytes,
                drain_ms,
                shortfall_ms,
            )
            return
        overcut_ms = -shortfall_ms
        self.player.logger.warning(
            "AirPlay content cut on %s overran by %d ms: the corrected anchor asked "
            "for %d ms and the cut took %d ms (%d bytes in %d ms). Reported position "
            "was under-advanced by that much and is re-based by +%d ms.",
            self.player.display_name,
            overcut_ms,
            applied_ms,
            cut_ms,
            cut_bytes,
            drain_ms,
            overcut_ms,
        )

    def _parse_clock_ready(self, line: str) -> None:
        """
        Parse a [STATUS] clock_ready line into the receiver's readiness projection.

        A ``stalled`` state means the receiver never answered our clock and will
        render silence, which is warned about once per stream session.

        :param line: The status line, e.g. ``[STATUS] clock_ready mode=ptp
            state=probing streak_ms=0 exchanges=1 ready_in_ms=2300
            ready_at_unix_ms=1750000002300``.
        """
        try:
            fields = dict(part.split("=", 1) for part in line.split() if "=" in part)
            mode = fields.get("mode", "")
            state = fields.get("state", "")
            ready_at_unix_ms = int(fields.get("ready_at_unix_ms", 0))
        except ValueError, IndexError:
            # Malformed line: drop it rather than react to bogus numbers.
            return
        if state == "cold" and mode != "ntp":
            # No probe seen yet, so the line carries no projection; the binary
            # keeps reporting until one exists.
            return
        stalled = state == "stalled" and mode != "ntp"
        if stalled and not self._clock_stall_warned:
            # The receiver is not slaving to our clock at all, so it renders
            # silence while everything else about the session looks healthy.
            self._clock_stall_warned = True
            self.player.logger.warning(
                "%s has not answered the server's PTP clock (%s clock exchange(s), "
                "probe streak %s ms), so it will not play any audio. Check that UDP "
                "319/320 traffic can flow between the speaker and the server.",
                self.player.display_name,
                fields.get("exchanges", "?"),
                fields.get("streak_ms", "?"),
            )
        # NTP timing has no receiver clock to wait for, and a state without a
        # projection resolves the wait with nothing so a caller falls back
        # instead of blocking on evidence that will not arrive. A stalled clock
        # is one of those states however the line is numbered — but the caller
        # has to be able to tell the three apart, so each carries its own
        # readiness rather than a bare missing instant.
        if mode == "ntp":
            self._clock_readiness = ClockReadiness.NOT_APPLICABLE
        elif stalled:
            self._clock_readiness = ClockReadiness.STALLED
        else:
            self._clock_readiness = ClockReadiness.PROJECTED
        self._clock_ready_at_unix_ms = (
            ready_at_unix_ms if self._clock_readiness is ClockReadiness.PROJECTED else 0
        )
        self._clock_ready.set()
        self.player.logger.debug(
            "cliairplay reports the clock for %s as %s (mode=%s, readiness=%s, usable at %d)",
            self.player.display_name,
            state,
            mode,
            self._clock_readiness,
            self._clock_ready_at_unix_ms,
        )

    def _parse_clock_verified(self, line: str) -> None:
        """Debug-log a [STATUS] clock_verified line; no correction means no server action."""
        try:
            margin_ms = int(line.split("margin_ms=")[1])
        except ValueError, IndexError:
            return
        self.player.logger.debug(
            "cliairplay clock verified for %s (margin %d ms)",
            self.player.display_name,
            margin_ms,
        )


def _status_int(fields: Mapping[str, str], key: str) -> int:
    """
    Return one integer field of a [STATUS] line, or 0 when it is unusable.

    Status lines are read field by field so a value the binary could not format
    - or one an older build does not emit at all - does not take the rest of the
    line down with it. Every field read this way means "unreported" at 0.

    :param fields: The parsed key=value pairs of the status line.
    :param key: The field to read.
    """
    try:
        return int(fields[key])
    except KeyError, ValueError:
        return 0
