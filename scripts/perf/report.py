"""
Report rendering and baseline comparison for the performance benchmark suite.

Works on the JSON documents produced by run_benchmark.py. All metrics carry their
unit in the key name (cpu_seconds, rss_mb, p95_ms, payload_kb, ...) and for every
unit in this suite a higher value is worse.
"""

from __future__ import annotations

from typing import Any

# Relative regression threshold applied to every compared metric,
# unless overridden per metric below.
DEFAULT_REL_THRESHOLD = 0.15

# Per-metric relative thresholds, matched on the full dotted metric path first,
# then on the trailing path segment. Values express the allowed relative increase.
PER_METRIC_REL_THRESHOLDS: dict[str, float] = {
    # p95 latency is inherently noisier than the median
    "p95_ms": 0.30,
    # wall times absorb disk/scheduler noise; the cpu_seconds metrics are the signal
    "wall_seconds": 0.30,
    # scheduling jitter; only flag when it explodes
    "max_loop_lag_ms": 1.0,
    # short capture window on small absolute numbers; only flag structural changes
    "streaming.python_cpu_seconds": 0.5,
    "streaming.ffmpeg_cpu_seconds": 0.5,
}

# Absolute floors per unit suffix: a delta smaller than this is never flagged,
# so tiny absolute changes on near-zero metrics don't trip the relative check.
ABS_FLOORS: dict[str, float] = {
    "_ms": 5.0,
    "_seconds": 0.25,
    "_mb": 8.0,
    "_kb": 2.0,
}

# Per-metric absolute floors (matched like the relative overrides), for metrics whose
# baseline sits so close to zero that scheduler jitter dwarfs any relative threshold.
PER_METRIC_ABS_FLOORS: dict[str, float] = {
    # single-digit-ms baselines routinely jitter to ~25ms; real lag problems are 100ms+
    "max_loop_lag_ms": 50.0,
}


def flatten_metrics(scenarios: dict[str, Any]) -> dict[str, float]:
    """Flatten the scenarios tree into {dotted.path: value} for all numeric metrics."""
    flat: dict[str, float] = {}

    def _walk(node: dict[str, Any], prefix: str) -> None:
        for key, value in node.items():
            path = f"{prefix}.{key}" if prefix else key
            if isinstance(value, dict):
                _walk(value, path)
            elif isinstance(value, (int, float)) and not isinstance(value, bool):
                flat[path] = float(value)

    _walk(scenarios, "")
    return flat


def compare_reports(baseline: dict[str, Any], current: dict[str, Any]) -> tuple[list[str], int]:
    """
    Compare two benchmark reports metric-by-metric.

    :param baseline: The baseline report document.
    :param current: The report document to check for regressions.
    :return: Tuple of (human-readable lines, number of regressed metrics).
    """
    base_flat = flatten_metrics(baseline.get("scenarios", {}))
    cur_flat = flatten_metrics(current.get("scenarios", {}))
    lines: list[str] = []
    regressions = 0

    lines.append(f"{'metric':60s} {'baseline':>12s} {'current':>12s} {'delta':>9s}  status")
    for path in sorted(set(base_flat) | set(cur_flat)):
        base_val = base_flat.get(path)
        cur_val = cur_flat.get(path)
        if base_val is None or cur_val is None:
            lines.append(
                f"{path:60s} {_fmt(base_val):>12s} {_fmt(cur_val):>12s} {'-':>9s}  "
                + ("added" if base_val is None else "removed")
            )
            continue
        delta = cur_val - base_val
        rel = delta / base_val if base_val else 0.0
        status = "ok"
        if _is_regression(path, base_val, cur_val):
            status = "REGRESSED"
            regressions += 1
        elif rel <= -DEFAULT_REL_THRESHOLD and abs(delta) >= _abs_floor(path):
            status = "improved"
        rel_str = f"{rel:+.1%}" if base_val else "n/a"
        lines.append(f"{path:60s} {base_val:12.2f} {cur_val:12.2f} {rel_str:>9s}  {status}")

    lines.append("")
    lines.append(
        f"{regressions} regressed metric(s)" if regressions else "no regressions beyond thresholds"
    )
    return lines, regressions


