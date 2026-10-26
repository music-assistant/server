"""Tests for the Ynison AudioSource playback session."""

from __future__ import annotations

import asyncio

import pytest

from music_assistant.providers.yandex_ynison.playback_session import (
    ECHO_GRACE_PERIOD,
    PlaybackSession,
    RestartReason,
    StopReason,
)


class _Clock:
    """Controllable monotonic clock."""

    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def _claim_of(session: PlaybackSession) -> tuple[str | None, str | None]:
    """Return the owner and session id of a session's claim."""
    return session.owner_player_id, session.session_id


@pytest.fixture
def clock() -> _Clock:
    """Create a controllable clock."""
    return _Clock()


@pytest.fixture
def session(clock: _Clock) -> PlaybackSession:
    """Create an idle playback session."""
    return PlaybackSession(clock=clock)


def test_claim_and_stale_release(session: PlaybackSession) -> None:
    """A teardown from a superseded request does not release the live claim."""
    session.claim("owner", "bridge", "s1")
    session.claim("owner", "bridge", "s2")

    session.release("owner", "s1")
    assert session.owner_player_id == "owner"

    session.release("owner", "s2")
    assert _claim_of(session) == (None, None)


def test_capture_detects_superseding_session(session: PlaybackSession) -> None:
    """A generator's captured claim is lost once the owner or session id changes."""
    session.claim("owner", "owner", "s1")
    token = session.capture()

    assert not session.lost(token)
    session.claim("owner", "owner", "s2")
    assert session.lost(token)


def test_preload_capture_without_claim_is_never_lost(session: PlaybackSession) -> None:
    """A preload generator that started without a claim is not bound to one."""
    token = session.capture()
    session.claim("owner", "owner", "s1")

    assert not session.lost(token)
    session.release_if_current(token)
    assert session.owner_player_id == "owner"


def test_release_if_current_keeps_superseding_claim(session: PlaybackSession) -> None:
    """The old generator's teardown cannot clear a newer claim."""
    session.claim("owner", "owner", "s1")
    old = session.capture()
    session.claim("owner", "owner", "s2")

    session.release_if_current(old)
    assert session.owner_player_id == "owner"

    session.release_if_current(session.capture())
    assert session.owner_player_id is None


def test_restart_carries_position_reason_and_grace(session: PlaybackSession, clock: _Clock) -> None:
    """A restart request records where and why the stream restarts."""
    session.request_restart(42000, RestartReason.SEEK)

    assert session.restart_requested()
    assert session.in_grace()
    clock.now += ECHO_GRACE_PERIOD + 0.1
    assert not session.in_grace()
    assert session.take_start() == (42000, RestartReason.SEEK)
    assert session.take_start() == (0, None)


def test_repeat_restart_has_no_grace(session: PlaybackSession) -> None:
    """A repeat restart does not suppress seek detection."""
    session.request_restart(0, RestartReason.REPEAT, grace=False)

    assert session.restart_requested()
    assert not session.in_grace()


def test_seed_start_only_without_pending_position(session: PlaybackSession) -> None:
    """A controller seek position seeds the start unless Ynison already set one."""
    session.seed_start(5000)
    assert session.take_start() == (5000, RestartReason.RESUME)

    session.request_restart(7000, RestartReason.SEEK)
    session.seed_start(5000)
    assert session.take_start() == (7000, RestartReason.SEEK)


async def test_wait_restart_wakes_on_request(session: PlaybackSession) -> None:
    """Waiters wake when a restart is requested."""
    waiter = asyncio.create_task(session.wait_restart(1.0))
    await asyncio.sleep(0)
    session.request_restart(0, RestartReason.TRACK_CHANGE)

    assert await waiter is True


async def test_wait_restart_times_out(session: PlaybackSession) -> None:
    """Waiting without a request times out."""
    assert await session.wait_restart(0.01) is False


def test_consume_restart_clears_request(session: PlaybackSession) -> None:
    """Consuming the wake-up keeps the requested position."""
    session.request_restart(3000, RestartReason.TRACK_CHANGE)
    session.consume_restart()

    assert not session.restart_requested()
    assert session.take_start() == (3000, RestartReason.TRACK_CHANGE)


def test_stop_and_resume(session: PlaybackSession) -> None:
    """Resume reports whether the stream must be reselected and clears the stop."""
    assert session.resume() is False

    session.request_stop(StopReason.PROVIDER_LOST)
    assert session.stop_requested()
    assert session.stop_reason == StopReason.PROVIDER_LOST
    assert session.resume() is True
    assert not session.stop_requested()


def test_external_pause_survives_stream_start(session: PlaybackSession) -> None:
    """An external pause still forces reselection after a new stream cleared the stop."""
    session.claim("owner", "bridge", "s1")
    session.pause_externally("owner")
    session.begin_stream()

    assert session.consumer_player_id == "owner"
    assert not session.stop_requested()
    assert session.resume() is True
    assert not session.externally_paused


def test_reset_clears_claim_and_progress(session: PlaybackSession) -> None:
    """Clearing the session drops the claim, progress and pause marker and stops streaming."""
    session.claim("owner", "bridge", "s1")
    session.progress_ms = 5000
    session.pause_externally("owner")

    session.reset()

    assert session.owner_player_id is None
    assert session.consumer_player_id is None
    assert session.session_id is None
    assert session.progress_ms == 0
    assert not session.externally_paused
    assert session.stop_reason == StopReason.CLEARED
