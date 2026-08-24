"""Direct tests for the seven native debug read commands."""

from __future__ import annotations

import dataclasses
import threading
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastmcp.exceptions import ToolError

from music_assistant.providers.fastmcp_server.commands.debug import (
    event_buffer_stats,
    health,
    log_stats,
    packages,
    recent_events,
    routes,
    tail_log,
)
from music_assistant.providers.fastmcp_server.debug.event_buffer import EventBuffer
from music_assistant.providers.fastmcp_server.debug.log_reader import SafeLogTail


class _RaisingToDict:
    def to_dict(self) -> dict[str, object]:
        raise RuntimeError("broken to_dict")


class _RaisingModelDump:
    def model_dump(self, *, mode: str) -> dict[str, object]:
        raise RuntimeError("broken model_dump")


@dataclasses.dataclass
class _RaisingDataclass:
    value: str = "unreachable"

    def __getattribute__(self, name: str) -> object:
        if name == "value":
            raise RuntimeError("broken dataclass field")
        return super().__getattribute__(name)


class _RaisingProperty:
    @property
    def to_dict(self) -> object:
        raise RuntimeError("broken property")


class _RaisingStr:
    def __str__(self) -> str:
        raise RuntimeError("broken string conversion")


def _write_log(path: Path) -> None:
    path.joinpath("musicassistant.log").write_text(
        "2026-07-30 10:00:00,000 INFO music_assistant.mass: token=secret-token\n"
        "2026-07-30 10:00:01,000 ERROR music_assistant.streams: first failure\n"
        "2026-07-30 10:00:02,000 ERROR music_assistant.streams: second failure\n",
        encoding="utf-8",
    )


