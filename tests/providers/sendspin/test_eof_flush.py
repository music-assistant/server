"""
Tests for delivering the end of the audio at a natural end of stream.

Resamplers and encoders of the push stream hold the end of their input back until they
are flushed. A member that needs either would otherwise never play the last moment of
the queue.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, MagicMock

from tests.providers.sendspin.test_short_session_elapsed_anchor import _short_session

if TYPE_CHECKING:
    import pytest


def _recording_push_stream(session: object, calls: list[str]) -> MagicMock:
    """Return the push stream of a session, recording its commits, its flush and the drain."""
    push_stream = cast("MagicMock", cast("MagicMock", session)._create_push_stream.return_value)
    commit_timestamps = push_stream.commit_audio.side_effect

    async def commit_audio() -> int:
        calls.append("commit")
        timestamp: int = next(commit_timestamps)
        return timestamp

    push_stream.commit_audio = AsyncMock(side_effect=commit_audio)
    push_stream.flush_audio = AsyncMock(side_effect=lambda: calls.append("flush"))
    return push_stream


async def test_natural_end_flushes_the_push_stream_before_waiting_for_the_drain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The held back end of the audio goes out after the last commit, ahead of the drain."""
    session = _short_session(monkeypatch)
    calls: list[str] = []
    push_stream = _recording_push_stream(session, calls)
    monkeypatch.setattr(
        session, "_wait_for_buffer_drain", AsyncMock(side_effect=lambda: calls.append("drain"))
    )

    await session._run_playback(MagicMock())

    push_stream.flush_audio.assert_awaited_once_with()
    assert "commit" in calls
    assert calls[-2:] == ["flush", "drain"]
    assert "commit" not in calls[calls.index("flush") :]


async def test_superseded_stream_is_not_flushed(monkeypatch: pytest.MonkeyPatch) -> None:
    """A stream that makes way for new playback ends at once, without its held back audio."""
    session = _short_session(monkeypatch)
    calls: list[str] = []
    push_stream = _recording_push_stream(session, calls)
    session._cancel_requested = True

    await session._run_playback(MagicMock())

    push_stream.flush_audio.assert_not_awaited()


async def test_failing_flush_still_ends_the_stream(monkeypatch: pytest.MonkeyPatch) -> None:
    """A flush that fails is logged, and the session still waits for the drain and stops."""
    session = _short_session(monkeypatch)
    push_stream = _recording_push_stream(session, [])
    push_stream.flush_audio = AsyncMock(side_effect=RuntimeError("resampler failed"))

    await session._run_playback(MagicMock())

    cast("MagicMock", session.player.logger.exception).assert_called_once()
    cast("AsyncMock", session._wait_for_buffer_drain).assert_awaited_once_with()
    cast("MagicMock", session._stop_push_stream).assert_called_once()
