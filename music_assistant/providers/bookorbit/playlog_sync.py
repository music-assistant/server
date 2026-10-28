"""Apply BookOrbit audiobook progress into the Music Assistant playlog."""

from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass
class InboundProgressGuard:
    """
    Suppress inbound playlog updates shortly after MA reported progress outbound.

    Mirrors Audiobookshelf ProgressGuard cool-down so a poll cannot stomp a
    fresher MA→BookOrbit write that has not yet been reflected remotely.
    """

    min_interval_ms: int = 8_000
    _last_outbound_ms: dict[int, int] = field(default_factory=dict)

    def note_outbound(self, book_id: int) -> None:
        """Record that MA just wrote progress for ``book_id`` to BookOrbit."""
        self._last_outbound_ms[book_id] = self._now_ms()

    def allow_inbound(self, book_id: int) -> bool:
        """Return True when an inbound playlog apply for ``book_id`` is safe."""
        stamped = self._last_outbound_ms.get(book_id)
        if stamped is None:
            return True
        return self._now_ms() - stamped >= self.min_interval_ms

    def _now_ms(self) -> int:
        return int(time.time() * 1000)
