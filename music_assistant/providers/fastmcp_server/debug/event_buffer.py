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

from ..dynamic_serialization import json_value
from ..models import EventBufferStats, EventRecord

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

    # MA's source is fully typed when transplanted under its package; standalone
    # provider checks see the external package as untyped.
    return ma_now()  # type: ignore[no-any-return, unused-ignore]


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
        event_type = _event_type(event)
        object_id = _event_object_id(event)
        record = EventRecord(
            timestamp=_now().isoformat(),
            event_type=event_type,
            object_id=object_id,
            data=_event_data(event),
        )
        self._queue.append(record)
        self._counts[event_type] += 1
        self._total_seen += 1


def _bounded_event_data(value: Any) -> Any:
    """Convert event payloads to compact JSON values without retaining MA objects."""
    try:
        return _bound_json(json_value(value), depth=0)
    except Exception:
        return "<unserializable event data>"


def _event_type(event: Any) -> str:
    """Read the primary or legacy event type without escaping user-defined accessors."""
    value = _safe_attr(event, "event", None)
    if value is None:
        value = _safe_attr(event, "event_type", "<unavailable>")
    return _safe_text(value)


def _event_object_id(event: Any) -> str | None:
    """Read an optional object id without trusting its string conversion."""
    value = _safe_attr(event, "object_id", None)
    return None if value is None else _safe_text(value)


def _event_data(event: Any) -> Any:
    """Read and serialize event data behind one synchronous callback boundary."""
    try:
        value = event.data
    except AttributeError:
        value = None
    except Exception:
        return "<unserializable event data>"
    return _bounded_event_data(value)


def _safe_attr(value: Any, name: str, default: Any) -> Any:
    """Read one event attribute while isolating custom descriptors."""
    try:
        return getattr(value, name, default)
    except Exception:
        return default


def _safe_text(value: Any) -> str:
    """Convert event metadata to a bounded string without invoking a failing repr."""
    try:
        text = str(value)
    except Exception:
        return "<unavailable>"
    return text if len(text) <= 1024 else f"{text[:1024]}…({len(text) - 1024} more chars)"


def _bound_json(value: Any, *, depth: int) -> Any:
    """Bound event depth, string length, and collection width for the ring buffer."""
    if depth >= 4:
        return "<max depth>"
    if isinstance(value, str):
        return value if len(value) <= 1024 else f"{value[:1024]}…({len(value) - 1024} more chars)"
    if isinstance(value, list):
        rows = [_bound_json(item, depth=depth + 1) for item in value[:100]]
        if len(value) > 100:
            rows.append(f"<{len(value) - 100} more items>")
        return rows
    if isinstance(value, dict):
        rows = list(value.items())
        result = {key: _bound_json(item, depth=depth + 1) for key, item in rows[:100]}
        if len(rows) > 100:
            result["<truncated>"] = f"{len(rows) - 100} more fields"
        return result
    return value
