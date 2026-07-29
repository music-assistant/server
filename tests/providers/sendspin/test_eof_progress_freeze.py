"""
Tests for snapshotting progress at a natural end of stream (support#5813).

The group can only resolve the live playback position while its push stream is
up; the group.stop() that follows teardown would otherwise re-emit whatever
anchor was last pushed. Freezing is limited to a clean end of stream - doing it
for a superseded stream would flash a paused position between tracks.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from music_assistant.providers.sendspin.playback import SendspinPlaybackSession


def _session_mock() -> tuple[MagicMock, list[str]]:
    """Create a session mock recording the order of progress freeze vs transport stop."""
    session = MagicMock()
    calls: list[str] = []
    push_stream = MagicMock()
    push_stream.is_stopped = False
    push_stream.stop.side_effect = lambda: calls.append("stream_stop")
    session._push_stream = push_stream
    metadata_role = session.player._metadata_role
    metadata_role.freeze_progress.side_effect = lambda: calls.append("freeze")
    return session, calls


def test_clean_eof_snapshots_progress_before_stopping_transport() -> None:
    """The snapshot must happen while the push stream is still live."""
    session, calls = _session_mock()

    SendspinPlaybackSession._stop_push_stream(session, snapshot_progress=True)

    assert calls == ["freeze", "stream_stop"]


def test_superseded_stream_is_not_snapshotted() -> None:
    """A stream torn down for a transition must not freeze clients at a paused position."""
    session, calls = _session_mock()

    SendspinPlaybackSession._stop_push_stream(session, snapshot_progress=False)

    assert calls == ["stream_stop"]


def test_snapshot_without_metadata_role_still_stops_transport() -> None:
    """A group with no metadata role has nothing to freeze, but must still stop."""
    session, calls = _session_mock()
    session.player._metadata_role = None

    SendspinPlaybackSession._stop_push_stream(session, snapshot_progress=True)

    assert calls == ["stream_stop"]


def test_already_stopped_stream_is_not_snapshotted() -> None:
    """Nothing to freeze or stop once the stream is already down."""
    session, calls = _session_mock()
    session._push_stream.is_stopped = True

    SendspinPlaybackSession._stop_push_stream(session, snapshot_progress=True)

    assert calls == []
