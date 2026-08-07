"""Tests for the metadata payload the AriaCast receiver sends to its senders."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

from music_assistant_models.streamdetails import StreamMetadata

from music_assistant.providers.ariacast_receiver import AriaCastReceiver


def _meta_dict(elapsed_time: int | None) -> dict[str, Any]:
    """Serialise the wire payload for a stream reporting the given position."""
    receiver = SimpleNamespace(
        _stream_meta=StreamMetadata(title="Test Track", elapsed_time=elapsed_time),
        _is_playing=True,
    )
    return AriaCastReceiver._meta_dict(cast("AriaCastReceiver", receiver))


def test_zero_position_is_reported() -> None:
    """The start of a track is sent as position 0, not as 'unknown'."""
    assert _meta_dict(0)["position_ms"] == 0


def test_position_is_reported_in_milliseconds() -> None:
    """A known position is converted from seconds to milliseconds."""
    assert _meta_dict(42)["position_ms"] == 42000


def test_missing_position_stays_none() -> None:
    """A stream without a position reports none."""
    assert _meta_dict(None)["position_ms"] is None
