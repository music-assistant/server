"""Unit tests for EventBuffer and e2e tests for debug events tools."""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace
from typing import Any

import pytest
from fastmcp import Client

# Use the ``from music_assistant.providers.fastmcp_server.debug import …`` form (not ``import music_assistant.providers.fastmcp_server.debug.… as``):
# the upstream import-path rewrite only translates ``from music_assistant.providers.fastmcp_server.`` imports, so
# the aliased ``import`` form survives un-rewritten and breaks the bundled tests.
from music_assistant.providers.fastmcp_server.debug import event_buffer as ev_buf
from music_assistant.providers.fastmcp_server.debug.event_buffer import EventBuffer


def _ev(
    event_type: str, object_id: str | None = None, data: dict[str, Any] | None = None
) -> SimpleNamespace:
    return SimpleNamespace(event=event_type, object_id=object_id, data=data or {})


def test_buffer_starts_unsubscribed(mock_mass: Any) -> None:  # noqa: D103
    buf = EventBuffer(mock_mass, capacity=100)
    assert buf.stats().capacity == 100
    assert buf.stats().current_size == 0
    assert buf.stats().subscribed_since is None
    assert mock_mass.subscribe.called is False


def test_buffer_start_subscribes_once(mock_mass: Any) -> None:  # noqa: D103
    buf = EventBuffer(mock_mass, capacity=10)
    buf.start()
    buf.start()  # idempotent
    assert mock_mass.subscribe.call_count == 1
    assert buf.stats().subscribed_since is not None


def test_buffer_stop_idempotent(mock_mass: Any, fake_event_emitter: Any) -> None:  # noqa: D103
    buf = EventBuffer(mock_mass, capacity=10)
    buf.start()
    buf.stop()
    buf.stop()
    assert fake_event_emitter.removed is True


def test_buffer_drops_oldest_at_capacity(mock_mass: Any, fake_event_emitter: Any) -> None:  # noqa: D103
    buf = EventBuffer(mock_mass, capacity=500)
    buf.start()
    for i in range(503):
        fake_event_emitter.emit(_ev("player_updated", object_id=f"p{i}"))
    snap = buf.snapshot(limit=1000)
    assert len(snap) == 500
    stats = buf.stats()
    assert stats.total_seen == 503
    assert stats.dropped == 3
    assert stats.by_type["player_updated"] == 503


def test_snapshot_filters_event_types_and_id(mock_mass: Any, fake_event_emitter: Any) -> None:  # noqa: D103
    buf = EventBuffer(mock_mass, capacity=100)
    buf.start()
    fake_event_emitter.emit(_ev("player_updated", "kitchen"))
    fake_event_emitter.emit(_ev("queue_updated", "kitchen"))
    fake_event_emitter.emit(_ev("player_updated", "lenco"))
    snap = buf.snapshot(limit=100, event_types=["player_updated"], id_filter="kitchen")
    assert len(snap) == 1
    assert snap[0].event_type == "player_updated"
    assert snap[0].object_id == "kitchen"


def test_snapshot_since_seconds_filters_old_events(
    mock_mass: Any, fake_event_emitter: Any, monkeypatch: Any
) -> None:
    """
    Patch the buffer's _now indirection to a controllable clock.

    Project does not ship freezegun and pyproject.toml is templated
    (cannot be hand-edited per CLAUDE.md). The buffer module exposes
    a ``_now()`` helper specifically so tests can replace it without
    touching stdlib datetime.
    """
    clock: list[dt.datetime] = [dt.datetime(2026, 5, 28, 9, 0, 0, tzinfo=dt.UTC)]
    monkeypatch.setattr(ev_buf, "_now", lambda: clock[0])

    buf = EventBuffer(mock_mass, capacity=100)
    buf.start()
    fake_event_emitter.emit(_ev("a"))
    clock[0] = clock[0] + dt.timedelta(seconds=10)
    fake_event_emitter.emit(_ev("b"))
    snap = buf.snapshot(limit=100, since_seconds=2)
    assert [e.event_type for e in snap] == ["b"]


@pytest.mark.asyncio
async def test_e2e_recent_events_returns_filtered_snapshot(mounted_debug_with_events: Any) -> None:
    """Test debug_recent_events tool with filtering."""
    mcp, _buf, emitter = mounted_debug_with_events
    emitter.emit(_ev("player_updated", "kitchen"))
    emitter.emit(_ev("queue_updated", "kitchen"))
    async with Client(mcp) as client:
        result = await client.call_tool(
            "debug_recent_events",
            {"limit": 10, "event_types": ["player_updated"]},
        )
    assert len(result.data.events) == 1
    assert result.data.events[0].event_type == "player_updated"


@pytest.mark.asyncio
async def test_e2e_event_buffer_stats(mounted_debug_with_events: Any) -> None:
    """Test debug_event_buffer_stats tool."""
    mcp, _buf, emitter = mounted_debug_with_events
    emitter.emit(_ev("player_updated", "kitchen"))
    async with Client(mcp) as client:
        result = await client.call_tool("debug_event_buffer_stats", {})
    assert result.data.capacity == 500
    assert result.data.total_seen == 1
    assert result.data.by_type["player_updated"] == 1
