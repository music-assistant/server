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

from music_assistant.providers.bluesound.player import BluesoundPlayer


def _resolve(bluos_state: str, current_state: PlaybackState) -> PlaybackState:
    """Resolve the playback state for a BluOS state reported on top of a current state."""
    fake = MagicMock()
    fake.status.state = bluos_state
    fake._attr_playback_state = current_state
    return BluesoundPlayer._resolve_playback_state(cast("BluesoundPlayer", fake))


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
