"""
Best-effort track filter for dynamic-playlist generation.

A consumer that drives dynamic-playlist generation (the player queue, refilling from a dynamic
source) can publish an include-predicate through a context variable. Dynamic-playlist generators
(the radio_playlist provider, the dynamic radio helper) may consult it to skip tracks the consumer
would discard anyway -- recently played or already queued -- saving wasted similar-track lookups.

It is advisory: a generator may ignore the predicate, so the consumer stays authoritative. When the
generator did consult it (``TrackFilter.called``), the consumer can skip its own redundant pass.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from music_assistant_models.media_items import Track


@dataclass(slots=True)
class TrackFilter:
    """A best-effort include-predicate published to dynamic-playlist generators."""

    include: Callable[[Track], bool]
    called: bool = False

    def allows(self, track: Track) -> bool:
        """
        Return True if the track should be kept, recording that the filter was consulted.

        :param track: The candidate track to test.
        """
        self.called = True
        return self.include(track)


_CURRENT_TRACK_FILTER: ContextVar[TrackFilter | None] = ContextVar(
    "dynamic_playlist_track_filter", default=None
)


def get_track_filter() -> TrackFilter | None:
    """Return the track filter published for the current async context, if any."""
    return _CURRENT_TRACK_FILTER.get()


@contextmanager
def track_filter(include: Callable[[Track], bool]) -> Iterator[TrackFilter]:
    """
    Publish a best-effort include-predicate to dynamic-playlist generators within the block.

    :param include: Predicate returning True for tracks that should be kept.
    """
    active_filter = TrackFilter(include)
    token = _CURRENT_TRACK_FILTER.set(active_filter)
    try:
        yield active_filter
    finally:
        _CURRENT_TRACK_FILTER.reset(token)
