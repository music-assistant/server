"""
Fragment retention and playback gating for the Pandora provider.

Pandora serves its stations in fragments of ~4 tracks. Requesting a new fragment invalidates the
previous fragment's audio URLs account-wide, so the provider may only advance once every URL it
handed out has already reached the audio pipeline.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

# an untouched fragment's audio URLs expire on Pandora's own clock; refetch rather than
# serve URLs that a browse fetched and nobody ever played
FRAGMENT_STALE_SECONDS = 600

# how many fragments of (metadata-only) track data to keep so recently played tracks
# stay resolvable for queue history
MAX_RETAINED_FRAGMENTS = 4


class FragmentAction(StrEnum):
    """What a playlist-tracks request should do with a station's current fragment."""

    FETCH = "fetch"
    REUSE = "reuse"
    WITHHOLD = "withhold"


@dataclass
class PandoraFragment:
    """One Pandora playlist fragment: the tracks whose audio URLs are live together."""

    tracks: list[dict[str, Any]]
    fetched_at: float
    resolved: bool = False
    spent: bool = False

    def find(self, music_id: str) -> dict[str, Any] | None:
        """Return the raw track data for the given Pandora musicId, if this fragment holds it."""
        return next((track for track in self.tracks if track.get("musicId") == music_id), None)

    def mark_resolved(self, music_id: str) -> None:
        """Record that the given track has been handed to the audio pipeline."""
        if self.find(music_id) is None:
            return
        self.resolved = True
        if self.tracks and self.tracks[-1].get("musicId") == music_id:
            self.spent = True

    def is_stale(self, now: float) -> bool:
        """Return whether this fragment was never streamed from and has aged out."""
        return not self.resolved and (now - self.fetched_at) > FRAGMENT_STALE_SECONDS


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
        """Return the newest fragment: the only one whose audio URLs are still live."""
        return self.fragments[-1] if self.fragments else None

    def add_fragment(self, tracks: list[dict[str, Any]], now: float) -> PandoraFragment:
        """Retain a freshly fetched fragment as the station's live one."""
        fragment = PandoraFragment(tracks=tracks, fetched_at=now)
        self.fragments.append(fragment)
        return fragment

    def find_track(self, music_id: str) -> dict[str, Any] | None:
        """Return raw track data from any retained fragment, newest first."""
        for fragment in reversed(self.fragments):
            if (track := fragment.find(music_id)) is not None:
                return track
        return None


def next_fragment_action(fragment: PandoraFragment | None, now: float) -> FragmentAction:
    """
    Decide how to answer a playlist-tracks request for a station.

    :param fragment: The station's current (newest) fragment, or None if it has none yet.
    :param now: Current wall-clock time, used for the staleness check.
    """
    if fragment is None:
        return FragmentAction.FETCH
    if not fragment.resolved:
        # nobody is streaming from it: safe either to hand out again or to replace
        return FragmentAction.FETCH if fragment.is_stale(now) else FragmentAction.REUSE
    if not fragment.spent:
        # URLs are live and pending playback; fetching now would invalidate them
        return FragmentAction.WITHHOLD
    return FragmentAction.FETCH
