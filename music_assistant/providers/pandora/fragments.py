"""
Fragment retention and playback gating for the Pandora provider.

Pandora serves its stations in fragments of ~4 tracks whose audio URLs are time-limited: they
are signed CDN links that expire, so a fragment is worth serving only while it is fresh. The
provider therefore fetches a new fragment once the current one has been played through or has
gone stale, and keeps serving the current one in between.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Any

# A fragment nobody has streamed from for this long has been abandoned - playback stopped, or
# the station was never played. Fetch a fresh one rather than carry it forward. No track runs
# this long, so silence for this window cannot mean "still playing".
FRAGMENT_STALE_SECONDS = 600

# How long Pandora's signed audio URLs stay usable, measured from when the fragment was fetched.
# Their docs put radio URLs at "up to an hour"; this leaves margin under that. Kept separate
# from the staleness window above because the two answer different questions: that one is
# "is anyone still listening", this one is "do these links still work".
FRAGMENT_URL_TTL_SECONDS = 2700

# how many fragments of (metadata-only) track data to keep so recently played tracks
# stay resolvable for queue history
MAX_RETAINED_FRAGMENTS = 4

# how many stations can hold a session at once; the LRU evicts the least recently accessed
# one past this
MAX_ACTIVE_SESSIONS = 10


@dataclass
class PandoraFragment:
    """One Pandora playlist fragment: the tracks whose audio URLs are live together."""

    tracks: list[dict[str, Any]]
    fetched_at: float
    last_activity_at: float
    spent: bool = False
    served: set[str] = field(default_factory=set)

    def find(self, pandora_id: str) -> dict[str, Any] | None:
        """Return the raw track data for the given Pandora id, if this fragment holds it."""
        return next((track for track in self.tracks if track.get("pandoraId") == pandora_id), None)

    def mark_resolved(self, pandora_id: str, now: float) -> None:
        """Record that the given track has been handed to the audio pipeline."""
        if self.find(pandora_id) is None:
            return
        self.last_activity_at = now
        self.served.add(pandora_id)
        if self.tracks[-1].get("pandoraId") == pandora_id:
            self.spent = True

    def is_stale(self, now: float) -> bool:
        """Return whether nothing has been streamed from this fragment recently."""
        return (now - self.last_activity_at) > FRAGMENT_STALE_SECONDS

    def urls_expired(self, now: float) -> bool:
        """Return whether this fragment has outlived the life of its signed audio URLs."""
        return (now - self.fetched_at) > FRAGMENT_URL_TTL_SECONDS

    @property
    def pending(self) -> list[dict[str, Any]]:
        """
        Return the tracks not yet handed to the audio pipeline, in the fragment's original order.

        Never empty for a live fragment.
        """
        # Serving a fragment's last track sets `spent` (see `mark_resolved`), so a fragment left
        # with nothing pending is always already spent - `should_fetch_fragment` will have
        # returned True and a new fragment will already have replaced it as the session's
        # `current` one.
        return [track for track in self.tracks if track.get("pandoraId") not in self.served]


@dataclass
class PandoraStationSession:
    """Fragment state for a single Pandora station."""

    station_id: str
    fragments: deque[PandoraFragment] = field(
        default_factory=lambda: deque(maxlen=MAX_RETAINED_FRAGMENTS)
    )
    last_accessed: float = 0.0

    @property
    def current(self) -> PandoraFragment | None:
        """Return the newest fragment: an older one's signed URL might already have expired."""
        return self.fragments[-1] if self.fragments else None

    def add_fragment(self, tracks: list[dict[str, Any]], now: float) -> PandoraFragment:
        """Retain a freshly fetched fragment as the station's live one."""
        fragment = PandoraFragment(tracks=tracks, fetched_at=now, last_activity_at=now)
        self.fragments.append(fragment)
        return fragment


def should_fetch_fragment(fragment: PandoraFragment | None, now: float) -> bool:
    """
    Return whether a station needs a new fragment fetched from Pandora.

    False means the current fragment's audio URLs are still fresh and should keep being served.
    It never means "this station has no tracks".

    :param fragment: The station's current (newest) fragment, or None if it has none yet.
    :param now: Current wall-clock time, used for the staleness and expiry checks.
    """
    return (
        fragment is None or fragment.spent or fragment.is_stale(now) or fragment.urls_expired(now)
    )
