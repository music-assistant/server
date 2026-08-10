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

# a fragment nothing has streamed from for this long has audio URLs that Pandora's own
# clock has likely expired; refetch rather than serve them
FRAGMENT_STALE_SECONDS = 600

# how many fragments of (metadata-only) track data to keep so recently played tracks
# stay resolvable for queue history
MAX_RETAINED_FRAGMENTS = 4


@dataclass
class PandoraFragment:
    """One Pandora playlist fragment: the tracks whose audio URLs are live together."""

    tracks: list[dict[str, Any]]
    last_activity_at: float
    spent: bool = False
    served: set[str] = field(default_factory=set)

    def find(self, music_id: str) -> dict[str, Any] | None:
        """Return the raw track data for the given Pandora musicId, if this fragment holds it."""
        return next((track for track in self.tracks if track.get("musicId") == music_id), None)

    def mark_resolved(self, music_id: str, now: float) -> None:
        """Record that the given track has been handed to the audio pipeline."""
        if self.find(music_id) is None:
            return
        self.last_activity_at = now
        self.served.add(music_id)
        if self.tracks[-1].get("musicId") == music_id:
            self.spent = True

    def is_stale(self, now: float) -> bool:
        """Return whether nothing has been streamed from this fragment recently."""
        return (now - self.last_activity_at) > FRAGMENT_STALE_SECONDS

    @property
    def pending(self) -> list[dict[str, Any]]:
        """
        Return the tracks not yet handed to the audio pipeline, in the fragment's original order.

        Invariant: serving a fragment's last track sets `spent` (see `mark_resolved`), so a
        fragment left with nothing pending is always already spent - `should_fetch_fragment`
        will have returned True and a new fragment will already have replaced it as the
        session's `current` one. A live fragment's `pending` therefore never comes back empty,
        which matters because `get_playlist_tracks` treats an empty list as "this station has
        ended".
        """
        return [track for track in self.tracks if track.get("musicId") not in self.served]


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
        fragment = PandoraFragment(tracks=tracks, last_activity_at=now)
        self.fragments.append(fragment)
        return fragment

    def find_track(self, music_id: str) -> dict[str, Any] | None:
        """Return raw track data from any retained fragment, newest first."""
        for fragment in reversed(self.fragments):
            if (track := fragment.find(music_id)) is not None:
                return track
        return None


def should_fetch_fragment(fragment: PandoraFragment | None, now: float) -> bool:
    """
    Return whether a station needs a new fragment fetched from Pandora.

    False means the current fragment's audio URLs are still fresh and should keep being served.
    It never means "this station has no tracks".

    :param fragment: The station's current (newest) fragment, or None if it has none yet.
    :param now: Current wall-clock time, used for the staleness check.
    """
    return fragment is None or fragment.spent or fragment.is_stale(now)
