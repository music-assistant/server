"""Tests for the Snapcast stream registry."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from music_assistant.providers.snapcast.stream_registry import SnapcastStreamRegistry


def test_stream_registry_resolves_all_supported_references() -> None:
    """The registry should resolve a stream by all supported references."""
    registry = SnapcastStreamRegistry()
    stream: Any = SimpleNamespace(
        stream_name="mass_stream_broadcast",
        stream_id="snap-stream-123",
        stream_display_name="broadcast",
        source_id="source-broadcast",
        queue_id="queue-broadcast",
    )

    registry.register(stream)

    assert registry.resolve("mass_stream_broadcast") is stream
    assert registry.resolve("snap-stream-123") is stream
    assert registry.resolve("broadcast") is stream
    assert registry.resolve("source-broadcast") is stream
    assert registry.resolve("queue-broadcast") is stream
    assert registry.resolve("missing") is None


def test_stream_registry_resolve_all_returns_all_matching_streams() -> None:
    """The registry should return every match when multiple streams share one ref."""
    registry = SnapcastStreamRegistry()
    idle_stream: Any = SimpleNamespace(
        stream_name="mass_idle_broadcast",
        stream_id="broadcast",
        stream_display_name="broadcast",
        source_id="syncgroup-broadcast",
        queue_id=None,
    )
    active_stream: Any = SimpleNamespace(
        stream_name="mass_active_broadcast",
        stream_id="broadcast",
        stream_display_name="broadcast",
        source_id="syncgroup-broadcast",
        queue_id="queue-broadcast",
    )

    registry.register(idle_stream)
    registry.register(active_stream)

    assert registry.resolve_all("broadcast") == (idle_stream, active_stream)
