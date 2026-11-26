"""Tests for the Alice activity tracker, fed with aliceState sequences recorded on a Station."""

from __future__ import annotations

import pytest

from music_assistant.providers.yandex_station.alice import (
    AliceActivity,
    AliceTracker,
    AliceUpdate,
)


def _feed(*states: str) -> list[AliceUpdate]:
    tracker = AliceTracker()
    return [tracker.feed(state) for state in states]


def test_voice_request_is_one_request_until_idle() -> None:
    """A spoken request goes LISTENING, BUSY, NONE (answer), LISTENING, IDLE."""
    updates = _feed("IDLE", "LISTENING", "BUSY", "NONE", "LISTENING", "IDLE")

    assert [u.activity for u in updates] == [
        AliceActivity.IDLE,
        AliceActivity.REQUEST,
        AliceActivity.REQUEST,
        AliceActivity.REQUEST,
        AliceActivity.REQUEST,
        AliceActivity.IDLE,
    ]
    assert [u.started for u in updates] == [False, True, False, False, False, False]
    assert [u.ended for u in updates] == [None, None, None, None, None, AliceActivity.REQUEST]
    assert updates[-1].spoke is True


def test_request_answered_with_speaking_counts_as_spoken() -> None:
    """Older firmware reports the answer as SPEAKING."""
    updates = _feed("LISTENING", "SPEAKING", "IDLE")

    assert updates[-1].ended == AliceActivity.REQUEST
    assert updates[-1].spoke is True


def test_silent_request_is_not_spoken() -> None:
    """A request Alice did not answer ends without speech."""
    updates = _feed("LISTENING", "BUSY", "IDLE")

    assert updates[-1].ended == AliceActivity.REQUEST
    assert updates[-1].spoke is False


def test_server_action_phrase_is_a_phrase_not_a_request() -> None:
    """A phrase spoken through repeat_phrase reports NONE from IDLE."""
    updates = _feed("IDLE", "NONE", "NONE", "IDLE")

    assert [u.activity for u in updates] == [
        AliceActivity.IDLE,
        AliceActivity.PHRASE,
        AliceActivity.PHRASE,
        AliceActivity.IDLE,
    ]
    assert [u.started for u in updates] == [False, True, False, False]
    assert updates[-1].ended == AliceActivity.PHRASE
    assert updates[-1].spoke is False


def test_request_during_a_phrase_becomes_a_request() -> None:
    """Waking Alice while she speaks a phrase starts a request."""
    updates = _feed("NONE", "LISTENING", "NONE", "IDLE")

    assert updates[1].activity == AliceActivity.REQUEST
    assert updates[1].started is True
    assert updates[-1].ended == AliceActivity.REQUEST


def test_spoke_resets_for_the_next_request() -> None:
    """Speech in one request does not carry over to the next."""
    tracker = AliceTracker()
    for state in ("LISTENING", "NONE", "IDLE"):
        tracker.feed(state)
    updates = [tracker.feed(state) for state in ("LISTENING", "BUSY", "IDLE")]

    assert updates[-1].spoke is False


@pytest.mark.parametrize("state", ["", "IDLE", "SOMETHING_NEW"])
def test_idle_like_states_keep_alice_idle(state: str) -> None:
    """Wake-word-less commands and unknown states never start an activity."""
    update = AliceTracker().feed(state)

    assert update == AliceUpdate(AliceActivity.IDLE, started=False, ended=None, spoke=False)
