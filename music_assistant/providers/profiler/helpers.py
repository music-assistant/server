"""
Synchronous data collectors and formatting helpers for the Profiler provider.

All functions in this module are plain synchronous code so the provider can run
the (potentially) expensive ones in an executor thread. None of the collected
data contains user-specific information: only code identifiers (function names,
module paths, line numbers), counters and byte/time metrics are captured so the
resulting report is safe to share publicly.
"""

from __future__ import annotations

import collections
import gc
import json
import logging
import os
import time
import tracemalloc
from pathlib import Path
from typing import TYPE_CHECKING, Any

import psutil
import yappi

from music_assistant.helpers.datetime import utc

if TYPE_CHECKING:
    import asyncio

# fields of a single flight-recorder entry (also the stats.csv column order)
RECORDER_FIELDS = (
    "ts_unix",
    "rss_mb",
    "cpu_pct",
    "loop_lag_avg_ms",
    "loop_lag_max_ms",
    "asyncio_tasks",
    "tracked_tasks",
    "tracked_timers",
    "event_subscribers",
    "ws_clients",
    "ffmpeg_processes",
    "events_per_s",
    "log_errors_per_s",
)

MAX_CSV_SIZE = 4 * 1024 * 1024


class LogErrorCounter(logging.Handler):
    """
    Log handler that counts warnings/errors without storing any message content.

    Only the source code location, log level and exception type are recorded, so
    the aggregated counts are safe to include in a shareable report.
    """

    max_unique_keys = 200

    def __init__(self) -> None:
        """Initialize the counter for WARNING level records and above."""
        super().__init__(level=logging.WARNING)
        self.total = 0
        self.dropped = 0
        self._counts: dict[tuple[str, str, str], int] = {}
        self._last_seen: dict[tuple[str, str, str], int] = {}

    def emit(self, record: logging.LogRecord) -> None:
        """Count the log record by source code location, level and exception type."""
        exc_type = "-"
        if record.exc_info and record.exc_info[0]:
            exc_type = record.exc_info[0].__name__
        # group by code location: logger names may embed user-set names or device ids
        source = f"{sanitize_code_path(record.pathname)}:{record.lineno}"
        key = (source, record.levelname, exc_type)
        self.total += 1
        if key not in self._counts and len(self._counts) >= self.max_unique_keys:
            self.dropped += 1
            return
        self._counts[key] = self._counts.get(key, 0) + 1
        self._last_seen[key] = int(time.time())

    def summarize(self, top_n: int = 50) -> dict[str, Any]:
        """Return the aggregated warning/error counts, largest first."""
        # take a snapshot under the handler lock as emit() may run in other threads
        self.acquire()
        try:
            top_counts = sorted(self._counts.items(), key=lambda kv: kv[1], reverse=True)[:top_n]
            last_seen = dict(self._last_seen)
        finally:
            self.release()
        top = [
            {
                "source": key[0],
                "level": key[1],
                "exception": key[2],
                "count": count,
                "last_seen_ts_unix": last_seen.get(key, 0),
            }
            for key, count in top_counts
        ]
        return {"total_since_load": self.total, "unique_keys_dropped": self.dropped, "top": top}


def sanitize_code_path(path: str) -> str:
    """Reduce a code file path to a package-relative form without user-specific parts."""
    path = path.replace("\\", "/")
    for marker in ("/site-packages/", "/music_assistant/", "/lib/"):
        if (idx := path.rfind(marker)) >= 0:
            return path[idx + (1 if marker == "/music_assistant/" else len(marker)) :]
    return path.rsplit("/", 1)[-1]


