"""Resuming Music Assistant playback that an Alice request stopped or delayed."""

from __future__ import annotations

from enum import StrEnum


class VoiceRecoveryPhase(StrEnum):
    """Where the decision about interrupted playback stands."""

    NONE = "none"
    # An Alice request stopped or delayed the playback; its end decides.
    AWAITING_ANSWER = "awaiting_answer"
    # Alice answered; the resume runs after a short delay.
    SCHEDULED = "scheduled"


class VoiceRecovery:
    """
    Follow interrupted playback from Alice's request to its resumption.

    The player schedules the timer and asks the core to resume; this tracks the
    decision, the queue that owned the playback, the volume the deciding request
    started from, and whether a resume the core is already running still stands.
    A running resume cannot be recalled from the core, so anything that overrules
    it marks it superseded and the player drops the stream it asks for.
    """

    def __init__(self) -> None:
        """Start with nothing to recover."""
        self._phase = VoiceRecoveryPhase.NONE
        self._source: str | None = None
        self._baseline_volume = 0
        self._resuming = False
        self._superseded = False

    @property
    def phase(self) -> VoiceRecoveryPhase:
        """Return where the decision stands."""
        return self._phase

    @property
    def source(self) -> str | None:
        """Return the queue that owned the interrupted playback."""
        return self._source

    @property
    def holds_playback(self) -> bool:
        """Whether interrupted playback waits for Alice's request or the scheduled resume."""
        return self._phase != VoiceRecoveryPhase.NONE

    @property
    def resuming(self) -> bool:
        """Whether the core is running a resume this recovery asked for."""
        return self._resuming

    @property
    def drops_resumed_stream(self) -> bool:
        """Whether the stream the running resume starts must not play."""
        return self._resuming and self._superseded

    def begin(self, source: str, volume: int) -> None:
        """
        Start waiting for the end of the Alice request that interrupted playback.

        :param source: The queue that owned the playback.
        :param volume: The Station volume when the request started.
        """
        self._phase = VoiceRecoveryPhase.AWAITING_ANSWER
        self._source = source
        self._baseline_volume = volume

    def request_ended(self, *, spoke: bool, volume: int) -> bool:
        """
        Decide at the end of the request whether playback resumes.

        A request Alice answered, or one that changed the volume, paused the music
        only for a moment; a silent one ("Alice, stop") leaves it stopped.
        Returns True if a resume has to be scheduled.

        :param spoke: Whether Alice answered the request.
        :param volume: The Station volume at the end of the request.
        """
        if self._phase != VoiceRecoveryPhase.AWAITING_ANSWER:
            return False
        if spoke or volume != self._baseline_volume:
            self._phase = VoiceRecoveryPhase.SCHEDULED
            return True
        self._phase = VoiceRecoveryPhase.NONE
        return False

    def request_started(self, volume: int) -> bool:
        """
        Let an Alice request that just started decide instead of an earlier one.

        It takes over a scheduled resume, and it overrules a resume the core is
        already running. Its own end decides, measured from the volume it started
        at. Returns True if a scheduled resume was taken over (its timer must go).

        :param volume: The Station volume when the new request started.
        """
        took_over_timer = self._phase == VoiceRecoveryPhase.SCHEDULED
        if not (took_over_timer or self._resuming):
            return False
        if self._resuming:
            self._superseded = True
        self._phase = VoiceRecoveryPhase.AWAITING_ANSWER
        self._baseline_volume = volume
        return took_over_timer

    def start_resume(self) -> str | None:
        """
        Mark the scheduled resume as running and return the queue to resume.

        Returns None if nothing is scheduled any more, or if a superseded resume
        is still running: that one is allowed to play again instead.
        """
        if self._phase != VoiceRecoveryPhase.SCHEDULED:
            return None
        self._phase = VoiceRecoveryPhase.NONE
        if self._resuming:
            self._superseded = False
            return None
        self._resuming = True
        self._superseded = False
        return self._source

    def finish_resume(self) -> None:
        """Note that the resume the core ran has returned."""
        self._resuming = False
        self._superseded = False

    def cancel(self) -> None:
        """Drop the recovery because something else now decides playback."""
        if self._resuming:
            self._superseded = True
        self._phase = VoiceRecoveryPhase.NONE
