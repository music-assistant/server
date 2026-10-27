"""Playback session state of the Ynison AudioSource."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

# After our own track change or seek, inbound Ynison progress may still be the
# old position; drift inside this window is not a user seek.
ECHO_GRACE_PERIOD = 3.0


class RestartReason(StrEnum):
    """Why the audio stream restarts its current item."""

    TRACK_CHANGE = "track_change"
    SEEK = "seek"
    RESUME = "resume"
    REPEAT = "repeat"


class StopReason(StrEnum):
    """Why the audio stream must end."""

    PAUSE = "pause"
    PROVIDER_LOST = "provider_lost"
    STREAM_ERROR = "stream_error"
    QUEUE_END = "queue_end"
    CLEARED = "cleared"


@dataclass(frozen=True)
class ClaimToken:
    """The claim an audio generator started under."""

    owner_player_id: str
    session_id: str | None


class PlaybackSession:
    """
    Own the AudioSource claim, stream cursor, restart and stop requests.

    The claim names the user-facing owner player, the physical consumer (a
    protocol bridge when present) and the controller's stream session id. A
    restart request carries the position and reason of the next stream start;
    a stop request ends the stream. Fields not named here are plain playback
    facts maintained by the provider.
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        """
        Create an idle session.

        :param clock: Monotonic clock used for the echo grace window.
        """
        self._clock = clock
        self._restart = asyncio.Event()
        self.owner_player_id: str | None = None
        self.consumer_player_id: str | None = None
        self.session_id: str | None = None
        self.track_id: str | None = None
        self.index = -1
        # Leading items our own window trim removed, not yet confirmed by Ynison.
        self.pending_index_shift = 0
        self.progress_ms = 0
        self.duration_ms = 0
        self.externally_paused = False
        self.restart_at_ms = 0
        self.restart_reason: RestartReason | None = None
        self.grace_until = 0.0
        self.stop_reason: StopReason | None = None

    def restart_requested(self) -> bool:
        """Return whether a restart is waiting to be consumed by the stream."""
        return self._restart.is_set()

    def stop_requested(self) -> bool:
        """Return whether the stream must end."""
        return self.stop_reason is not None

    def claim(self, owner_player_id: str, consumer_player_id: str, session_id: str) -> None:
        """
        Record the player and controller request that own the source.

        :param owner_player_id: User-facing player that holds the source session.
        :param consumer_player_id: Player that physically consumes the stream.
        :param session_id: Controller stream session id of this request.
        """
        self.owner_player_id = owner_player_id
        self.consumer_player_id = consumer_player_id
        self.session_id = session_id

    def release(self, owner_player_id: str, session_id: str) -> None:
        """
        Release the claim when its own request is torn down.

        :param owner_player_id: Owner named by the teardown callback.
        :param session_id: Session id named by the teardown callback.
        """
        if session_id != self.session_id:
            return
        self.session_id = None
        if self.owner_player_id == owner_player_id:
            self.owner_player_id = None

    def capture(self) -> ClaimToken | None:
        """Return the current claim for a starting stream, or ``None`` without one."""
        if self.owner_player_id is None:
            return None
        return ClaimToken(self.owner_player_id, self.session_id)

    def lost(self, token: ClaimToken | None) -> bool:
        """
        Return whether a captured claim was superseded or released.

        :param token: Claim captured at stream start; ``None`` is never lost.
        """
        return token is not None and (
            self.owner_player_id != token.owner_player_id or self.session_id != token.session_id
        )

    def release_if_current(self, token: ClaimToken | None) -> None:
        """
        Release the owner when the stream that captured it ends unsuperseded.

        :param token: Claim captured at stream start.
        """
        if token is not None and not self.lost(token):
            self.owner_player_id = None

    def request_restart(self, at_ms: int, reason: RestartReason, *, grace: bool = True) -> None:
        """
        Ask the stream to restart its current item.

        :param at_ms: Position to start from in milliseconds.
        :param reason: Why the stream restarts.
        :param grace: Treat inbound drift as an echo for :data:`ECHO_GRACE_PERIOD`.
        """
        self.restart_at_ms = at_ms
        self.restart_reason = reason
        if grace:
            self.grace_until = self._clock() + ECHO_GRACE_PERIOD
        self._restart.set()

    def seed_start(self, at_ms: int) -> None:
        """
        Start from a controller-supplied position unless a restart already set one.

        :param at_ms: Position in milliseconds.
        """
        if at_ms > 0 and self.restart_at_ms == 0:
            self.restart_at_ms = at_ms
            self.restart_reason = RestartReason.RESUME

    def consume_restart(self) -> None:
        """Acknowledge the wake-up; the requested position stays until taken."""
        self._restart.clear()

    def take_start(self) -> tuple[int, RestartReason | None]:
        """Return and clear the position and reason of the next stream start."""
        start = (self.restart_at_ms, self.restart_reason)
        self.restart_at_ms = 0
        self.restart_reason = None
        return start

    async def wait_restart(self, timeout: float) -> bool:
        """
        Wait for a restart request.

        :param timeout: Maximum seconds to wait.
        :return: ``False`` on timeout.
        """
        try:
            await asyncio.wait_for(self._restart.wait(), timeout=timeout)
        except TimeoutError:
            return False
        return True

    def in_grace(self) -> bool:
        """Return whether inbound progress drift is still treated as an echo."""
        return self._clock() < self.grace_until

    def request_stop(self, reason: StopReason) -> None:
        """
        Ask the stream to end.

        :param reason: Why the stream ends.
        """
        self.stop_reason = reason

    def begin_stream(self) -> None:
        """Start a new stream; an earlier stop request no longer applies."""
        self.stop_reason = None

    def resume(self) -> bool:
        """
        Clear stop and external-pause markers before playback resumes.

        :return: Whether the source must be selected again on the player.
        """
        needs_reselect = self.stop_requested() or self.externally_paused
        self.stop_reason = None
        self.externally_paused = False
        return needs_reselect

    def pause_externally(self, owner_player_id: str) -> None:
        """
        Record a peer pause that stopped the player; resume must reselect the source.

        :param owner_player_id: Owner that was stopped; resume plays on its queue.
        """
        self.consumer_player_id = owner_player_id
        self.externally_paused = True

    def follow_window_trim(self, track_id: str | None, index: int) -> None:
        """
        Move the cursor once Ynison reports the playing item at its trimmed index.

        :param track_id: Current Ynison track.
        :param index: Current Ynison playable index.
        """
        if not self.pending_index_shift or track_id != self.track_id:
            return
        if index == self.index - self.pending_index_shift:
            self.index = index
            self.pending_index_shift = 0
        elif index != self.index:
            self.pending_index_shift = 0

    def reset(self) -> None:
        """Drop the claim and progress and stop the stream."""
        self.owner_player_id = None
        self.consumer_player_id = None
        self.session_id = None
        self.progress_ms = 0
        self.externally_paused = False
        self.stop_reason = StopReason.CLEARED