def extract_cpu_profile(duration_s: float, out_dir: str, top_n: int = 40) -> dict[str, Any]:
    """
    Convert the stats of a just-stopped yappi run into a result dict and a .pstats file.

    Must be called after ``yappi.stop()``; the collected stats are cleared when done.

    :param duration_s: The actual (wall clock) duration the profile window ran for.
    :param out_dir: Directory to store the .pstats file for offline analysis.
    :param top_n: Maximum number of top functions to include in the result.
    """
    stats = yappi.get_func_stats()
    captured_at = utc()
    pstats_file: str | None = None
    top_functions: list[dict[str, Any]] = []
    if not stats.empty():
        pstats_file = f"cpu_profile_{captured_at.strftime('%Y%m%d_%H%M%S')}.pstats"
        stats.save(os.path.join(out_dir, pstats_file), type="pstat")
        stats.sort("ttot", "desc")
        for stat in stats:
            if len(top_functions) >= top_n:
                break
            top_functions.append(
                {
                    "name": stat.name,
                    "location": f"{sanitize_code_path(stat.module)}:{stat.lineno}",
                    "ncall": stat.ncall,
                    "tsub_s": round(stat.tsub, 4),
                    "ttot_s": round(stat.ttot, 4),
                    "tavg_ms": round(stat.tavg * 1000, 3),
                }
            )
    yappi.clear_stats()
    _prune_files(out_dir, "cpu_profile_", keep=10)
    return {
        "captured_at": captured_at.isoformat(timespec="seconds"),
        "duration_s": round(duration_s, 1),
        "clock_type": "cpu",
        "top_functions": top_functions,
        "pstats_file": pstats_file,
    }


def collect_memory_stats(proc: psutil.Process) -> dict[str, Any]:
    """Collect process-level and garbage-collector memory statistics."""
    mem = proc.memory_info()
    try:
        open_fds: int | None = proc.num_fds()
    except AttributeError, psutil.Error:
        open_fds = None
    return {
        "rss_mb": round(mem.rss / 1024**2, 1),
        "vms_mb": round(mem.vms / 1024**2, 1),
        "num_threads": proc.num_threads(),
        "open_fds": open_fds,
        "gc_enabled": gc.isenabled(),
        "gc_counts": list(gc.get_count()),
        "gc_thresholds": list(gc.get_threshold()),
    }


def collect_object_census(top_n: int = 30) -> list[dict[str, Any]]:
    """
    Count all live Python objects by type, largest counts first.

    This walks the entire object heap and can stall the process for multiple
    seconds on large installations; run it in an executor and only on request.
    """
    counter: collections.Counter[str] = collections.Counter(
        type(obj).__name__ for obj in gc.get_objects()
    )
    return [{"type": name, "count": count} for name, count in counter.most_common(top_n)]


def collect_tracemalloc_stats(
    previous: tracemalloc.Snapshot | None, top_n: int = 30
) -> tuple[dict[str, Any], tracemalloc.Snapshot]:
    """
    Summarize current tracemalloc data and the growth since the previous snapshot.

    :param previous: Snapshot from the previous report to diff against (or None).
    :param top_n: Maximum number of allocation sites to include per list.
    :return: The summary dict plus the new snapshot to diff against next time.
    """
    snapshot = tracemalloc.take_snapshot().filter_traces(
        (tracemalloc.Filter(False, tracemalloc.__file__),)
    )
    traced_current, traced_peak = tracemalloc.get_traced_memory()
    top_sites = []
    for stat in snapshot.statistics("lineno")[:top_n]:
        frame = stat.traceback[0]
        top_sites.append(
            {
                "location": f"{sanitize_code_path(frame.filename)}:{frame.lineno}",
                "size_kb": round(stat.size / 1024, 1),
                "count": stat.count,
            }
        )
    growth = []
    if previous is not None:
        # only report allocation sites that actually grew (compare_to sorts by absolute
        # difference, so large frees would otherwise dominate the list)
        grown = [diff for diff in snapshot.compare_to(previous, "lineno") if diff.size_diff > 0]
        for diff in grown[:top_n]:
            frame = diff.traceback[0]
            growth.append(
                {
                    "location": f"{sanitize_code_path(frame.filename)}:{frame.lineno}",
                    "size_diff_kb": round(diff.size_diff / 1024, 1),
                    "count_diff": diff.count_diff,
                }
            )
    summary = {
        "traced_current_mb": round(traced_current / 1024**2, 1),
        "traced_peak_mb": round(traced_peak / 1024**2, 1),
        "top_allocation_sites": top_sites,
        "growth_since_previous_report": growth,
    }
    return summary, snapshot


def collect_task_stats(tasks: set[asyncio.Task[Any]], top_n: int = 50) -> dict[str, Any]:
    """
    Summarize all asyncio tasks by the code location where they are suspended.

    Only coroutine names and file:line locations are included - never task
    names or arguments, as those may contain user-specific data.
    """
    locations: collections.Counter[str] = collections.Counter()
    for task in tasks:
        coro = task.get_coro()
        name = getattr(coro, "__qualname__", None) or type(coro).__name__
        frame = getattr(coro, "cr_frame", None)
        if frame is not None:
            name = f"{name} @ {sanitize_code_path(frame.f_code.co_filename)}:{frame.f_lineno}"
        locations[name] += 1
    return {
        "total": len(tasks),
        "top_by_location": [
            {"coroutine": name, "count": count} for name, count in locations.most_common(top_n)
        ],
    }


