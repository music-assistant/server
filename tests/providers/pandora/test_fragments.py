"""Tests for Pandora fragment retention and the fetch-or-keep-serving gate."""

from __future__ import annotations

from typing import Any

from music_assistant.providers.pandora.fragments import (
    FRAGMENT_STALE_SECONDS,
    MAX_RETAINED_FRAGMENTS,
    PandoraFragment,
    PandoraStationSession,
    should_fetch_fragment,
)

NOW = 1_000_000.0


def _tracks(count: int = 4, prefix: str = "S") -> list[dict[str, Any]]:
    """Build `count` raw Pandora track dicts with distinct music ids."""
    return [
        {
            "musicId": f"{prefix}{index}",
            "stationId": "4360491625318318161",
            "songTitle": f"Song {index}",
            "artistName": "Some Artist",
            "albumTitle": "Some Album",
            "trackLength": 180,
            "audioURL": f"https://audio-sv5-t3-2.pandora.com/access/{index}.mp4",
        }
        for index in range(count)
    ]


def _fragment(**kwargs: Any) -> PandoraFragment:
    """Build a fragment of four tracks last active at NOW, overridable by keyword."""
    fragment = PandoraFragment(tracks=_tracks(), last_activity_at=NOW)
    for key, value in kwargs.items():
        setattr(fragment, key, value)
    return fragment


def test_no_fragment_fetches() -> None:
    """A station with no fragment yet must fetch one."""
    assert should_fetch_fragment(None, NOW) is True


def test_live_fragment_is_not_refetched() -> None:
    """A fragment whose URLs are still live must be served again, not replaced."""
    assert should_fetch_fragment(_fragment(), NOW) is False


def test_stale_fragment_is_refetched() -> None:
    """A fragment nothing has streamed from for the whole window holds expired URLs."""
    later = NOW + FRAGMENT_STALE_SECONDS + 1
    assert should_fetch_fragment(_fragment(), later) is True


def test_abandoned_playback_is_refetched_once_stale() -> None:
    """Stopping mid-fragment leaves URLs that eventually expire; refetch then."""
    fragment = _fragment()
    fragment.mark_resolved("S0", NOW)
    later = NOW + FRAGMENT_STALE_SECONDS + 1
    assert should_fetch_fragment(fragment, later) is True


def test_active_playback_never_refetches_mid_fragment() -> None:
    """Each hand-out refreshes activity, so a playing station never trips staleness."""
    fragment = _fragment()
    # tracks handed out a few minutes apart, as the stream feeder would during playback
    for index, offset in enumerate((0, 300, 600)):
        fragment.mark_resolved(f"S{index}", NOW + offset)
        assert should_fetch_fragment(fragment, NOW + offset) is False
    # 900s since the fragment was fetched, but only 300s since the last hand-out:
    # a fetched-at clock would wrongly call this abandoned, a last-activity clock does not
    assert should_fetch_fragment(fragment, NOW + 900) is False


def test_spent_fragment_advances() -> None:
    """Once the last track has been handed out it is safe to pull the next fragment."""
    assert should_fetch_fragment(_fragment(spent=True), NOW) is True


def test_mark_resolved_last_track_spends_fragment() -> None:
    """Handing out the final track opens the gate."""
    fragment = _fragment()
    fragment.mark_resolved("S3", NOW)
    assert fragment.spent is True


def test_mark_resolved_earlier_track_does_not_spend() -> None:
    """Handing out a non-final track keeps the gate shut."""
    fragment = _fragment()
    fragment.mark_resolved("S1", NOW)
    assert fragment.spent is False


def test_mark_resolved_refreshes_activity() -> None:
    """Handing out a track restarts the staleness clock."""
    fragment = _fragment()
    later = NOW + FRAGMENT_STALE_SECONDS - 1
    fragment.mark_resolved("S1", later)
    assert fragment.last_activity_at == later
    assert fragment.is_stale(later + FRAGMENT_STALE_SECONDS - 1) is False
    assert fragment.is_stale(later + FRAGMENT_STALE_SECONDS + 1) is True


def test_mark_resolved_unknown_track_is_a_noop() -> None:
    """An id from an older fragment must not flip this fragment's flag or clock."""
    fragment = _fragment()
    fragment.mark_resolved("nope", NOW + 100)
    assert fragment.spent is False
    assert fragment.last_activity_at == NOW


def test_find_returns_track_by_music_id() -> None:
    """find() looks a raw track dict up by its Pandora musicId."""
    fragment = _fragment()
    found = fragment.find("S2")
    assert found is not None
    assert found["songTitle"] == "Song 2"
    assert fragment.find("missing") is None


def test_is_stale_measures_time_since_last_activity() -> None:
    """Staleness is purely a function of the last hand-out, with a strict boundary."""
    assert _fragment().is_stale(NOW + FRAGMENT_STALE_SECONDS + 1) is True
    assert _fragment().is_stale(NOW) is False
    # exactly at the boundary is not yet stale
    assert _fragment().is_stale(NOW + FRAGMENT_STALE_SECONDS) is False


def test_session_current_is_the_newest_fragment() -> None:
    """Current always points at the fragment holding live audio URLs."""
    session = PandoraStationSession("4360491625318318161")
    assert session.current is None
    first = session.add_fragment(_tracks(prefix="A"), NOW)
    assert session.current is first
    second = session.add_fragment(_tracks(prefix="B"), NOW)  # type: ignore[unreachable]
    assert session.current is second


def test_session_retains_a_bounded_number_of_fragments() -> None:
    """Fragment metadata is bounded so a long session cannot grow without limit."""
    session = PandoraStationSession("4360491625318318161")
    for index in range(MAX_RETAINED_FRAGMENTS + 3):
        session.add_fragment(_tracks(prefix=f"F{index}_"), NOW)
    assert len(session.fragments) == MAX_RETAINED_FRAGMENTS


def test_session_find_track_searches_retained_fragments() -> None:
    """Recently played tracks stay resolvable for queue history."""
    session = PandoraStationSession("4360491625318318161")
    session.add_fragment(_tracks(prefix="old"), NOW)
    session.add_fragment(_tracks(prefix="new"), NOW)
    assert session.find_track("old1") is not None
    assert session.find_track("new1") is not None
    assert session.find_track("gone") is None


def test_session_find_track_drops_evicted_fragments() -> None:
    """A fragment pushed out of the deque is no longer resolvable."""
    session = PandoraStationSession("4360491625318318161")
    session.add_fragment(_tracks(prefix="first"), NOW)
    for index in range(MAX_RETAINED_FRAGMENTS):
        session.add_fragment(_tracks(prefix=f"later{index}_"), NOW)
    assert session.find_track("first1") is None
