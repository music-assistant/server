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


@pytest.mark.asyncio
async def test_real_port_conflict_retries_with_different_port(
    fake_provider, fake_snapserver: "FakeSnapserver"
):
    """Regression: a non-name error (e.g. port already bound) keeps the loop going."""
    fake_snapserver.queue_other_error("bind: Address already in use")
    fake_snapserver.queue_success(stream_id="ok-after-retry")

    stream = _make_stream(fake_provider)
    await stream._register_tcp_server_source()

    assert stream.snap_stream is not None
    assert stream.snap_stream.identifier == "ok-after-retry"
    # Two add_stream calls were made — first failed, second succeeded
    assert len(fake_snapserver.add_stream_calls) == 2
    # The two URIs must use different ports
    port_1 = fake_snapserver.add_stream_calls[0].split("0.0.0.0:")[1].split("?")[0]
    port_2 = fake_snapserver.add_stream_calls[1].split("0.0.0.0:")[1].split("?")[0]
    assert port_1 != port_2
