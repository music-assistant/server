"""Tests for the recovery of playback that an Alice request interrupted."""

from __future__ import annotations

from music_assistant.providers.yandex_station.voice import VoiceRecovery, VoiceRecoveryPhase


def _awaiting(volume: int = 40) -> VoiceRecovery:
    recovery = VoiceRecovery()
    recovery.begin("queue_1", volume)
    return recovery


def test_answered_request_schedules_the_resume() -> None:
    """Alice answering means the music paused only for a moment."""
    recovery = _awaiting()

    assert recovery.request_ended(spoke=True, volume=40) is True
    assert recovery.phase == VoiceRecoveryPhase.SCHEDULED
    assert recovery.holds_playback is True


def test_silent_request_ends_the_recovery() -> None:
    """A request Alice did not answer ("Alice, stop") leaves the music stopped."""
    recovery = _awaiting()

    assert recovery.request_ended(spoke=False, volume=40) is False
    assert recovery.phase == VoiceRecoveryPhase.NONE
    assert recovery.holds_playback is False


def test_volume_change_schedules_the_resume() -> None:
    """A silent request that changed the volume ("Alice, louder") resumes."""
    recovery = _awaiting(volume=40)

    assert recovery.request_ended(spoke=False, volume=50) is True


def test_new_request_takes_over_with_its_own_volume_baseline() -> None:
    """
    A silent stop after a volume-changing request does not resume.

    The new request is measured from the volume it started at, not from the
    first request's baseline.
    """
    recovery = _awaiting(volume=40)
    recovery.request_ended(spoke=False, volume=50)

    taken_over = recovery.request_started(volume=50)
    phase_after_takeover = recovery.phase
    resumes = recovery.request_ended(spoke=False, volume=50)

    assert (taken_over, phase_after_takeover) == (True, VoiceRecoveryPhase.AWAITING_ANSWER)
    assert (resumes, recovery.phase) == (False, VoiceRecoveryPhase.NONE)


def test_request_without_a_scheduled_resume_takes_nothing_over() -> None:
    """A request with no scheduled resume leaves nothing to take over."""
    recovery = VoiceRecovery()

    assert recovery.request_started(volume=40) is False
    assert recovery.phase == VoiceRecoveryPhase.NONE


def test_resume_runs_for_the_owning_queue() -> None:
    """The resume returns the queue that owned the playback, then ends."""
    recovery = _awaiting()
    recovery.request_ended(spoke=True, volume=40)

    source = recovery.start_resume()
    phase_while_resuming = recovery.phase
    holds_while_resuming = recovery.holds_playback
    recovery.finish_resume()

    assert source == "queue_1"
    assert (phase_while_resuming, holds_while_resuming) == (VoiceRecoveryPhase.RESUMING, False)
    assert recovery.phase == VoiceRecoveryPhase.NONE


def test_cancel_before_the_resume_runs_drops_it() -> None:
    """Cancelling before the timer fires leaves nothing to resume."""
    recovery = _awaiting()
    recovery.request_ended(spoke=True, volume=40)

    recovery.cancel()

    assert recovery.phase == VoiceRecoveryPhase.NONE
    assert recovery.start_resume() is None


def test_cancel_while_resuming_marks_the_stream_superseded() -> None:
    """A running resume cannot be recalled; the stream it starts is dropped instead."""
    recovery = _awaiting()
    recovery.request_ended(spoke=True, volume=40)
    recovery.start_resume()

    before_cancel = recovery.drops_resumed_stream
    recovery.cancel()
    after_cancel = recovery.drops_resumed_stream
    recovery.finish_resume()

    assert (before_cancel, after_cancel, recovery.drops_resumed_stream) == (False, True, False)
    assert recovery.phase == VoiceRecoveryPhase.NONE
