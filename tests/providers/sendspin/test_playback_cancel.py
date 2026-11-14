"""Tests for Sendspin playback cancellation."""

from __future__ import annotations

import asyncio
from collections import deque
from typing import cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.enums import MediaType

from music_assistant.models.player import PlayerMedia
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


def _line_in_session(monkeypatch: pytest.MonkeyPatch) -> tuple[SendspinPlaybackSession, MagicMock]:
    """Create a session whose server opens a line-in when asked."""
    session = object.__new__(SendspinPlaybackSession)
    session.player = MagicMock()
    session._state_lock = asyncio.Lock()
    session._pipeline_config_cache = {}
    session._push_stream = None
    session._history = deque()
    session._produced_audio_us = 0
    session._timeline_start_us = None
    source_stream = MagicMock()
    source_stream.aclose = AsyncMock()
    session.player.mass.streams.open_audio_source_stream = AsyncMock(return_value=source_stream)
    monkeypatch.setattr(
        session, "_select_session_pcm_formats", MagicMock(side_effect=RuntimeError("stop here"))
    )
    return session, source_stream


def _line_in() -> PlayerMedia:
    return PlayerMedia(uri="x://audio_source/main", media_type=MediaType.AUDIO_SOURCE)


async def test_a_line_in_session_follows_the_source_format(monkeypatch: pytest.MonkeyPatch) -> None:
    """A line-in has no queue item, so its opened stream details set the session format."""
    session, source_stream = _line_in_session(monkeypatch)
    followed: list[object] = []

    async def follow(_player_ids: object) -> None:
        followed.append(session._start_streamdetails)

    monkeypatch.setattr(session, "_follow_session_sample_rate", follow)

    with pytest.raises(RuntimeError):
        await session._run_playback(_line_in())

    assert followed == [source_stream.streamdetails]


async def test_a_line_in_cancelled_during_setup_is_released(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A new selection cancelling setup releases the line-in that was opened for it."""
    session, source_stream = _line_in_session(monkeypatch)
    monkeypatch.setattr(
        session, "_follow_session_sample_rate", AsyncMock(side_effect=asyncio.CancelledError)
    )

    with pytest.raises(asyncio.CancelledError):
        await session._run_playback(_line_in())

    source_stream.aclose.assert_awaited_once_with()