async def test_log_handlers_preserve_redaction_paging_stats_and_worker_thread(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Native log reads retain bounded paging/statistics and avoid the event loop."""
    _write_log(tmp_path)
    mass = SimpleNamespace(storage_path=str(tmp_path))
    main_thread = threading.current_thread()
    seen_threads: list[threading.Thread] = []
    real_tail = SafeLogTail.tail
    real_stats = SafeLogTail.stats

    def recording_tail(self: SafeLogTail, **kwargs: Any) -> Any:
        seen_threads.append(threading.current_thread())
        return real_tail(self, **kwargs)

    def recording_stats(self: SafeLogTail, **kwargs: Any) -> Any:
        seen_threads.append(threading.current_thread())
        return real_stats(self, **kwargs)

    monkeypatch.setattr(SafeLogTail, "tail", recording_tail)
    monkeypatch.setattr(SafeLogTail, "stats", recording_stats)

    page = await tail_log(mass, lines=1)
    redacted = await tail_log(mass, lines=10, search="secret")
    stats = await log_stats(mass)

    assert [line.message for line in page.lines] == ["second failure"]
    assert page.has_more is True
    assert page.next_call_hint is not None
    assert "secret-token" not in redacted.lines[0].message
    assert "<redacted>" in redacted.lines[0].message
    assert stats.total_records == 3
    assert stats.level_counts == {"ERROR": 2, "INFO": 1}
    assert seen_threads
    assert all(thread is not main_thread for thread in seen_threads)


async def test_event_handlers_preserve_limits_and_stats(
    mock_mass: MagicMock, fake_event_emitter: Any
) -> None:
    """Recent events clamp their response while stats retain the full seen count."""
    buffer = EventBuffer(mock_mass, capacity=50)
    buffer.start()
    for index in range(55):
        fake_event_emitter.emit(
            SimpleNamespace(event="player_updated", object_id=str(index), data={})
        )

    snapshot = await recent_events(buffer, limit=5000)
    stats = await event_buffer_stats(buffer)

    assert len(snapshot.events) == 50
    assert snapshot.buffer_capacity == 50
    assert snapshot.total_seen == 55
    assert stats.current_size == 50
    assert stats.dropped == 5
    assert stats.by_type == {"player_updated": 55}


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param(_RaisingToDict(), id="to-dict"),
        pytest.param(_RaisingModelDump(), id="model-dump"),
        pytest.param(_RaisingDataclass(), id="dataclass-field"),
        pytest.param(_RaisingProperty(), id="property"),
        pytest.param({_RaisingStr(): "value"}, id="mapping-key-str"),
    ],
)
async def test_event_callback_isolates_malformed_payloads_and_stays_usable(
    mock_mass: MagicMock,
    fake_event_emitter: Any,
    payload: object,
) -> None:
    """One malformed payload cannot escape the subscriber or poison later events."""
    buffer = EventBuffer(mock_mass, capacity=50)
    buffer.start()

    fake_event_emitter.emit(SimpleNamespace(event="broken", object_id="bad", data=payload))
    fake_event_emitter.emit(SimpleNamespace(event="healthy", object_id="good", data={"ok": True}))

    snapshot = await recent_events(buffer, limit=10)
    stats = await event_buffer_stats(buffer)
    assert [event.data for event in snapshot.events] == [
        "<unserializable event data>",
        {"ok": True},
    ]
    assert stats.total_seen == 2
    assert stats.by_type == {"broken": 1, "healthy": 1}


async def test_event_callback_isolates_broken_attributes_and_string_conversion(
    mock_mass: MagicMock,
    fake_event_emitter: Any,
) -> None:
    """Event metadata getters and string conversion cannot escape the subscriber."""

    class BrokenEvent:
        event_type = "fallback"
        object_id = _RaisingStr()

        @property
        def event(self) -> str:
            raise RuntimeError("broken event property")

        @property
        def data(self) -> object:
            raise RuntimeError("broken data property")

    buffer = EventBuffer(mock_mass, capacity=50)
    buffer.start()

    fake_event_emitter.emit(BrokenEvent())

    snapshot = await recent_events(buffer, limit=10)
    stats = await event_buffer_stats(buffer)
    assert len(snapshot.events) == 1
    assert snapshot.events[0].event_type == "fallback"
    assert snapshot.events[0].object_id == "<unavailable>"
    assert snapshot.events[0].data == "<unserializable event data>"
    assert stats.total_seen == 1
    assert stats.by_type == {"fallback": 1}


async def test_event_handlers_return_bounded_empty_results_without_buffer() -> None:
    """Disabled event capture returns stable response dataclasses."""
    assert (await recent_events(None)).events == []
    assert (await event_buffer_stats(None)).capacity == 0


async def test_health_rolls_up_state_and_respects_disabled_log_access() -> None:
    """Health reports provider/queue failures without reading disabled logs."""
    mass = MagicMock()
    mass.providers = [
        SimpleNamespace(
            instance_id="ok",
            domain="demo",
            type=SimpleNamespace(value="music"),
            name="Demo",
            available=True,
            enabled=True,
            last_error=None,
        ),
        SimpleNamespace(
            instance_id="bad",
            domain="broken",
            type=SimpleNamespace(value="player"),
            name="Broken",
            available=False,
            enabled=False,
            last_error="boom",
        ),
    ]
    mass.player_queues.all.return_value = [
        SimpleNamespace(state="playing", available=True),
        SimpleNamespace(state="error", available=False),
    ]

    result = await health(
        mass,
        buffer=None,
        logs_enabled=False,
        policy_schema_version=2,
        policy_profile="Interactive admin",
        token_resolution_failures=3,
    )

    assert result.providers_loaded == 1
    assert result.providers_disabled == 1
    assert result.providers_error == 1
    assert result.queues_total == 2
    assert result.queues_with_active_playback == 1
    assert result.queues_with_errors == 1
    assert result.log_errors_last_5min is None
    assert result.disabled_capabilities == ["DEBUG_EVENTS", "DEBUG_LOGS"]
    assert result.policy_schema_version == 2
    assert result.policy_profile == "Interactive admin"
    assert result.token_resolution_failures == 3
    assert result.event_buffer_active is False


async def test_health_reports_actual_active_event_buffer_state() -> None:
    """Event-buffer diagnostics follow the live subscription, not policy intent."""
    mass = MagicMock()
    mass.providers = []
    mass.player_queues.all.return_value = []
    buffer = MagicMock()
    buffer.stats.return_value = SimpleNamespace(
        subscribed_since=datetime.now().astimezone().isoformat(),
        by_type={},
    )

    result = await health(
        mass,
        buffer=buffer,
        logs_enabled=False,
        policy_schema_version=2,
        policy_profile="Custom",
        token_resolution_failures=0,
    )

    assert result.event_buffer_active is True


async def test_routes_handles_private_api_absence_and_attributes_known_paths() -> None:
    """Route reads attribute known prefixes and fail clearly on older MA layouts."""
    mass = MagicMock()
    route = SimpleNamespace(method="GET", resource=SimpleNamespace(canonical="/mcp/v1/sse"))
    mass.webserver._server = SimpleNamespace(
        app=SimpleNamespace(router=SimpleNamespace(routes=lambda: [route]))
    )
    result = await routes(mass)
    assert result.routes[0].registered_by == "fastmcp_server"

    mass.webserver._server = None
    with pytest.raises(ToolError, match="routes are unavailable"):
        await routes(mass)


async def test_packages_returns_all_bounded_tracked_versions() -> None:
    """Package diagnostics keep the fixed allowlist rather than enumerating the environment."""
    result = await packages()
    assert set(result.packages) == {
        "music_assistant",
        "music_assistant_models",
        "fastmcp",
        "aiohttp",
        "mashumaro",
    }
    assert all(result.packages.values())


async def test_health_counts_recent_log_errors_off_event_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Enabled health log diagnostics retain the worker-thread scan."""
    now = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S,000")
    tmp_path.joinpath("musicassistant.log").write_text(
        f"{now} ERROR music_assistant.mass: boom\n", encoding="utf-8"
    )
    mass = MagicMock(storage_path=str(tmp_path))
    mass.storage_path = str(tmp_path)
    mass.providers = []
    mass.player_queues.all.return_value = []
    main_thread = threading.current_thread()
    seen: list[threading.Thread] = []
    real_count = SafeLogTail.count_errors_last_5min

    def recording_count(self: SafeLogTail, *, name: str = "musicassistant.log") -> int:
        seen.append(threading.current_thread())
        result: object = real_count(self, name=name)
        assert isinstance(result, int)
        return result

    monkeypatch.setattr(SafeLogTail, "count_errors_last_5min", recording_count)
    result = await health(mass, buffer=None, logs_enabled=True)
    assert result.log_errors_last_5min == 1
    assert seen == [seen[0]]
    assert seen[0] is not main_thread
