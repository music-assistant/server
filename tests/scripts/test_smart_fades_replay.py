"""Tests for the smart fades replay script."""

import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest

from music_assistant.controllers.streams.smart_fades.planner import SmartCrossFadePlanner
from music_assistant.controllers.streams.smart_fades.planner.selection import CandidateSelector
from scripts.smart_fades_replay import (
    Replayer,
    TrackInfo,
    _snapshot,
    random_pairs,
    style_bucket,
    summarize,
    tag_bucket,
    vocal_class,
)
from tests.controllers.streams.smart_fades.conftest import _analysis_with_bands


@pytest.mark.parametrize(
    ("tag", "bucket"),
    [
        ("Pop Rock", "rock"),
        ("dance-pop", "pop"),
        ("rock opera", "rock"),
        ("opera", "classical"),
        ("Deep House", "house/electronic"),
        ("trip hop", "ambient/downtempo"),
        ("hip hop", "hip-hop"),
        ("Soundtrack", None),
    ],
)
def test_tag_bucket_follows_the_head_noun(tag: str, bucket: str | None) -> None:
    """The keyword ending last decides, and of two ending together the longer one."""
    assert tag_bucket(tag) == bucket


def test_style_bucket_takes_the_majority_and_breaks_ties_in_bucket_order() -> None:
    """Most votes win; equal votes go to the bucket listed first; no style is unknown."""
    assert style_bucket(["rock", "pop", "synthpop"]) == "pop"
    assert style_bucket(["pop", "rock"]) == "rock"
    assert style_bucket(["Soundtrack"]) == "unknown"
    assert style_bucket([]) == "unknown"


@pytest.mark.parametrize(
    ("out_duty", "in_duty", "expected"),
    [
        (0.5, 0.2, "both sing"),
        (0.5, 0.1, "outgoing only"),
        (0.1, 0.11, "incoming only"),
        (0.0, 0.0, "neither"),
        (None, 0.5, "unknown"),
        (0.5, None, "unknown"),
    ],
)
def test_vocal_class_needs_more_than_a_tenth_to_sing(
    out_duty: float | None, in_duty: float | None, expected: str
) -> None:
    """A deck sings above 0.10 vocal duty; a deck without a vocal timeline is unknown."""
    assert vocal_class(out_duty, in_duty) == expected


