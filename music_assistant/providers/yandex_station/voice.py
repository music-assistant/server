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


class RunningResume(StrEnum):
    """What may still come of a resume the core is already running."""

    NONE = "none"
    # Its stream may play.
    ACTIVE = "active"
    # A new Alice request overruled it; the request's end decides.
    HELD = "held"
    # Something else took playback over; its stream never plays.
    CANCELLED = "cancelled"


class VoiceRecovery:
    """
    Follow interrupted playback from Alice's request to its resumption.

    The player schedules the timer and asks the core to resume; this tracks the
    decision, the queue that owned the playback, the volume the deciding request
    started from, and what may still come of a resume the core is already running.
    A running resume cannot be recalled from the core, so whatever overrules it
    makes the player drop the stream it asks for. A resume that has to follow a
    dropped stream waits until the running one has returned.
    """

    def __init__(self) -> None:
        """Start with nothing to recover."""
        self._phase = VoiceRecoveryPhase.NONE
        self._source: str | None = None
        self._baseline_volume = 0
        self._running = RunningResume.NONE
        self._stream_dropped = False

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
        return self._running != RunningResume.NONE

    @property
    def awaits_resumed_stream(self) -> bool:
        """Whether a running resume may still bring the interrupted playback back."""
        return self._running in (RunningResume.ACTIVE, RunningResume.HELD)

    @property
    def drops_resumed_stream(self) -> bool:
        """Whether the stream the running resume starts must not play."""
        return self._running in (RunningResume.HELD, RunningResume.CANCELLED)

    def begin(self, source: str, volume: int) -> None:
        """
        Start waiting for the end of the Alice request that interrupted playback.

        :param source: The queue that owned the playback.
        :param volume: The Station volume when the request started.
        """
        self._phase = VoiceRecoveryPhase.AWAITING_ANSWER
        self._source = source
        self._baseline_volume = volume
        if self._running != RunningResume.NONE:
            # The stream the running resume asked for was stopped or held back.
            self._stream_dropped = True
            if self._running == RunningResume.ACTIVE:
                self._running = RunningResume.HELD

    def request_ended(self, *, spoke: bool, volume: int) -> bool:
        """
        Decide at the end of the request whether playback resumes.

        A request Alice answered, or one that changed the volume, paused the music
        only for a moment; a silent one ("Alice, stop") leaves it stopped. When the
        core is already running a resume, that one is allowed to play again instead
        of scheduling another: a second resume would replace the running one. If
        its stream was already dropped, the resume is scheduled once it returns
        (see `finish_resume`). Returns True if a resume has to be scheduled now.

        :param spoke: Whether Alice answered the request.
        :param volume: The Station volume at the end of the request.
        """
        if self._phase != VoiceRecoveryPhase.AWAITING_ANSWER:
            return False
        if not (spoke or volume != self._baseline_volume):
            self._phase = VoiceRecoveryPhase.NONE
            return False
        if self._running == RunningResume.NONE:
            self._phase = VoiceRecoveryPhase.SCHEDULED
            return True
        if self._stream_dropped:
            self._phase = VoiceRecoveryPhase.SCHEDULED
            return False
        self._running = RunningResume.ACTIVE
        self._phase = VoiceRecoveryPhase.NONE
        return False

    def request_started(self, volume: int) -> bool:
        """
        Let an Alice request that just started decide instead of an earlier one.

        It takes over a scheduled resume, and it overrules a resume the core is
        already running unless something else took playback from that one. Its own
        end decides, measured from the volume it started at. Returns True if a
        scheduled resume was taken over (its timer must go).

        :param volume: The Station volume when the new request started.
        """
        took_over_timer = self._phase == VoiceRecoveryPhase.SCHEDULED
        if self._running == RunningResume.ACTIVE:
            self._running = RunningResume.HELD
        elif not (took_over_timer or self._running == RunningResume.HELD):
            return False
        self._phase = VoiceRecoveryPhase.AWAITING_ANSWER
        self._baseline_volume = volume
        return took_over_timer

    def start_resume(self) -> str | None:
        """
        Mark the scheduled resume as running and return the queue to resume.

        Returns None if nothing is scheduled any more.
        """
        if self._phase != VoiceRecoveryPhase.SCHEDULED:
            return None
        self._phase = VoiceRecoveryPhase.NONE
        self._running = RunningResume.ACTIVE
        self._stream_dropped = False
        return self._source

    def drop_resumed_stream(self) -> None:
        """Note that the player dropped the stream the running resume asked for."""
        self._stream_dropped = True

    def finish_resume(self) -> bool:
        """
        Note that the resume the core ran has returned.

        Returns True if a resume has to be scheduled now: a request that ended while
        it ran, after its stream was dropped, asked for one.
        """
        self._running = RunningResume.NONE
        self._stream_dropped = False
        return self._phase == VoiceRecoveryPhase.SCHEDULED

    def cancel(self) -> None:
        """Drop the recovery because something else now decides playback."""
        if self._running != RunningResume.NONE:
            self._running = RunningResume.CANCELLED
        self._phase = VoiceRecoveryPhase.NONE
