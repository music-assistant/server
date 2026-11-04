"""Diagnostics of Apple Music live radio sessions, to explain why a stream stopped."""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Any

from music_assistant.helpers.datetime import utc

if TYPE_CHECKING:
    from music_assistant.helpers.json import SerializableType

MAX_EVENTS = 100
MAX_SESSIONS = 5


@dataclass(slots=True)
class LiveSession:
    """Counters of one run of a live stream, from start until it ended."""

    session: int
    station_id: str
    started_at: str
    substream: str = ""
    key_id: str = ""
    segments_sent: int = 0
    audio_seconds_sent: float = 0.0
    playlist_errors: int = 0
    segment_errors: int = 0
    segments_skipped: int = 0
    key_changes: int = 0
    license_renewals: int = 0
    license_errors: int = 0
    max_segment_fetch_ms: int = 0
    max_consumer_wait_s: float = 0.0
    behind_live_s: float | None = None
    stream_title: str | None = None
    ended_at: str | None = None
    end_reason: str | None = None


class LiveRadioMonitor:
    """Keep what a live stream does for the logs and the provider diagnostics."""

    def __init__(self, logger: logging.Logger) -> None:
        """
        Initialize the monitor.

        :param logger: Logger of the provider.
        """
        self._logger = logger
        self._events: deque[dict[str, Any]] = deque(maxlen=MAX_EVENTS)
        self._sessions: deque[LiveSession] = deque(maxlen=MAX_SESSIONS)
        self._counter = 0
        self.current: LiveSession | None = None

    def start_session(self, station_id: str) -> LiveSession:
        """
        Start the counters of a new run.

        :param station_id: The Apple Music station id.
        """
        self._counter += 1
        self.current = LiveSession(
            session=self._counter,
            station_id=station_id,
            started_at=utc().isoformat(timespec="seconds"),
        )
        self._sessions.append(self.current)
        self.event("session_start", station=station_id)
        return self.current

    def end_session(self, reason: str) -> None:
        """
        Record why the current run ended.

        :param reason: consumer_closed, cancelled, or the error that ended it.
        """
        if (session := self.current) is None:
            return
        session.ended_at = utc().isoformat(timespec="seconds")
        session.end_reason = reason
        self.event("session_end", reason=reason, segments=session.segments_sent)
        self.current = None

    def event(self, name: str, *, level: int = logging.DEBUG, **fields: Any) -> None:
        """
        Record one event.

        :param name: The event name.
        :param level: Log level of the line in the regular log.
        :param fields: The details; never pass tokens, keys or signed urls.
        """
        session = self.current.session if self.current else self._counter
        self._events.append(
            {
                "ts": utc().isoformat(timespec="milliseconds"),
                "session": session,
                "event": name,
                **fields,
            }
        )
        details = " ".join(f"{key}={value}" for key, value in fields.items())
        self._logger.log(level, "Live radio [%s] %s %s", session, name, details)

    def as_dict(self) -> dict[str, SerializableType]:
        """Return the sessions and recent events for the diagnostics."""
        return {
            "current_session": self.current.session if self.current else None,
            "sessions": [asdict(session) for session in self._sessions],
            "recent_events": list(self._events),
        }
