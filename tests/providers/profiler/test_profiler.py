"""Tests for the Profiler plugin provider."""

from __future__ import annotations

import logging
import tracemalloc
from pathlib import Path
from typing import TYPE_CHECKING
from unittest import mock

import pytest
import yappi

from music_assistant.providers.profiler import provider as provider_module
from music_assistant.providers.profiler.helpers import (
    LogErrorCounter,
    collect_object_census,
    render_markdown,
    sanitize_code_path,
)
from music_assistant.providers.profiler.provider import (
    CONF_TRACEMALLOC_ENABLED,
    ProfilerProvider,
)

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant


@pytest.fixture
async def profiler(mass: MusicAssistant) -> ProfilerProvider:
    """Load the profiler provider into a running Music Assistant instance."""
    await mass.config._create_provider_instance("profiler", {})
    provider = mass.get_provider("profiler", provider_type=ProfilerProvider)
    assert provider is not None
    await provider.initialized.wait()
    return provider


async def test_report_shape(profiler: ProfilerProvider) -> None:
    """Test that the report contains all sections with sane, bounded content."""
    report = await profiler.get_report()
    assert isinstance(report, dict)
    for section in (
        "server",
        "config_summary",
        "memory",
        "event_loop",
        "asyncio_tasks",
        "events",
        "log_errors",
        "flight_recorder",
    ):
        assert section in report, f"missing section: {section}"
    assert report["report_format_version"] == 1
    assert report["server"]["uptime_s"] >= 0
    assert report["memory"]["rss_mb"] > 0
    assert report["memory"]["asyncio_tasks"] > 0
    assert "library_counts" in report["config_summary"]
    assert report["asyncio_tasks"]["total"] > 0
    assert len(report["asyncio_tasks"]["top_by_location"]) <= 50
    assert len(report["events"]["per_type_top"]) <= 30
    assert report["flight_recorder"]["window_minutes"] == 30
    # no CPU profile window has completed yet
    assert report["cpu_profile"] is None
    # report files are persisted in the profiler storage dir
    out_files = {path.name for path in Path(profiler._out_dir).iterdir()}
    assert {"report.json", "report.md"} <= out_files


async def test_report_markdown(profiler: ProfilerProvider) -> None:
    """Test that the markdown rendering of the report is returned as text."""
    report_md = await profiler.get_report(markdown=True)
    assert isinstance(report_md, str)
    assert report_md.startswith("# Music Assistant profiler report")
    assert "## memory" in report_md


async def test_object_census(profiler: ProfilerProvider) -> None:
    """Test that the object census is only included on request and bounded."""
    report = await profiler.get_report()
    assert isinstance(report, dict)
    assert "object_census_top" not in report["memory"]
    report = await profiler.get_report(include_object_census=True)
    assert isinstance(report, dict)
    census = report["memory"]["object_census_top"]
    assert 0 < len(census) <= 30
    assert set(census[0]) == {"type", "count"}