def render_markdown(report: dict[str, Any]) -> str:
    """Render a benchmark report as human-readable markdown tables."""
    out: list[str] = []
    env = report.get("env", {})
    meta = report.get("meta", {})
    mode = "quick" if meta.get("quick") else "full"
    out.append(f"# Music Assistant perf benchmark ({mode})")
    out.append("")
    out.append(
        f"`{env.get('git_sha', '?')[:10]}`{' (dirty)' if env.get('dirty') else ''} · "
        f"python {env.get('python', '?')} · {env.get('cpu', '?')} · "
        f"{meta.get('generated_at', '?')}"
    )

    for name, metrics in report.get("scenarios", {}).items():
        out.append("")
        out.append(f"## {name}")
        out.append("")
        if name == "api_bench":
            out.append("| command | median_ms | p95_ms | payload_kb | items |")
            out.append("|---|---:|---:|---:|---:|")
            for cmd, values in metrics.items():
                out.append(
                    f"| {cmd} | {values['median_ms']} | {values['p95_ms']} "
                    f"| {values['payload_kb']} | {values['items']} |"
                )
            continue
        out.append("| metric | value |")
        out.append("|---|---:|")
        for key, value in metrics.items():
            if isinstance(value, list):
                continue
            out.append(f"| {key} | {value} |")
        if importtime_top := metrics.get("importtime_top"):
            out.append("")
            out.append("Slowest imports (self time):")
            out.append("")
            out.append("| module | self_ms | cumulative_ms |")
            out.append("|---|---:|---:|")
            for row in importtime_top:
                out.append(f"| {row['module']} | {row['self_ms']} | {row['cumulative_ms']} |")

    if yappi_top := report.get("yappi_top"):
        out.append("")
        out.append("## CPU hotspots (yappi, profiled pass)")
        for scenario, rows in yappi_top.items():
            out.append("")
            out.append(f"### {scenario}")
            out.append("")
            out.append("| function | ncall | tsub_ms | ttot_ms |")
            out.append("|---|---:|---:|---:|")
            for row in rows[:5]:
                out.append(
                    f"| {row['name']} | {row['ncall']} | {row['tsub_ms']} | {row['ttot_ms']} |"
                )

    out.append("")
    return "\n".join(out)


def _abs_floor(path: str) -> float:
    """Return the absolute delta floor for a metric based on its unit suffix."""
    if path in PER_METRIC_ABS_FLOORS:
        return PER_METRIC_ABS_FLOORS[path]
    leaf = path.rsplit(".", 1)[-1]
    if leaf in PER_METRIC_ABS_FLOORS:
        return PER_METRIC_ABS_FLOORS[leaf]
    for suffix, floor in ABS_FLOORS.items():
        if path.endswith(suffix):
            return floor
    return 0.0


def _rel_threshold(path: str) -> float:
    """Return the relative regression threshold for a metric path."""
    if path in PER_METRIC_REL_THRESHOLDS:
        return PER_METRIC_REL_THRESHOLDS[path]
    leaf = path.rsplit(".", 1)[-1]
    return PER_METRIC_REL_THRESHOLDS.get(leaf, DEFAULT_REL_THRESHOLD)


def _is_regression(path: str, base_val: float, cur_val: float) -> bool:
    """Whether current exceeds baseline beyond the threshold (higher is always worse)."""
    delta = cur_val - base_val
    if delta <= 0 or abs(delta) < _abs_floor(path):
        return False
    if base_val == 0:
        return True
    return (delta / base_val) > _rel_threshold(path)


def _fmt(value: float | None) -> str:
    """Format an optional numeric value for the comparison table."""
    return "-" if value is None else f"{value:.2f}"
