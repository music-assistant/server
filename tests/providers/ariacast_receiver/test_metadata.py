"""Tests for the metadata the AriaCast receiver exchanges with its senders."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
from music_assistant_models.streamdetails import StreamMetadata

from music_assistant.providers.ariacast_receiver import AriaCastReceiver


def _meta_dict(elapsed_time: int | None) -> dict[str, Any]:
    """Serialise the outgoing wire payload for a stream at the given position."""
    receiver = SimpleNamespace(
        _stream_meta=StreamMetadata(title="Test Track", elapsed_time=elapsed_time),
        _is_playing=True,
    )
    return AriaCastReceiver._meta_dict(cast("AriaCastReceiver", receiver))


async def _apply_meta(data: dict[str, Any]) -> StreamMetadata:
    """Merge an incoming sender update into a stream that already has a position."""
    receiver = SimpleNamespace(
        _stream_meta=StreamMetadata(title="Test Track", elapsed_time=120, duration=240),
        _last_artwork_url=None,
        _broadcast_meta=AsyncMock(),
    )
    await AriaCastReceiver._apply_meta(cast("AriaCastReceiver", receiver), data)
    return cast("StreamMetadata", receiver._stream_meta)


def test_zero_position_is_reported() -> None:
    """The start of a track is sent as position 0, not as 'unknown'."""
    assert _meta_dict(0)["position_ms"] == 0


def test_position_is_reported_in_milliseconds() -> None:
    """A known position is converted from seconds to milliseconds."""
    assert _meta_dict(42)["position_ms"] == 42000


def test_missing_position_stays_none() -> None:
    """A stream without a position reports none."""
    assert _meta_dict(None)["position_ms"] is None


async def test_zero_position_from_sender_is_applied() -> None:
    """A sender rewinding to the start of a track replaces the previous position."""
    assert (await _apply_meta({"positionMs": 0})).elapsed_time == 0


async def test_zero_duration_from_sender_is_applied() -> None:
    """A sender switching to a stream of unknown length clears the previous duration."""
    assert (await _apply_meta({"durationMs": 0})).duration == 0


@pytest.mark.parametrize("key", ["positionMs", "position_ms"])
async def test_position_from_sender_is_applied(key: str) -> None:
    """Senders may use either casing for the position."""
    assert (await _apply_meta({key: 42000})).elapsed_time == 42


async def test_update_without_position_keeps_the_previous_one() -> None:
    """An update that carries no position leaves the known position alone."""
    assert (await _apply_meta({"title": "Other Track"})).elapsed_time == 120
