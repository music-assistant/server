"""
Tests for how BluOS transport states map onto playback states.

BluOS reports 'connecting' while it (re)fills its buffer, which includes the stretch where
it plays out the tail of a stream that stopped sending. Reporting that as idle ends the
queue while the player is still making sound, so a running stream stays 'playing' until
BluOS reports a real stop.
"""

from __future__ import annotations

from typing import cast
from unittest.mock import MagicMock

import pytest
from music_assistant_models.enums import PlaybackState

from music_assistant.providers.bluesound.const import MAX_CONNECTING_POLLS
from music_assistant.providers.bluesound.player import BluesoundPlayer


def _resolve(bluos_state: str, current_state: PlaybackState) -> PlaybackState:
    """Resolve the playback state for a BluOS state reported on top of a current state."""
    fake = MagicMock()
    fake.status.state = bluos_state
    fake._attr_playback_state = current_state
    fake._connecting_polls = 0
    return BluesoundPlayer._resolve_playback_state(cast("BluesoundPlayer", fake))


def _poll(fake: MagicMock, bluos_state: str) -> PlaybackState:
    """Report one poll of the given BluOS state and feed the result back as current state."""
    fake.status.state = bluos_state
    state = BluesoundPlayer._resolve_playback_state(cast("BluesoundPlayer", fake))
    fake._attr_playback_state = state
    return state


def test_connecting_while_playing_stays_playing() -> None:
    """A buffering stream must not be mistaken for the end of the queue."""
    assert _resolve("connecting", PlaybackState.PLAYING) == PlaybackState.PLAYING


@pytest.mark.parametrize("current", [PlaybackState.IDLE, PlaybackState.PAUSED])
def test_connecting_outside_playback_is_idle(current: PlaybackState) -> None:
    """Connecting from a standstill is the device starting up, not playback."""
    assert _resolve("connecting", current) == PlaybackState.IDLE


@pytest.mark.parametrize(
    ("bluos_state", "expected"),
    [
        ("play", PlaybackState.PLAYING),
        ("stream", PlaybackState.PLAYING),
        ("stop", PlaybackState.IDLE),
        ("pause", PlaybackState.PAUSED),
    ],
)
def test_reported_states_are_mapped(bluos_state: str, expected: PlaybackState) -> None:
    """Every other BluOS state maps straight through, including a stop while playing."""
    assert _resolve(bluos_state, PlaybackState.PLAYING) == expected


def test_a_player_stuck_connecting_ends_up_idle() -> None:
    """A player that never resumes must be reported idle so playback can recover."""
    fake = MagicMock()
    fake._attr_playback_state = PlaybackState.PLAYING
    fake._connecting_polls = 0

    states = [_poll(fake, "connecting") for _ in range(MAX_CONNECTING_POLLS + 1)]

    assert states[:-1] == [PlaybackState.PLAYING] * MAX_CONNECTING_POLLS
    assert states[-1] == PlaybackState.IDLE


def test_resumed_playback_clears_the_connecting_grace() -> None:
    """Buffering that resolves must not spend the grace of a later interruption."""
    fake = MagicMock()
    fake._attr_playback_state = PlaybackState.PLAYING
    fake._connecting_polls = 0

    assert _poll(fake, "connecting") == PlaybackState.PLAYING
    assert _poll(fake, "stream") == PlaybackState.PLAYING
    assert fake._connecting_polls == 0
    # the full grace is available again
    for _ in range(MAX_CONNECTING_POLLS):
        assert _poll(fake, "connecting") == PlaybackState.PLAYING