def test_random_pairs_are_reproducible_distinct_and_cross_album() -> None:
    """The same seed draws the same pairs, never twice and never within one album."""
    keys = [(f"t{n}", "filesystem--x") for n in range(6)]
    tracks = {key: TrackInfo(name=key[0], album_id=n // 2) for n, key in enumerate(keys)}

    pairs = random_pairs(keys, tracks, 20, seed=7)

    assert pairs == random_pairs(keys, tracks, 20, seed=7)
    assert len(pairs) == len(set(pairs)) == 20
    assert all(tracks[out].album_id != tracks[inc].album_id for out, inc in pairs)


def test_random_pairs_draw_from_the_bucket_pools() -> None:
    """Pools restrict the outgoing and incoming side independently."""
    keys = [(f"t{n}", "tidal") for n in range(6)]
    tracks = {key: TrackInfo(name=key[0]) for key in keys}

    pairs = random_pairs(keys, tracks, 4, seed=1, out_pool=keys[:2], in_pool=keys[4:])

    assert len(pairs) == 4
    assert all(out in keys[:2] and inc in keys[4:] for out, inc in pairs)


def test_summarize_aggregates_tiers_triggers_and_reasons() -> None:
    """The summary counts outcomes, overlap by tier, quick fade triggers and N/A reasons."""
    rows = [
        _plan_row("FULL_BLEND", 16.0, "neither", "blend: short top rung"),
        _plan_row("QUICK_FADE", 2.0, "both sing", "QF: tempo", qf_trigger="tempo"),
        _plan_row("QUICK_FADE", 3.0, "outgoing only", "QF: meter", qf_trigger="meter"),
        {
            **_pair_row(),
            "outcome": "not_applicable",
            "reason": "outgoing tail is mostly silent (2.1s audible)",
        },
    ]
    tracks = {("t1", "p"): TrackInfo(name="t1", bucket="rock")}

    summary = summarize(rows, tracks, "header")

    assert summary.startswith("header\n")
    assert "QUICK_FADE: 2 (50.0%), FULL_BLEND: 1 (25.0%), not_applicable: 1 (25.0%)" in summary
    assert "  QUICK_FADE       2   2.50   2.10   2.90 100.0% 100.0%" in summary
    assert "== QUICK_FADE 2; trigger: {'tempo': 1, 'meter': 1}" in summary
    assert "== short (<8 s) shipped: 2 of 3, by cause" in summary
    assert "   both sing         1 100.0%  2.00s | 1 | 0 | 0 | 0 | 0 | 0" in summary
    assert "  outgoing tail is mostly silent: 1" in summary
    assert "bucketed 1 (100.0%)" in summary


def test_snapshot_reads_the_wal_from_a_copy_and_leaves_the_source_alone(
    tmp_path: Path,
) -> None:
    """The copy holds the WAL's rows while the source data files stay byte for byte the same."""
    source = tmp_path / "source" / "library.db"
    source.parent.mkdir()
    writer = sqlite3.connect(source)
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute("PRAGMA wal_autocheckpoint=0")
    writer.execute("CREATE TABLE tracks (name TEXT)")
    writer.execute("INSERT INTO tracks VALUES ('in the wal')")
    writer.commit()
    files = _data_files(source.parent)
    assert "library.db-wal" in files

    with closing(_snapshot(source, tmp_path / "copy")) as conn:
        assert [tuple(row) for row in conn.execute("SELECT name FROM tracks")] == [("in the wal",)]

    assert _data_files(source.parent) == files
    writer.close()


def test_snapshot_reads_an_offline_copy_with_only_a_wal_file(tmp_path: Path) -> None:
    """A copy taken from a backup (database plus -wal, no -shm) still yields the WAL's rows."""
    live = tmp_path / "live" / "library.db"
    live.parent.mkdir()
    writer = sqlite3.connect(live)
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute("PRAGMA wal_autocheckpoint=0")
    writer.execute("CREATE TABLE tracks (name TEXT)")
    writer.execute("INSERT INTO tracks VALUES ('in the wal')")
    writer.commit()
    offline = tmp_path / "offline"
    offline.mkdir()
    for suffix in ("", "-wal"):
        name = f"library.db{suffix}"
        (offline / name).write_bytes((live.parent / name).read_bytes())
    writer.close()
    files = _data_files(offline)

    with closing(_snapshot(offline / "library.db", tmp_path / "copy")) as conn:
        assert [tuple(row) for row in conn.execute("SELECT name FROM tracks")] == [("in the wal",)]

    assert _data_files(offline) == files


def test_replayer_stops_on_an_unexpected_planner_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only a not-applicable pair is a result; any other planner error ends the run, naming the pair."""
    monkeypatch.setattr(CandidateSelector, "select", CandidateSelector.select)
    monkeypatch.setattr(CandidateSelector, "_score", CandidateSelector._score)

    def _broken_plan(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("planner regression")

    monkeypatch.setattr(SmartCrossFadePlanner, "plan", _broken_plan)
    out_key, in_key = ("out", "filesystem--x"), ("in", "filesystem--x")
    analyses = {key: _analysis_with_bands(1.0, 0.5, 0.5, 0.3) for key in (out_key, in_key)}
    tracks = {key: TrackInfo(name=key[0]) for key in analyses}

    with pytest.raises(RuntimeError, match="planner regression") as exc_info:
        Replayer(analyses, tracks, ceiling=45.0).replay(out_key, in_key)

    assert exc_info.value.__notes__ == ["while planning out -> in"]


def test_replayer_plans_a_pair_and_reports_its_facts(monkeypatch: pytest.MonkeyPatch) -> None:
    """A synthetic pair runs through the planner and comes back with its plan, vocal and drum facts."""
    # the replayer wraps the selector's methods; monkeypatch puts the originals back afterwards
    monkeypatch.setattr(CandidateSelector, "select", CandidateSelector.select)
    monkeypatch.setattr(CandidateSelector, "_score", CandidateSelector._score)
    out_key, in_key = ("out", "filesystem--x"), ("in", "filesystem--x")
    analyses = {key: _analysis_with_bands(1.0, 0.5, 0.5, 0.3) for key in (out_key, in_key)}
    tracks = {key: TrackInfo(name=key[0]) for key in analyses}

    row = Replayer(analyses, tracks, ceiling=45.0).replay(out_key, in_key)

    assert row["outcome"] == "plan"
    assert row["tier"] == row["ctx_tier"] == "FULL_BLEND"
    assert row["qf_trigger"] == ""
    assert row["shipped_via"] == "main"
    assert row["source"]
    assert row["bars"] == 8
    assert row["vocal_class"] == "unknown"
    assert row["rhythm_safe"] is False
    assert row["cause"] == "blend: short top rung"


def _data_files(folder: Path) -> dict[str, bytes]:
    """Return the database files in folder by name, leaving out SQLite's -shm reader index."""
    return {
        path.name: path.read_bytes() for path in folder.iterdir() if not path.name.endswith("-shm")
    }


def _pair_row() -> dict[str, Any]:
    """Return the per-pair columns every row carries."""
    return {"out_bucket": "rock", "in_bucket": "unknown"}


def _plan_row(
    tier: str, overlap: float, vocals: str, cause: str, qf_trigger: str = ""
) -> dict[str, Any]:
    """Return a planned row with the columns the summary reads."""
    return {
        **_pair_row(),
        "outcome": "plan",
        "bpm_diff_pct": 25.0,
        "ctx_tier": tier,
        "tier": tier,
        "qf_trigger": qf_trigger,
        "strategy": "ENERGY_ALIGNED",
        "shipped_via": "main",
        "source": "energy-ladder",
        "bars": 1,
        "overlap_s": overlap,
        "vocal_class": vocals,
        "rhythm_safe": "",
        "cause": cause,
    }
