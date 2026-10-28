"""Tests for BookOrbit playlog sync helpers."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest

from music_assistant.providers.bookorbit.playlog_sync import (
    InboundProgressGuard,
    resolve_remote_progress_seconds,
)
from tests.providers.bookorbit.conftest import manifest_from_book


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


@pytest.mark.asyncio
async def test_resolve_prefers_playback_state(book_multifile: dict[str, Any]) -> None:
    """Playback state wins over legacy progress; seconds are floored from ms."""
    client = Mock()
    manifest = manifest_from_book(book_multifile)
    client.get_audiobook_manifest = AsyncMock(return_value=manifest)
    client.get_playback_state = AsyncMock(
        return_value={
            "assetId": manifest["assets"][1]["assetId"],
            "positionMs": 10_000,
            "percentage": 40.0,
            "completed": False,
        }
    )
    client.get_audio_progress = AsyncMock()
    client.get_book = AsyncMock()
    result = await resolve_remote_progress_seconds(client, int(book_multifile["id"]))
    assert result == (False, 110)
    client.get_audio_progress.assert_not_awaited()


@pytest.mark.asyncio
async def test_resolve_falls_back_to_legacy(book_multifile: dict[str, Any]) -> None:
    """Legacy file progress uses manifest playback order for absolute seconds."""
    client = Mock()
    manifest = manifest_from_book(book_multifile)
    client.get_audiobook_manifest = AsyncMock(return_value=manifest)
    client.get_playback_state = AsyncMock(return_value=None)
    client.get_book = AsyncMock(return_value=book_multifile)
    client.get_audio_progress = AsyncMock(
        return_value={
            "currentFileId": 772,
            "positionSeconds": 50.0,
            "percentage": 50.0,
        }
    )
    result = await resolve_remote_progress_seconds(client, int(book_multifile["id"]))
    assert result == (False, 150)


@pytest.mark.asyncio
async def test_resolve_returns_none_when_empty(book_audiobook: dict[str, Any]) -> None:
    """No playback state and no legacy progress yields None."""
    client = Mock()
    client.get_audiobook_manifest = AsyncMock(return_value=manifest_from_book(book_audiobook))
    client.get_playback_state = AsyncMock(return_value=None)
    client.get_book = AsyncMock(return_value=book_audiobook)
    client.get_audio_progress = AsyncMock(return_value=None)
    assert await resolve_remote_progress_seconds(client, int(book_audiobook["id"])) is None
