"""Tests for Smart Fades-aware queue ordering primitives."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.enums import MediaType
from music_assistant_models.media_items import ItemMapping, ProviderMapping, Track
from music_assistant_models.unique_list import UniqueList

from music_assistant.controllers.player_queues.smart_fade_ordering import (
    _energy_score,
    _pair_score,
    _tempo_score,
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
    """SFREQ-07: No analysis is neutral; it is neither a bonus nor a penalty."""
    known = _analysis()
    assert _pair_score(None, known) == 0.0
    assert _pair_score(known, None) == 0.0


def test_partial_analysis_has_lower_confidence_than_full_match() -> None:
    """SFREQ-07: Missing fields stay at zero; known fields do not get extra weight."""
    outgoing = _analysis(bpm=120.0, key="C", mode="major", tail=0.5)
    partial = AudioAnalysisData(key="G", mode="major")
    full = _analysis(bpm=120.0, key="G", mode="major", head=0.5)

    partial_score = _pair_score(outgoing, partial)
    full_score = _pair_score(outgoing, full)

    assert 0.0 < partial_score < full_score


def test_half_and_double_time_count_as_tempo_matches() -> None:
    """SFREQ-06: Half/double-time relationships count as tempo matches."""
    assert _tempo_score(120.0, 60.0) == 1.0
    assert _tempo_score(120.0, 240.0) == 1.0


def test_outside_stretch_window_scores_negative() -> None:
    """SFREQ-06: A tempo mismatch lowers the score but does not make the track ineligible."""
    score = _tempo_score(120.0, 150.0)
    assert score is not None
    assert score < 0.0


def test_good_harmonic_energy_pair_beats_bad_pair() -> None:
    """SFREQ-06: A better tempo/key/energy match should get the better score."""
    outgoing = _analysis(bpm=120.0, key="C", mode="major", tail=0.5)
    good = _analysis(bpm=122.0, key="G", mode="major", head=0.5)
    bad = _analysis(bpm=145.0, key="F#", mode="minor", head=0.05)

    assert _pair_score(outgoing, good) > _pair_score(outgoing, bad)


def test_energy_score_prefers_matching_edges() -> None:
    """SFREQ-06: Matching end-to-start energy should score better."""
    outgoing = _analysis(tail=0.5)
    matching = _analysis(head=0.5)
    dropping = _analysis(head=0.05)

    good_score = _energy_score(outgoing, matching)
    bad_score = _energy_score(outgoing, dropping)

    assert good_score is not None
    assert bad_score is not None
    assert good_score > bad_score


@pytest.mark.asyncio
async def test_order_tracks_prefers_better_transition(monkeypatch: pytest.MonkeyPatch) -> None:
    """SFREQ-06/SFREQ-08: The preceding track anchors the first choice in the batch."""
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
    """SFREQ-04: Once a track is in the refill batch, its source position does not lock it in place."""
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
    """SFREQ-05: Keep existing artist spacing ahead of transition score."""
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
async def test_analysis_lookups_are_bounded_to_local_window() -> None:
    """SFREQ-07/SFREQ-09: Only look up the small local set we are actually considering."""
    anchor = _track("anchor")
    tracks = [_track(f"track-{index}") for index in range(20)]
    active = 0
    peak = 0

    async def lookup(*_args: object, **_kwargs: object) -> AudioAnalysisData:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0)
        active -= 1
        return _analysis()

    mass = MagicMock()
    mass.streams.audio_analysis.get_audio_analysis = AsyncMock(side_effect=lookup)

    await order_tracks(mass, tracks, preceding_track=anchor)

    assert peak <= 6


@pytest.mark.asyncio
async def test_near_ties_keep_random_choice(monkeypatch: pytest.MonkeyPatch) -> None:
    """SFREQ-09: Near-equal candidates should still leave room for a random choice."""
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
    """SFREQ-10: Queue ordering must not ask the full planner to rank candidates."""
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
