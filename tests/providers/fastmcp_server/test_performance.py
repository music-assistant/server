"""Bounded health-performance primitives."""

from music_assistant.providers.fastmcp_server.performance import PerformanceTracker


def test_empty_performance_tracker_reports_zeroes() -> None:
    """Health diagnostics are stable before the first timed operation."""
    assert PerformanceTracker().summary() == {
        "count": 0,
        "sample_count": 0,
        "p50_ms": 0.0,
        "p95_ms": 0.0,
        "max_ms": 0.0,
    }


def test_performance_tracker_is_bounded_and_reports_lifetime_count() -> None:
    """Only the latest samples are retained while count remains cumulative."""
    tracker = PerformanceTracker()
    for value in range(300):
        tracker.record(value)
    assert tracker.summary() == {
        "count": 300,
        "sample_count": 256,
        "p50_ms": 171.0,
        "p95_ms": 287.0,
        "max_ms": 299.0,
    }