def finalize_recorder_entry(
    proc: psutil.Process, entry: dict[str, Any], csv_path: str
) -> dict[str, Any]:
    """Add process-level metrics to a flight-recorder entry and append it to the stats CSV."""
    mem = proc.memory_info()
    entry["rss_mb"] = round(mem.rss / 1024**2, 1)
    # cpu percent is measured over the interval since the previous sample
    entry["cpu_pct"] = round(proc.cpu_percent(), 1)
    try:
        entry["ffmpeg_processes"] = sum(
            1 for child in proc.children() if "ffmpeg" in _process_name(child)
        )
    except psutil.Error:
        entry["ffmpeg_processes"] = None
    _append_csv_row(csv_path, entry)
    return entry


def persist_report(out_dir: str, report: dict[str, Any]) -> str:
    """Write the report to disk as report.json + report.md and return the markdown version."""
    markdown = render_markdown(report)
    with open(os.path.join(out_dir, "report.json"), "w", encoding="utf-8") as _file:
        json.dump(report, _file, indent=2, default=str)
    with open(os.path.join(out_dir, "report.md"), "w", encoding="utf-8") as _file:
        _file.write(markdown)
    return markdown


def render_markdown(report: dict[str, Any]) -> str:
    """Render the report dict as human-readable markdown for pasting into a chat or issue."""
    lines: list[str] = ["# Music Assistant profiler report", ""]
    lines.extend(
        f"- {key}: {value}" for key, value in report.items() if not isinstance(value, dict | list)
    )
    for key, value in report.items():
        if isinstance(value, dict):
            lines.extend(("", f"## {key}", *_render_section(value, depth=3)))
        elif isinstance(value, list):
            lines.extend(("", f"## {key}", *_md_table(value)))
    return "\n".join(lines) + "\n"


def _render_section(mapping: dict[str, Any], depth: int) -> list[str]:
    """Render a report section as markdown lines (scalars first, then nested content)."""
    lines: list[str] = []
    nested: list[tuple[str, Any]] = []
    for key, value in mapping.items():
        if isinstance(value, dict) or (value and isinstance(value, list)):
            nested.append((key, value))
        else:
            lines.append(f"- {key}: {value}")
    for key, value in nested:
        lines.extend(("", f"{'#' * min(depth, 6)} {key}"))
        if isinstance(value, dict):
            lines.extend(_render_section(value, depth + 1))
        elif value and isinstance(value[0], dict):
            lines.extend(_md_table(value))
        else:
            lines.extend(f"- {item}" for item in value)
    return lines


def _md_table(rows: list[dict[str, Any]]) -> list[str]:
    """Render a list of uniform dicts as a markdown table."""
    if not rows:
        return ["(none)"]
    columns = list(rows[0])
    lines = ["| " + " | ".join(columns) + " |", "|" + "---|" * len(columns)]
    lines.extend(
        "| " + " | ".join(str(row.get(column, "")) for column in columns) + " |" for row in rows
    )
    return lines


def _append_csv_row(csv_path: str, entry: dict[str, Any]) -> None:
    """Append a recorder entry to the stats CSV, restarting the file when it grows too large."""
    path = Path(csv_path)
    write_header = True
    if path.is_file():
        if path.stat().st_size > MAX_CSV_SIZE:
            path.unlink()
        else:
            write_header = False
    with path.open("a", encoding="utf-8") as _file:
        if write_header:
            _file.write(",".join(RECORDER_FIELDS) + "\n")
        _file.write(",".join(str(entry.get(field, "")) for field in RECORDER_FIELDS) + "\n")


def _process_name(proc: psutil.Process) -> str:
    """Return the process name, or an empty string if the process is already gone."""
    try:
        return str(proc.name())
    except psutil.Error:
        return ""


def _prune_files(out_dir: str, prefix: str, keep: int) -> None:
    """Remove the oldest files with the given prefix, keeping only the newest ones."""
    files = sorted(path for path in Path(out_dir).iterdir() if path.name.startswith(prefix))
    for path in files[:-keep]:
        path.unlink()