async def test_cpu_profile_window(
    profiler: ProfilerProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Test that a CPU profile window captures results and stops yappi."""
    monkeypatch.setattr(provider_module, "CPU_PROFILE_MIN_DURATION", 1)
    with mock.patch.object(profiler, "get_config_value", return_value=1):
        await profiler._run_cpu_profile_window()
    assert not yappi.is_running()
    result = profiler._last_cpu_profile
    assert result is not None
    assert result["clock_type"] == "cpu"
    assert result["top_functions"]
    assert len(result["top_functions"]) <= 40
    assert {"name", "location", "ncall", "tsub_s", "ttot_s", "tavg_ms"} == set(
        result["top_functions"][0]
    )
    # the pstats file was written for offline analysis
    out_files = {path.name for path in Path(profiler._out_dir).iterdir()}
    assert result["pstats_file"] in out_files
    report = await profiler.get_report()
    assert isinstance(report, dict)
    assert report["cpu_profile"] == result


async def test_measurement_tasks_running(profiler: ProfilerProvider) -> None:
    """Test that the continuous measurement tasks are tracked in mass."""
    mass = profiler.mass
    assert "profiler_lag_monitor" in mass._tracked_tasks
    assert "profiler_flight_recorder" in mass._tracked_tasks
    assert "profiler_cpu_scheduler" in mass._tracked_tasks
    assert "profiler/report" in mass.command_handlers


async def test_unload_cleans_up(profiler: ProfilerProvider) -> None:
    """Test that unload cancels all tasks and removes all hooks."""
    mass = profiler.mass
    log_counter = profiler._log_counter
    await mass.unload_provider(profiler.instance_id)
    assert not any(task_id.startswith("profiler_") for task_id in mass._tracked_tasks)
    assert "profiler/report" not in mass.command_handlers
    assert log_counter not in logging.getLogger().handlers
    assert not yappi.is_running()


async def test_tracemalloc_lifecycle(mass: MusicAssistant) -> None:
    """Test that tracemalloc is started/stopped with the provider when enabled."""
    was_tracing = tracemalloc.is_tracing()
    await mass.config._create_provider_instance("profiler", {CONF_TRACEMALLOC_ENABLED: True})
    provider = mass.get_provider("profiler", provider_type=ProfilerProvider)
    assert provider is not None
    await provider.initialized.wait()
    assert tracemalloc.is_tracing()
    report = await provider.get_report()
    assert isinstance(report, dict)
    tm_stats = report["memory"]["tracemalloc"]
    assert tm_stats["top_allocation_sites"]
    assert len(tm_stats["top_allocation_sites"]) <= 30
    await mass.unload_provider(provider.instance_id)
    assert tracemalloc.is_tracing() == was_tracing


def test_sanitize_code_path() -> None:
    """Test that code paths are stripped of user-specific parts."""
    assert (
        sanitize_code_path("/home/user/.venv/lib/python3.14/site-packages/aiohttp/web.py")
        == "aiohttp/web.py"
    )
    assert (
        sanitize_code_path("/Users/someone/repo/music_assistant/mass.py")
        == "music_assistant/mass.py"
    )
    assert sanitize_code_path("/usr/local/lib/python3.14/asyncio/tasks.py").startswith("python3.14")
    assert sanitize_code_path("<frozen importlib._bootstrap>") == "<frozen importlib._bootstrap>"


def test_log_error_counter() -> None:
    """Test that the log counter aggregates without storing message content."""
    counter = LogErrorCounter()
    logger = logging.getLogger("test.profiler.dummy")
    record = logger.makeRecord(
        "test.profiler.dummy",
        logging.ERROR,
        "/app/music_assistant/mass.py",
        1,
        "secret %s",
        ("arg",),
        None,
    )
    counter.emit(record)
    counter.emit(record)
    # records are keyed by code location: logger names may embed user-set names or device ids
    record_with_id = logger.makeRecord(
        "music_assistant.Kitchen Sonos",
        logging.WARNING,
        "/app/music_assistant/providers/sonos/player.py",
        42,
        "msg",
        (),
        None,
    )
    counter.emit(record_with_id)
    summary = counter.summarize()
    assert summary["total_since_load"] == 3
    assert summary["top"][0]["source"] == "music_assistant/mass.py:1"
    assert summary["top"][0]["count"] == 2
    assert summary["top"][1]["source"] == "music_assistant/providers/sonos/player.py:42"
    assert "secret" not in str(summary)
    assert "Kitchen" not in str(summary)


def test_render_markdown() -> None:
    """Test the markdown renderer with nested sections and tables."""
    report = {
        "report_format_version": 1,
        "server": {"version": "x", "nested": {"a": 1}},
        "memory": {"rss_mb": 1.0, "sites": [{"location": "a.py:1", "size_kb": 2}]},
    }
    text = render_markdown(report)
    assert "# Music Assistant profiler report" in text
    assert "## server" in text
    assert "| location | size_kb |" in text


def test_object_census_bounds() -> None:
    """Test the census helper directly for bounds."""
    census = collect_object_census(top_n=5)
    assert len(census) == 5
    assert all(entry["count"] > 0 for entry in census)
