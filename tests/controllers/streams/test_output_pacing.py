"""Tests for the rate at which a single queue item is handed to a player."""

from __future__ import annotations

from music_assistant.controllers.streams.constants import (
    SINGLE_ITEM_READRATE,
    SINGLE_ITEM_READRATE_INITIAL_BURST,
)


def test_pacing_stays_ahead_of_playback() -> None:
    """
    At or below playback rate a player would underrun as soon as its burst ran out.

    The ceiling may be lowered per player, but never to realtime or slower.
    """
    assert float(SINGLE_ITEM_READRATE) > 1.0


def test_burst_covers_the_start_of_a_track() -> None:
    """Playback starts from the burst, so it has to be worth more than a moment of audio."""
    assert float(SINGLE_ITEM_READRATE_INITIAL_BURST) >= 10
