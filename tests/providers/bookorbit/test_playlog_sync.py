"""Tests for BookOrbit playlog sync helpers."""

from __future__ import annotations

import pytest  # noqa: TC002

from music_assistant.providers.bookorbit.playlog_sync import InboundProgressGuard


def test_inbound_guard_blocks_immediately_after_outbound() -> None:
    """Inbound apply is blocked right after an outbound progress write."""
    guard = InboundProgressGuard(min_interval_ms=8_000)
    guard.note_outbound(42)
    assert guard.allow_inbound(42) is False


def test_inbound_guard_allows_after_interval(monkeypatch: pytest.MonkeyPatch) -> None:
    """Inbound apply is allowed once the cool-down interval has elapsed."""
    guard = InboundProgressGuard(min_interval_ms=8_000)
    now = {"ms": 1_000_000}

    def fake_time_ms() -> int:
        return now["ms"]

    monkeypatch.setattr(guard, "_now_ms", fake_time_ms)
    guard.note_outbound(7)
    now["ms"] = 1_000_000 + 8_000
    assert guard.allow_inbound(7) is True


def test_inbound_guard_allows_unknown_book() -> None:
    """Books with no recent outbound write always allow inbound apply."""
    assert InboundProgressGuard().allow_inbound(99) is True
