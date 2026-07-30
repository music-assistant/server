"""Tests for the shared baseline helper used by baseline-aware lint checks."""

from pathlib import Path

from scripts.lint_baseline import diff_baseline, load_baseline, render_baseline


def test_render_and_load_roundtrip(tmp_path: Path) -> None:
    """A rendered baseline reloads to the same per-file counts (and skips zero counts)."""
    counts = {"b/file.py": 2, "a/file.py": 1, "c/skip.py": 0}
    path = tmp_path / "baseline.txt"
    path.write_text(render_baseline(counts, "header line"), encoding="utf-8")
    assert load_baseline(path) == {"a/file.py": 1, "b/file.py": 2}


def test_render_is_sorted_with_comment_header() -> None:
    """The body is comment-prefixed header lines followed by path/count lines sorted by path."""
    body = render_baseline({"z.py": 1, "a.py": 1}, "first\nsecond")
    lines = body.splitlines()
    assert lines[0] == "# first"
    assert lines[1] == "# second"
    assert lines[2:] == ["a.py\t1", "z.py\t1"]


def test_load_missing_file_is_empty(tmp_path: Path) -> None:
    """A missing baseline file yields an empty mapping rather than raising."""
    assert load_baseline(tmp_path / "does_not_exist.txt") == {}


def test_load_tolerates_bare_paths(tmp_path: Path) -> None:
    """A line without a tab is treated as a single grandfathered violation."""
    path = tmp_path / "baseline.txt"
    path.write_text("# comment\n\nbare/path.py\nwith/tab.py\t3\n", encoding="utf-8")
    assert load_baseline(path) == {"bare/path.py": 1, "with/tab.py": 3}


def test_diff_reports_regressions_and_improvements() -> None:
    """diff_baseline flags files above their baseline and those that improved below it."""
    current = {"new.py": 1, "worse.py": 3, "same.py": 2, "better.py": 1}
    baseline = {"worse.py": 2, "same.py": 2, "better.py": 4, "fixed.py": 1}
    regressions, improvements = diff_baseline(current, baseline)
    assert regressions == {"new.py": 1, "worse.py": 3}
    assert improvements == {"better.py": 4, "fixed.py": 1}
