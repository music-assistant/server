"""Tests for Sendspin playback cancellation."""

from __future__ import annotations

import asyncio
from collections import deque
from typing import cast
from unittest.mock import MagicMock

import pytest

from music_assistant.providers.sendspin.playback import SendspinPlaybackSession


def _session_mock(
    *, allow_noncompliant: bool
) -> tuple[SendspinPlaybackSession, list[str | tuple[str, bool]]]:
    """Create a session with a live push stream recording the order of teardown calls."""
    session = object.__new__(SendspinPlaybackSession)
    session.player = MagicMock()
    session.player.provider.server_api.allow_noncompliant_clients = allow_noncompliant
    session._cancel_requested = False
    calls: list[str | tuple[str, bool]] = []
    push_stream = MagicMock()
    push_stream.is_stopped = False
    push_stream.clear.side_effect = lambda: calls.append("stream_clear")
    push_stream.stop.side_effect = lambda *, keep_stream: calls.append(("stream_stop", keep_stream))
    session._push_stream = push_stream

    async def playback() -> None:
        try:
            await asyncio.Future()
        finally:
            calls.append("task_cleanup")

    session.playback_task = asyncio.create_task(playback())
    return session, calls


async def test_transition_clears_stream_before_task_cleanup() -> None:
    """A spec-only fleet gets the stream cleared and preserved before task teardown."""
    session, calls = _session_mock(allow_noncompliant=False)
    await asyncio.sleep(0)

    await session.cancel("new media requested", keep_stream=True)

    assert calls == ["stream_clear", ("stream_stop", True), "task_cleanup"]


async def test_transition_leaves_teardown_to_task_while_noncompliant_allowed() -> None:
    """Legacy clients mishandle stream/clear, so cancel leaves the stop to task teardown."""
    session, calls = _session_mock(allow_noncompliant=True)
    await asyncio.sleep(0)

    await session.cancel("new media requested", keep_stream=True)

    assert calls == ["task_cleanup"]


async def test_transition_awaits_task_when_stopping_the_stream_fails() -> None:
    """A stream stop failing on a dead connection must not abort the track change."""
    session, calls = _session_mock(allow_noncompliant=False)
    cast("MagicMock", session._push_stream).stop.side_effect = ConnectionResetError("client gone")
    await asyncio.sleep(0)

    await session.cancel("new media requested", keep_stream=True)

    assert calls == ["stream_clear", "task_cleanup"]
    assert session.playback_task is None


async def test_setup_failure_ends_the_new_stream(monkeypatch: pytest.MonkeyPatch) -> None:
    """A session that fails during setup ends its stream instead of leaving clients hanging."""
    session = object.__new__(SendspinPlaybackSession)
    session.player = MagicMock()
    session._state_lock = asyncio.Lock()
    session._pipeline_config_cache = {}
    session._push_stream = None
    session._history = deque()
    session._produced_audio_us = 0
    session._timeline_start_us = None
    session._first_commit_monotonic_us = None
    push_stream = MagicMock()
    push_stream.set_live_source.side_effect = RuntimeError("no clients")
    monkeypatch.setattr(
        session, "_select_session_pcm_formats", MagicMock(return_value=(MagicMock(), MagicMock()))
    )
    monkeypatch.setattr(session, "_create_push_stream", MagicMock(return_value=push_stream))

    with pytest.raises(RuntimeError):
        await session._run_playback(MagicMock())

    push_stream.stop.assert_called_once_with()
    assert session._push_stream is None
    assert session._playback_running is False
