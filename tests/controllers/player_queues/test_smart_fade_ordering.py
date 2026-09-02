"""Tests for Smart Fades-aware queue ordering primitives."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.enums import MediaType
from music_assistant_models.media_items import ItemMapping, ProviderMapping, Track
from music_assistant_models.unique_list import UniqueList

from music_assistant.controllers.player_queues.smart_fade_ordering import (
    _edge_energy,
    _energy_score,
    _features,
    _pair_score,
    _tempo_score,
    order_queue_items,
    order_tracks,
)
from music_assistant.controllers.streams.smart_fades.planner import SmartCrossFadePlanner
from music_assistant.models.audio_analysis import AudioAnalysisData


def _analysis(
    *,
    bpm: float = 120.0,
    key: str = "C",
    mode: str = "major",
    head: float = 0.5,
    tail: float = 0.5,
) -> AudioAnalysisData:
    values = [0.5] * 180
    values[:10] = [head] * 10
    values[-10:] = [tail] * 10
    return AudioAnalysisData(
        duration=180.0,
        bpm=bpm,
        key=key,
        mode=mode,
        rms_energy=values,
    )


def _track(item_id: str, *, artist: str = "A") -> Track:
    return Track(
        item_id=item_id,
        provider="test",
        name=f"Track {item_id}",
        duration=180,
        artists=UniqueList(
            [
                ItemMapping(
                    item_id=f"artist-{artist.lower()}",
                    provider="test",
                    name=artist,
                    media_type=MediaType.ARTIST,
                )
            ]
        ),
        provider_mappings={
            ProviderMapping(item_id=item_id, provider_domain="test", provider_instance="test")
        },
    )


def test_missing_analysis_is_exactly_neutral() -> None:
    """No analysis is neutral; it is neither a bonus nor a penalty."""
    known = _features(_analysis())
    assert _pair_score(_features(None), known) == 0.0
    assert _pair_score(known, _features(None)) == 0.0


def test_partial_analysis_has_lower_confidence_than_full_match() -> None:
    """Missing fields stay at zero; known fields do not get extra weight."""
    outgoing = _features(_analysis(bpm=120.0, key="C", mode="major", tail=0.5))
    partial = _features(AudioAnalysisData(key="G", mode="major"))
    full = _features(_analysis(bpm=120.0, key="G", mode="major", head=0.5))

    partial_score = _pair_score(outgoing, partial)
    full_score = _pair_score(outgoing, full)

    assert 0.0 < partial_score < full_score


def test_half_and_double_time_are_discounted_tempo_matches() -> None:
    """Octave-related pairs are musically coherent but not yet beatmatchable, so they score lower."""
    same_tempo = _tempo_score(120.0, 120.0)
    half_time = _tempo_score(120.0, 60.0)
    double_time = _tempo_score(120.0, 240.0)

    assert half_time == 0.6
    assert double_time == 0.6
    assert same_tempo is not None
    assert half_time < same_tempo
    assert double_time < same_tempo


def test_outside_stretch_window_scores_negative() -> None:
    """A tempo mismatch lowers the score but does not make the track ineligible."""
    score = _tempo_score(120.0, 150.0)
    assert score is not None
    assert score < 0.0


def test_good_harmonic_energy_pair_beats_bad_pair() -> None:
    """A better tempo/key/energy match should get the better score."""
    outgoing = _features(_analysis(bpm=120.0, key="C", mode="major", tail=0.5))
    good = _features(_analysis(bpm=122.0, key="G", mode="major", head=0.5))
    bad = _features(_analysis(bpm=145.0, key="F#", mode="minor", head=0.05))

    assert _pair_score(outgoing, good) > _pair_score(outgoing, bad)


def test_energy_score_prefers_matching_edges() -> None:
    """Matching end-to-start energy should score better."""
    outgoing = _features(_analysis(tail=0.5))
    matching = _features(_analysis(head=0.5))
    dropping = _features(_analysis(head=0.05))

    good_score = _energy_score(outgoing.tail_level, matching.head_level)
    bad_score = _energy_score(outgoing.tail_level, dropping.head_level)

    assert good_score is not None
    assert bad_score is not None
    assert good_score > bad_score


def test_energy_score_ignores_silent_outgoing_tail() -> None:
    """A silent portion at the edge is excluded; only the audible level counts."""
    padded_tail = AudioAnalysisData(
        duration=300.0,
        rms_energy=[0.5] * 1700 + [0.0] * 100,
    )
    fully_silent = AudioAnalysisData(
        duration=300.0,
        rms_energy=[0.0] * 1800,
    )

    assert _edge_energy(padded_tail, from_start=False) == pytest.approx(0.5)
    assert _edge_energy(fully_silent, from_start=False) is None


@pytest.mark.asyncio
async def test_order_tracks_prefers_better_transition(monkeypatch: pytest.MonkeyPatch) -> None:
    """The preceding track anchors the first choice in the batch."""
    anchor = _track("anchor")
    bad = _track("bad")
    good = _track("good")
    rows = {
        "anchor": _analysis(bpm=120.0, key="C", mode="major", tail=0.5),
        "bad": _analysis(bpm=145.0, key="F#", mode="minor", head=0.05),
        "good": _analysis(bpm=121.0, key="G", mode="major", head=0.5),
    }
    mass = MagicMock()
    mass.streams.audio_analysis.get_audio_analysis = AsyncMock(
        side_effect=lambda item_id, *_args, **_kwargs: rows.get(item_id)
    )
    monkeypatch.setattr(
        "music_assistant.controllers.player_queues.smart_fade_ordering.random.choice",
        lambda values: values[0],
    )

    ordered = await order_tracks(mass, [bad, good], preceding_track=anchor)

    assert ordered[0] is good


@pytest.mark.asyncio
async def test_input_order_does_not_block_better_transition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Once a track is in the refill batch, its source position does not lock it in place."""
    anchor = _track("anchor")
    first = _track("first")
    second = _track("second")
    rows = {
        "anchor": _analysis(bpm=120.0, key="C", mode="major"),
        "first": _analysis(bpm=145.0, key="F#", mode="minor"),
        "second": _analysis(bpm=120.0, key="G", mode="major"),
    }
    mass = MagicMock()
    mass.streams.audio_analysis.get_audio_analysis = AsyncMock(
        side_effect=lambda item_id, *_args, **_kwargs: rows.get(item_id)
    )
    monkeypatch.setattr(
        "music_assistant.controllers.player_queues.smart_fade_ordering.random.choice",
        lambda values: values[0],
    )

    ordered = await order_tracks(
        mass,
        [first, second],
        preceding_track=anchor,
    )

    assert ordered[0] is second


