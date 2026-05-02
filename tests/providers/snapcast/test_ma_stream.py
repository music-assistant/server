"""Tests for music_assistant.providers.snapcast.ma_stream._register_tcp_server_source."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest

from music_assistant.providers.snapcast.ma_stream import SnapcastMAStream

if TYPE_CHECKING:
    from .conftest import FakeSnapserver


def _make_stream(provider, name: str = "Music Assistant - testhash (announcement)") -> SnapcastMAStream:
    """Build a SnapcastMAStream instance directly, bypassing the constructor's
    media handling (we only test the snapserver source registration)."""
    media = MagicMock()
    s = SnapcastMAStream(
        provider=provider,
        media=media,
        stream_name=name,
    )
    return s


@pytest.mark.asyncio
async def test_happy_path_register_succeeds_first_attempt(
    fake_provider, fake_snapserver: "FakeSnapserver"
):
    """Regression: a clean snapserver returns id on first try, MA registers the stream."""
    fake_snapserver.queue_success(stream_id="ok-1")
    stream = _make_stream(fake_provider)

    await stream._register_tcp_server_source()

    assert stream.snap_stream is not None
    assert stream.snap_stream.identifier == "ok-1"
    assert len(fake_snapserver.add_stream_calls) == 1
