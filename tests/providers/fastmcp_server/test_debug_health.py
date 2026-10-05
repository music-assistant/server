"""End-to-end tests for debug_health_summary."""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastmcp import Client, FastMCP

from music_assistant.providers.fastmcp_server.debug import log_reader
from music_assistant.providers.fastmcp_server.tools.debug import build_debug_server


@pytest.fixture
def populated_mass(mock_mass: MagicMock) -> MagicMock:
    """Populate mock_mass with providers and queues."""
    mock_mass.providers = [
        SimpleNamespace(
            instance_id="yandex_music_1",
            domain="yandex_music",
            type=SimpleNamespace(value="music"),
            name="Yandex",
            available=True,
            enabled=True,
            last_error=None,
        ),
        SimpleNamespace(
            instance_id="sonos_1",
            domain="sonos",
            type=SimpleNamespace(value="player"),
            name="Sonos",
            available=False,
            enabled=True,
            last_error="auth expired",
        ),
        SimpleNamespace(
            instance_id="spotify_1",
            domain="spotify",
            type=SimpleNamespace(value="music"),
            name="Spotify",
            available=True,
            enabled=False,
            last_error=None,
        ),
    ]
    mock_mass.player_queues.all = MagicMock(
        return_value=[
            SimpleNamespace(queue_id="kitchen", state="playing", available=True),
            SimpleNamespace(queue_id="lenco", state="idle", available=True),
            SimpleNamespace(queue_id="broken", state="error", available=False),
        ]
    )
    return mock_mass


async def test_health_summary_rolls_up_state(mounted_debug: Any, populated_mass: MagicMock) -> None:  # noqa: ARG001
    """Test that health_summary correctly rolls up provider and queue state."""
    async with Client(mounted_debug) as client:
        result = await client.call_tool("debug_health_summary", {})
    data = result.data
    assert data.providers_loaded == 2  # yandex + spotify
    assert data.providers_disabled == 1  # spotify
    assert data.providers_error == 1  # sonos
    assert any(p.instance_id == "sonos_1" for p in data.providers_error_details)
    assert data.queues_total == 3
    assert data.queues_with_active_playback == 1
    assert data.queues_with_errors >= 1


async def test_health_summary_marks_capabilities_disabled_when_off(
    mounted_debug: Any,
    populated_mass: MagicMock,  # noqa: ARG001
) -> None:
    """Test that disabled capabilities are listed when underlying system unavailable."""
    async with Client(mounted_debug) as client:
        result = await client.call_tool("debug_health_summary", {})
    # mounted_debug has no live EventBuffer and SafeLogTail.ROOT points
    # at $HOME/.musicassistant which doesn't exist in the test environment.
    # Both should report as disabled, NOT crash.
    assert result.data.events_per_min_by_type is None
    assert "DEBUG_EVENTS" in result.data.disabled_capabilities


async def test_health_summary_events_rate_when_buffer_present(
    mounted_debug_with_events: Any,
    populated_mass: MagicMock,  # noqa: ARG001
) -> None:
    """Test that events_per_min_by_type is populated when EventBuffer is active."""
    mcp, _buf, emitter = mounted_debug_with_events
    for _ in range(6):
        emitter.emit(SimpleNamespace(event="player_updated", object_id="kitchen", data={}))
    async with Client(mcp) as client:
        result = await client.call_tool("debug_health_summary", {})
    assert result.data.events_per_min_by_type is not None
    assert "player_updated" in result.data.events_per_min_by_type


async def test_health_summary_counts_recent_log_errors(
    mounted_debug: Any,
    populated_mass: MagicMock,
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Write a synthetic log with current-time ERROR lines, assert count."""
    monkeypatch.setattr(log_reader.SafeLogTail, "ROOT", tmp_path, raising=True)
    # health_summary's SafeLogTail(mass) now reads mass.storage_path first;
    # point it at the same sandbox so the patched ROOT and the runtime
    # resolution agree.
    populated_mass.storage_path = str(tmp_path)
    log_path = tmp_path / "musicassistant.log"
    now = datetime.now().astimezone()
    ts = now.strftime("%Y-%m-%d %H:%M:%S,000")
    log_path.write_text(
        f"{ts} ERROR music_assistant.providers.sonos: failure A\n"
        f"{ts} ERROR music_assistant.providers.sonos: failure B\n"
        f"{ts} INFO music_assistant.mass: not an error\n"
        f"{ts} ERROR music_assistant.controllers.music: failure C\n",
        encoding="utf-8",
    )
    async with Client(mounted_debug) as client:
        result = await client.call_tool("debug_health_summary", {})
    assert result.data.log_errors_last_5min == 3
    assert "DEBUG_LOGS" not in result.data.disabled_capabilities


async def test_health_summary_skips_log_read_when_logs_disabled(
    mock_mass: MagicMock,
    populated_mass: MagicMock,  # noqa: ARG001 -- populates mock_mass providers/queues
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    When DEBUG_LOGS is off, health_summary must not touch the log file at all.

    Reading logs to count errors when the operator disabled log access bypasses
    the permission. The capability is reported as disabled instead.
    """

    def boom(_self: Any, **_kwargs: Any) -> int:
        raise AssertionError("logs must not be read when DEBUG_LOGS is disabled")

    monkeypatch.setattr(log_reader.SafeLogTail, "count_errors_last_5min", boom, raising=True)

    mcp = FastMCP(name="test")
    mcp.mount(
        build_debug_server(mock_mass, require_confirmation=False, logs_enabled=False),
        namespace="debug",
    )
    async with Client(mcp) as client:
        result = await client.call_tool("debug_health_summary", {})
    assert result.data.log_errors_last_5min is None
    assert "DEBUG_LOGS" in result.data.disabled_capabilities