@pytest.mark.asyncio
async def test_order_tracks_prefers_different_artist_before_transition_score(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep existing artist spacing ahead of transition score."""
    anchor = _track("anchor", artist="A")
    same_artist = _track("same", artist="A")
    other_artist = _track("other", artist="B")
    rows = {
        "anchor": _analysis(bpm=120.0, key="C", mode="major", tail=0.5),
        "same": _analysis(bpm=120.0, key="G", mode="major", head=0.5),
        "other": _analysis(bpm=145.0, key="F#", mode="minor", head=0.05),
    }
    mass = MagicMock()
    mass.streams.audio_analysis.get_audio_analysis = AsyncMock(
        side_effect=lambda item_id, *_args, **_kwargs: rows.get(item_id)
    )
    monkeypatch.setattr(
        "music_assistant.controllers.player_queues.smart_fade_ordering.random.choice",
        lambda values: values[0],
    )

    ordered = await order_tracks(mass, [same_artist, other_artist], preceding_track=anchor)

    assert ordered[0] is other_artist


@pytest.mark.asyncio
async def test_fixed_queue_considers_tracks_beyond_first_few_positions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fixed queue can select the best transition from the full population."""
    anchor = _track("anchor")
    bad_tracks = [_track(f"bad-{index}") for index in range(7)]
    good = _track("good")
    rows = {
        "anchor": _analysis(bpm=120.0, key="C", mode="major", tail=0.5),
        **{
            track.item_id: _analysis(bpm=150.0, key="F#", mode="minor", head=0.05)
            for track in bad_tracks
        },
        "good": _analysis(bpm=120.0, key="G", mode="major", head=0.5),
    }
    mass = MagicMock()
    mass.streams.audio_analysis.get_audio_analysis = AsyncMock(
        side_effect=lambda item_id, *_args, **_kwargs: rows.get(item_id)
    )
    monkeypatch.setattr(
        "music_assistant.controllers.player_queues.smart_fade_ordering.random.choice",
        lambda values: values[0],
    )

    ordered = await order_queue_items(
        mass,
        [*bad_tracks, good],
        get_track=lambda track: track,
        preceding_track=anchor,
    )

    assert ordered[0] is good


@pytest.mark.asyncio
async def test_analysis_lookup_happens_once_per_distinct_track() -> None:
    """Precomputed features mean each distinct track's analysis is fetched only once."""
    anchor = _track("anchor")
    tracks = [_track(f"track-{index}") for index in range(20)]
    calls: dict[str, int] = {}

    async def lookup(item_id: str, *_args: object, **_kwargs: object) -> AudioAnalysisData:
        calls[item_id] = calls.get(item_id, 0) + 1
        return _analysis()

    mass = MagicMock()
    mass.streams.audio_analysis.get_audio_analysis = AsyncMock(side_effect=lookup)

    await order_tracks(mass, tracks, preceding_track=anchor)

    assert all(count == 1 for count in calls.values())


@pytest.mark.asyncio
async def test_dynamic_batch_considers_the_full_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 25-track refill batch can still pair tracks that start far apart in the batch."""
    anchor = _track("anchor")
    good_analysis = _analysis(bpm=120.0, key="C", mode="major", head=0.5, tail=0.5)
    bad_analysis = _analysis(bpm=145.0, key="F#", mode="minor", head=0.05, tail=0.05)

    first = _track("first")
    partner = _track("partner")
    fillers = [_track(f"filler-{index}") for index in range(23)]
    tracks = [first, *fillers[:19], partner, *fillers[19:]]

    rows = {
        "anchor": good_analysis,
        "first": good_analysis,
        "partner": good_analysis,
        **{track.item_id: bad_analysis for track in fillers},
    }
    mass = MagicMock()
    mass.streams.audio_analysis.get_audio_analysis = AsyncMock(
        side_effect=lambda item_id, *_args, **_kwargs: rows.get(item_id)
    )
    monkeypatch.setattr(
        "music_assistant.controllers.player_queues.smart_fade_ordering.random.choice",
        lambda values: values[0],
    )

    ordered = await order_tracks(mass, tracks, preceding_track=anchor)

    assert abs(ordered.index(first) - ordered.index(partner)) == 1


@pytest.mark.asyncio
async def test_near_ties_keep_random_choice(monkeypatch: pytest.MonkeyPatch) -> None:
    """Near-equal candidates should still leave room for a random choice."""
    anchor = _track("anchor")
    first = _track("first")
    second = _track("second")
    rows = {
        "anchor": _analysis(),
        "first": _analysis(),
        "second": _analysis(),
    }
    mass = MagicMock()
    mass.streams.audio_analysis.get_audio_analysis = AsyncMock(
        side_effect=lambda item_id, *_args, **_kwargs: rows.get(item_id)
    )
    chooser = MagicMock(side_effect=lambda values: values[-1])
    monkeypatch.setattr(
        "music_assistant.controllers.player_queues.smart_fade_ordering.random.choice",
        chooser,
    )

    ordered = await order_tracks(mass, [first, second], preceding_track=anchor)

    first_choice = chooser.call_args_list[0].args[0]
    assert set(first_choice) == {0, 1}
    assert ordered[0] is second


@pytest.mark.asyncio
async def test_ordering_does_not_call_full_transition_planner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Queue ordering must not ask the full planner to rank candidates."""
    anchor = _track("anchor")
    first = _track("first")
    second = _track("second")
    rows = {
        "anchor": _analysis(),
        "first": _analysis(bpm=121.0, key="G"),
        "second": _analysis(bpm=123.0, key="F"),
    }
    mass = MagicMock()
    mass.streams.audio_analysis.get_audio_analysis = AsyncMock(
        side_effect=lambda item_id, *_args, **_kwargs: rows.get(item_id)
    )

    def planner_must_not_run(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("SmartCrossFadePlanner must not run during queue ordering")

    monkeypatch.setattr(SmartCrossFadePlanner, "plan", planner_must_not_run)

    ordered = await order_tracks(mass, [first, second], preceding_track=anchor)

    assert sorted(track.item_id for track in ordered) == ["first", "second"]
