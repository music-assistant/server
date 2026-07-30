"""
Shared helpers for baseline-aware Music Assistant lint checks.

Some rules (oversized provider icons, naive ``datetime`` usage, blocking IO in async code) already
have pre-existing violations in the tree. Rather than block the introduction of the check on a
repo-wide cleanup, each such check records the *current* violations in a baseline file and only
fails on **new** regressions. The follow-up cleanup then shrinks the baseline over time.

A baseline file is a sorted list of tab-separated ``<repo-relative-path>`` and ``<count>`` lines
(``count`` = the number of currently-grandfathered violations in that file); ``#`` comment and blank
lines are ignored, and a bare path without a tab implies a count of ``1``. Regenerate one with the
owning check's ``--update-baseline`` flag.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path


def load_baseline(path: Path) -> dict[str, int]:
    """
    Return the allowed per-file violation counts recorded in a baseline file.

    :param path: Path to the baseline file. A missing file yields an empty mapping.
    """
    counts: dict[str, int] = {}
    if not path.is_file():
        return counts
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        relpath, separator, count = line.rpartition("\t")
        if separator:
            counts[relpath] = int(count)
        else:
            counts[line] = 1
    return counts


def render_baseline(counts: Mapping[str, int], header: str) -> str:
    """
    Render a baseline file body from per-file violation counts.

    :param counts: Mapping of repo-relative path to its grandfathered violation count.
    :param header: Explanatory text written as leading ``#`` comment lines.
    """
    lines = [f"# {comment_line}" for comment_line in header.strip().splitlines()]
    lines.extend(f"{path}\t{counts[path]}" for path in sorted(counts) if counts[path] > 0)
    return "\n".join(lines) + "\n"


def diff_baseline(
    current: Mapping[str, int], baseline: Mapping[str, int]
) -> tuple[dict[str, int], dict[str, int]]:
    """
    Compare current violation counts against the baseline.

    :param current: Mapping of repo-relative path to its current violation count.
    :param baseline: Mapping of repo-relative path to its grandfathered violation count.
    :return: ``(regressions, improvements)`` where ``regressions`` maps each path whose current
        count exceeds its baseline to that current count, and ``improvements`` maps each path whose
        current count dropped below its baseline to that (now stale) baseline count.
    """
    regressions = {path: count for path, count in current.items() if count > baseline.get(path, 0)}
    improvements = {
        path: allowed for path, allowed in baseline.items() if current.get(path, 0) < allowed
    }
    return regressions, improvements
