"""
Tests for how BluOS transport states map onto playback states.

BluOS reports 'connecting' while it (re)fills its buffer, which includes the stretch where
it plays out the tail of a stream that stopped sending. Reporting that as idle ends the
queue while the player is still making sound, so a running stream stays 'playing' until
BluOS reports a real stop.
"""

from __future__ import annotations

from typing import cast
from unittest.mock import MagicMock, patch

import pytest
from music_assistant_models.enums import PlaybackState

from music_assistant.providers.bluesound.const import MAX_CONNECTING_TIME, PLAYBACK_POLL_INTERVAL
from music_assistant.providers.bluesound.player import BluesoundPlayer


def _resolve(bluos_state: str, current_state: PlaybackState) -> PlaybackState:
    """Resolve the playback state for a BluOS state reported on top of a current state."""
    fake = MagicMock()
    fake.status.state = bluos_state
    fake._attr_playback_state = current_state
    fake._connecting_since = None
    return BluesoundPlayer._resolve_playback_state(cast("BluesoundPlayer", fake))


def _playing() -> MagicMock:
    """Create a player that is playing and has not reported 'connecting' yet."""
    fake = MagicMock()
    fake._attr_playback_state = PlaybackState.PLAYING
    fake._connecting_since = None
    return fake


def _poll(fake: MagicMock, bluos_state: str, now: float = 0.0) -> PlaybackState:
    """Report one poll of the given BluOS state at time `now` and feed the result back."""
    fake.status.state = bluos_state
    with patch("music_assistant.providers.bluesound.player.time.monotonic", return_value=now):
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


@pytest.mark.parametrize("interval", [PLAYBACK_POLL_INTERVAL, 0.5], ids=["playback", "fast"])
def test_a_player_stuck_connecting_ends_up_idle(interval: float) -> None:
    """A player that never resumes must be reported idle so playback can recover."""
    fake = _playing()
    times = [n * interval for n in range(int(MAX_CONNECTING_TIME / interval) + 1)]

    states = [_poll(fake, "connecting", now) for now in times]

    assert states[:-1] == [PlaybackState.PLAYING] * (len(times) - 1)
    assert states[-1] == PlaybackState.IDLE


def test_slow_connect_after_a_command_stays_playing() -> None:
    """A player that takes a few seconds to connect is not stopped by the fast polls."""
    fake = _playing()

    # a NAD C700 connects for 2 to 4 seconds; MA polls every 0.5 s after a command
    states = [_poll(fake, "connecting", n * 0.5) for n in range(9)]

    assert states == [PlaybackState.PLAYING] * 9
    assert _poll(fake, "stream", 4.5) == PlaybackState.PLAYING


def test_resumed_playback_clears_the_connecting_grace() -> None:
    """Buffering that resolves must not spend the grace of a later interruption."""
    fake = _playing()

    assert _poll(fake, "connecting", 0) == PlaybackState.PLAYING
    assert _poll(fake, "stream", MAX_CONNECTING_TIME - 1) == PlaybackState.PLAYING
    assert fake._connecting_since is None
    # the full grace is available again
    start = MAX_CONNECTING_TIME
    for now in (start, start + MAX_CONNECTING_TIME - 1):
        assert _poll(fake, "connecting", now) == PlaybackState.PLAYING
