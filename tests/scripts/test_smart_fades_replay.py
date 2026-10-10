"""Tests for the smart fades replay script's pure helpers."""

import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest

from scripts.smart_fades_replay import (
    TrackInfo,
    _snapshot_db,
    album_pairs,
    random_pairs,
    style_bucket,
    summarize,
    tag_bucket,
    vocal_class,
)


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


def test_album_pairs_follow_track_order_across_discs() -> None:
    """Consecutive tracks pair up, also from a disc's last track to the next disc's first."""
    a, b, c, d = (("a", "p"), ("b", "p"), ("c", "p"), ("d", "p"))
    tracks = {
        a: TrackInfo(name="a", album_positions=[(1, 1, 1)]),
        b: TrackInfo(name="b", album_positions=[(1, 1, 2)]),
        c: TrackInfo(name="c", album_positions=[(1, 2, 1)]),
        # a gap: track 3 of disc 2 has no predecessor in the library
        d: TrackInfo(name="d", album_positions=[(1, 2, 3)]),
    }

    assert album_pairs([a, b, c, d], tracks) == [(a, b), (b, c)]


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
    assert "first trigger: {'tempo': 1, 'meter': 1}" in summary
    assert "== short (<8 s) shipped: 2 of 3, by cause" in summary
    assert "   both sing         1 100.0%  2.00s | 1 | 0 | 0 | 0 | 0 | 0 | 0" in summary
    assert "  outgoing tail is mostly silent: 1" in summary
    assert "bucketed 1 (100.0%)" in summary


def test_snapshot_db_folds_the_wal_into_a_copy_and_leaves_the_source_alone(
    tmp_path: Path,
) -> None:
    """The copy holds the WAL's rows while the source files stay byte for byte the same."""
    source = tmp_path / "source" / "library.db"
    source.parent.mkdir()
    writer = sqlite3.connect(source)
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute("PRAGMA wal_autocheckpoint=0")
    writer.execute("CREATE TABLE tracks (name TEXT)")
    writer.execute("INSERT INTO tracks VALUES ('in the wal')")
    writer.commit()
    files = {path.name: path.read_bytes() for path in source.parent.iterdir()}
    assert "library.db-wal" in files

    copy = _snapshot_db(source, tmp_path / "copy")

    assert {path.name: path.read_bytes() for path in source.parent.iterdir()} == files
    writer.close()
    with closing(sqlite3.connect(f"{copy.as_uri()}?mode=ro", uri=True)) as conn:
        assert conn.execute("SELECT name FROM tracks").fetchall() == [("in the wal",)]
    assert not copy.with_name("library.db-wal").exists()


def _pair_row() -> dict[str, Any]:
    """Return the per-pair columns every row carries."""
    return {"out_bucket": "rock", "in_bucket": "unknown", "bpm_diff_pct": 25.0}


def _plan_row(
    tier: str, overlap: float, vocals: str, cause: str, qf_trigger: str = ""
) -> dict[str, Any]:
    """Return a planned row with the columns the summary reads."""
    return {
        **_pair_row(),
        "outcome": "plan",
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
