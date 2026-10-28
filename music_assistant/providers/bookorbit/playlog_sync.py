"""Apply BookOrbit audiobook progress into the Music Assistant playlog."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from music_assistant.providers.bookorbit.helpers import book_with_manifest_playback_order
from music_assistant.providers.bookorbit.parsers import _audio_files
from music_assistant.providers.bookorbit.progress import (
    parse_resume_position,
    parse_resume_position_from_playback_state,
)


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


async def resolve_remote_progress_seconds(client: Any, book_id: int) -> tuple[bool, int] | None:
    """
    Resolve BookOrbit progress to (fully_played, seconds_played).

    :returns: Tuple when usable progress exists; ``None`` when absent or unusable.
    May raise transport errors from the client; conversion failures return ``None``.
    """
    try:
        manifest = await client.get_audiobook_manifest(book_id)
        state = await client.get_playback_state(book_id)
        if state:
            fully_played, position_ms, _ = parse_resume_position_from_playback_state(
                manifest, state
            )
            return fully_played, position_ms // 1000
        book = await client.get_book(book_id)
        book = book_with_manifest_playback_order(book, manifest)
        progress = await client.get_audio_progress(book_id)
        if not progress:
            return None
        fully_played, position_ms, _ = parse_resume_position(_audio_files(book), progress)
        return fully_played, position_ms // 1000
    except TypeError, ValueError, KeyError:
        return None
