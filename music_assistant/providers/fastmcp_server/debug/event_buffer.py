"""
Bounded in-memory ring buffer of recent MA events.

Activated only when ``DEBUG_EVENTS`` is enabled. Subscribes via
``mass.subscribe(...)`` at :meth:`start` and unsubscribes at :meth:`stop`.
The callback is synchronous (a O(1) ``deque.append``) so it can never
back-pressure MA's event loop.

Restarts and reloads reset the buffer — events are not persisted across
provider lifecycles. See spec 0005 "Deliberately deferred" for the
rationale.
"""
# ruff: noqa: TID252  -- relative imports are the canonical MA-provider pattern.

from __future__ import annotations

from collections import Counter, deque
from datetime import datetime
from typing import TYPE_CHECKING, Any

from ..models import EventBufferStats, EventRecord
from .inspect_serializer import dump

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant


def _now() -> datetime:
    """
    Indirection for tests.

    The project does not ship ``freezegun`` and ``pyproject.toml`` is
    template-generated (cannot be hand-edited). Tests replace this
    module-level callable to control timestamps deterministically.
    """
    # Deferred: a top-level music_assistant import would pull the full package
    # init (and optional deps) at module load; here it runs inside the host.
    from music_assistant.helpers.datetime import now as ma_now  # noqa: PLC0415

    return ma_now()


class EventBuffer:
    """Ring deque + lifecycle around ``mass.subscribe``."""

    def __init__(self, mass: MusicAssistant, *, capacity: int) -> None:
        """
        Initialize the event buffer.

        :param mass: Music Assistant instance to subscribe to.
        :param capacity: Maximum events to retain (clamped to [50, 5000]).
        """
        self._mass = mass
        self._capacity = max(50, min(int(capacity), 5000))
        self._queue: deque[EventRecord] = deque(maxlen=self._capacity)
        self._counts: Counter[str] = Counter()
        self._total_seen = 0
        self._subscribed_since: datetime | None = None
        self._remove: Any = None  # callable returned by mass.subscribe

    def start(self) -> None:
        """Subscribe to the event bus. Idempotent — second call is a no-op."""
        if self._remove is not None:
            return
        self._remove = self._mass.subscribe(self._on_event)
        self._subscribed_since = _now()

    def stop(self) -> None:
        """Unsubscribe. Idempotent."""
        if self._remove is None:
            return
        try:
            self._remove()
        finally:
            self._remove = None
            self._subscribed_since = None

    def snapshot(
        self,
        *,
        limit: int,
        event_types: list[str] | None = None,
        id_filter: str | None = None,
        since_seconds: int | None = None,
    ) -> list[EventRecord]:
        """Return a filtered copy of the buffer, newest last."""
        limit = max(1, min(int(limit), 1000))
        now = _now()
        type_set = set(event_types) if event_types else None
        results: list[EventRecord] = []
        for record in self._queue:
            if type_set and record.event_type not in type_set:
                continue
            if id_filter and record.object_id != id_filter:
                continue
            if since_seconds is not None:
                try:
                    when = datetime.fromisoformat(record.timestamp)
                except ValueError:
                    continue
                if (now - when).total_seconds() > since_seconds:
                    continue
            results.append(record)
        return results[-limit:]

    def stats(self) -> EventBufferStats:
        """Return introspection counters."""
        return EventBufferStats(
            capacity=self._capacity,
            current_size=len(self._queue),
            total_seen=self._total_seen,
            dropped=max(0, self._total_seen - self._capacity),
            subscribed_since=self._subscribed_since.isoformat() if self._subscribed_since else None,
            by_type=dict(self._counts),
        )

    def _on_event(self, event: Any) -> None:
        event_type = str(getattr(event, "event", getattr(event, "event_type", "")))
        object_id = getattr(event, "object_id", None)
        record = EventRecord(
            timestamp=_now().isoformat(),
            event_type=event_type,
            object_id=str(object_id) if object_id is not None else None,
            data=dump(getattr(event, "data", None), max_depth=4, max_str=1024),
        )
        self._queue.append(record)
        self._counts[event_type] += 1
        self._total_seen += 1
