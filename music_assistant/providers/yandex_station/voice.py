"""Resuming Music Assistant playback that an Alice request stopped or delayed."""

from __future__ import annotations

from enum import StrEnum


class VoiceRecoveryPhase(StrEnum):
    """Where the recovery of interrupted playback stands."""

    NONE = "none"
    # An Alice request stopped or delayed the playback; its end decides.
    AWAITING_ANSWER = "awaiting_answer"
    # Alice answered; the resume runs after a short delay.
    SCHEDULED = "scheduled"
    # The resume asked the core to play the owning queue again.
    RESUMING = "resuming"


class VoiceRecovery:
    """
    Follow one interrupted playback from Alice's request to its resumption.

    The player schedules and runs the resume; this tracks which step it is at, the
    queue that owned the playback, and the volume the request started from.
    """

    def __init__(self) -> None:
        """Start with nothing to recover."""
        self._phase = VoiceRecoveryPhase.NONE
        self._source: str | None = None
        self._baseline_volume = 0
        self._superseded = False

    @property
    def phase(self) -> VoiceRecoveryPhase:
        """Return the current step."""
        return self._phase

    @property
    def source(self) -> str | None:
        """Return the queue that owned the interrupted playback."""
        return self._source

    @property
    def holds_playback(self) -> bool:
        """Whether interrupted playback waits for Alice's request or the scheduled resume."""
        return self._phase in (VoiceRecoveryPhase.AWAITING_ANSWER, VoiceRecoveryPhase.SCHEDULED)

    @property
    def drops_resumed_stream(self) -> bool:
        """Whether the stream the running resume starts must not play: it was superseded."""
        return self._phase == VoiceRecoveryPhase.RESUMING and self._superseded

    def begin(self, source: str, volume: int) -> None:
        """
        Start waiting for the end of the Alice request that interrupted playback.

        :param source: The queue that owned the playback.
        :param volume: The Station volume when the request started.
        """
        self._phase = VoiceRecoveryPhase.AWAITING_ANSWER
        self._source = source
        self._baseline_volume = volume
        self._superseded = False

    def request_ended(self, *, spoke: bool, volume: int) -> bool:
        """
        Decide at the end of the request whether playback resumes.

        A request Alice answered, or one that changed the volume, paused the music
        only for a moment; a silent one ("Alice, stop") leaves it stopped.
        Returns True if the resume is now scheduled.

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
        Hand a scheduled resume over to an Alice request that just started.

        The new request's own end decides, measured from the volume it started at.
        Returns True if a scheduled resume was taken over (its timer must go).

        :param volume: The Station volume when the new request started.
        """
        if self._phase != VoiceRecoveryPhase.SCHEDULED:
            return False
        self._phase = VoiceRecoveryPhase.AWAITING_ANSWER
        self._baseline_volume = volume
        return True

    def start_resume(self) -> str | None:
        """
        Mark the scheduled resume as running and return the queue to resume.

        Returns None if nothing is scheduled any more.
        """
        if self._phase != VoiceRecoveryPhase.SCHEDULED:
            return None
        self._phase = VoiceRecoveryPhase.RESUMING
        self._superseded = False
        return self._source

    def finish_resume(self) -> None:
        """Forget the recovery once the resume has run."""
        if self._phase == VoiceRecoveryPhase.RESUMING:
            self._reset()

    def cancel(self) -> None:
        """
        Drop the recovery because something else now decides playback.

        A resume that is already running cannot be recalled from the core, so it is
        marked superseded and the stream it starts is dropped.
        """
        if self._phase == VoiceRecoveryPhase.RESUMING:
            self._superseded = True
            return
        self._reset()

    def _reset(self) -> None:
        self._phase = VoiceRecoveryPhase.NONE
        self._source = None
        self._superseded = False
