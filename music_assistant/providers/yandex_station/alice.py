"""Alice activity on a Yandex Station, derived from the aliceState it reports."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

# aliceState values that belong to a spoken request.
_REQUEST_STATES = frozenset({"LISTENING", "BUSY", "SPEAKING"})
# aliceState while Alice speaks: the answer to a request, or a server-action phrase.
_SPEAKING_STATES = frozenset({"SPEAKING", "NONE"})


class AliceActivity(StrEnum):
    """What Alice is doing on the Station."""

    IDLE = "idle"
    # A request spoken with the wake word, up to the end of Alice's answer.
    REQUEST = "request"
    # A phrase spoken through a server action (repeat_phrase), with music only ducked.
    PHRASE = "phrase"


@dataclass(frozen=True, slots=True)
class AliceUpdate:
    """Alice's activity after one Station report."""

    activity: AliceActivity
    # The activity began with this report.
    started: bool
    # The activity that ended with this report, if any.
    ended: AliceActivity | None
    # Alice answered the current (or just ended) request.
    spoke: bool


class AliceTracker:
    """
    Follow Alice's activity through the aliceState values a Station reports.

    A request goes LISTENING, BUSY, NONE while Alice answers (SPEAKING on older
    firmware), LISTENING, then IDLE. NONE from IDLE is a server-action phrase.
    Commands recognized without the wake word never leave IDLE.
    """

    def __init__(self) -> None:
        """Start with Alice idle."""
        self._activity = AliceActivity.IDLE
        self._spoke = False

    @property
    def activity(self) -> AliceActivity:
        """Return what Alice is doing now."""
        return self._activity

    def feed(self, alice_state: str) -> AliceUpdate:
        """
        Advance with the next reported aliceState.

        :param alice_state: The aliceState value of the report.
        """
        previous = self._activity
        if alice_state in _REQUEST_STATES or (
            alice_state == "NONE" and previous == AliceActivity.REQUEST
        ):
            current = AliceActivity.REQUEST
        elif alice_state == "NONE":
            current = AliceActivity.PHRASE
        else:
            current = AliceActivity.IDLE

        changed = current != previous
        started = changed and current != AliceActivity.IDLE
        if started:
            self._spoke = False
        if current == AliceActivity.REQUEST and alice_state in _SPEAKING_STATES:
            self._spoke = True
        self._activity = current
        return AliceUpdate(
            activity=current,
            started=started,
            ended=previous if changed and previous != AliceActivity.IDLE else None,
            spoke=self._spoke,
        )
