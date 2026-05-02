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


@pytest.mark.asyncio
async def test_all_attempts_exhausted_raises_with_honest_message(
    fake_provider, fake_snapserver: "FakeSnapserver"
):
    """When all 50 retries fail, the error must reference 'after retries' or
    similar — not 'No free port found' which lies about the cause.
    """
    # Queue 51 errors so even after 50 attempts the loop hits the raise
    for _ in range(51):
        fake_snapserver.queue_other_error("Some persistent error")

    stream = _make_stream(fake_provider)

    with pytest.raises(RuntimeError) as exc_info:
        await stream._register_tcp_server_source()

    message = str(exc_info.value)
    assert "No free port" not in message, (
        f"Error message still claims port shortage when the real cause may differ: {message}"
    )
    assert "attempts" in message.lower() or "register" in message.lower()


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        ({"code": -32603, "data": 'Stream with name "x" already exists', "message": "Internal error"}, True),
        ({"code": -32603, "data": "Stream with name \"abc\" already exists in registry", "message": "Internal error"}, True),
        # negative cases — these must NOT match
        ({"code": -32603, "data": "bind: Address already in use", "message": "Internal error"}, False),
        ({"code": -32603, "data": "Some other error already exists somewhere", "message": "Internal error"}, False),
        ({}, False),
        (None, False),
        ("plain string", False),
        ({"data": 12345}, False),
    ],
)
def test_is_name_collision_error_matches_only_specific_pattern(result, expected):
    """_is_name_collision_error must NOT false-positive on unrelated errors."""
    from music_assistant.providers.snapcast.ma_stream import SnapcastMAStream

    assert SnapcastMAStream._is_name_collision_error(result) is expected
