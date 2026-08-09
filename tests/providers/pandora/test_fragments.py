"""Tests for Pandora fragment retention and the fetch/reuse/withhold gate."""

from __future__ import annotations

from typing import Any

from music_assistant.providers.pandora.fragments import (
    FRAGMENT_STALE_SECONDS,
    MAX_RETAINED_FRAGMENTS,
    FragmentAction,
    PandoraFragment,
    PandoraStationSession,
    next_fragment_action,
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
    assert next_fragment_action(None, NOW) is FragmentAction.FETCH


def test_unresolved_fragment_is_reused() -> None:
    """Browse fetched a fragment nobody streams from; play must get that same batch."""
    assert next_fragment_action(_fragment(), NOW) is FragmentAction.REUSE


def test_stale_unresolved_fragment_is_refetched() -> None:
    """An untouched fragment older than the staleness window holds expired URLs."""
    fragment = _fragment()
    later = NOW + FRAGMENT_STALE_SECONDS + 1
    assert next_fragment_action(fragment, later) is FragmentAction.FETCH


def test_resolved_unspent_fragment_withholds() -> None:
    """The gate is closed while handed-out URLs are still pending playback."""
    fragment = _fragment(resolved=True)
    assert next_fragment_action(fragment, NOW) is FragmentAction.WITHHOLD


def test_abandoned_playback_recovers_after_the_stale_window() -> None:
    """Stopping mid-fragment must not strand the station on WITHHOLD forever."""
    fragment = _fragment(resolved=True)
    later = NOW + FRAGMENT_STALE_SECONDS + 1
    assert next_fragment_action(fragment, later) is FragmentAction.FETCH


def test_active_playback_keeps_the_gate_shut_across_tracks() -> None:
    """Each resolution refreshes activity, so a playing station never trips staleness."""
    fragment = _fragment()
    # tracks resolved a few minutes apart, as the stream feeder would during playback
    for index, offset in enumerate((0, 300, 600, 900)[:3]):
        fragment.mark_resolved(f"S{index}", NOW + offset)
        assert next_fragment_action(fragment, NOW + offset) is FragmentAction.WITHHOLD
    # 900s since the fragment was fetched, but only 300s since the last resolution:
    # a fetched-at clock would wrongly call this abandoned, a last-activity clock does not
    assert next_fragment_action(fragment, NOW + 900) is FragmentAction.WITHHOLD


def test_spent_fragment_advances() -> None:
    """Once the last track has been handed out it is safe to pull the next fragment."""
    fragment = _fragment(resolved=True, spent=True)
    assert next_fragment_action(fragment, NOW) is FragmentAction.FETCH


def test_mark_resolved_last_track_spends_fragment() -> None:
    """Resolving the final track opens the gate."""
    fragment = _fragment()
    fragment.mark_resolved("S3", NOW)
    assert fragment.resolved is True
    assert fragment.spent is True


def test_mark_resolved_earlier_track_does_not_spend() -> None:
    """Resolving a non-final track marks the fragment live but keeps the gate shut."""
    fragment = _fragment()
    fragment.mark_resolved("S1", NOW)
    assert fragment.resolved is True
    assert fragment.spent is False


def test_mark_resolved_refreshes_activity() -> None:
    """Resolving a track restarts the staleness clock."""
    fragment = _fragment()
    later = NOW + FRAGMENT_STALE_SECONDS - 1
    fragment.mark_resolved("S1", later)
    assert fragment.last_activity_at == later
    assert fragment.is_stale(later + FRAGMENT_STALE_SECONDS - 1) is False
    assert fragment.is_stale(later + FRAGMENT_STALE_SECONDS + 1) is True


def test_mark_resolved_unknown_track_is_a_noop() -> None:
    """An id from an older fragment must not flip this fragment's flags or clock."""
    fragment = _fragment()
    fragment.mark_resolved("nope", NOW + 100)
    assert fragment.resolved is False
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
    """Staleness applies to resolved and unresolved fragments alike."""
    later = NOW + FRAGMENT_STALE_SECONDS + 1
    assert _fragment().is_stale(later) is True
    assert _fragment(resolved=True).is_stale(later) is True
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
