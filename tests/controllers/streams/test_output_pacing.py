"""Tests for the rate at which stream output is handed to a player."""

from __future__ import annotations

from music_assistant.controllers.streams.constants import (
    BURST_OUTPUT_READRATE,
    BURST_OUTPUT_READRATE_INITIAL_BURST,
    OUTPUT_READRATE,
    OUTPUT_READRATE_INITIAL_BURST,
)


def test_pacing_stays_ahead_of_playback() -> None:
    """
    At or below playback rate a player would underrun as soon as its burst ran out.

    The ceiling may be lowered per player, but never to realtime or slower.
    """
    assert float(OUTPUT_READRATE) > 1.0
    assert float(BURST_OUTPUT_READRATE) > 1.0


def test_the_default_burst_stays_small() -> None:
    """
    The default burst covers a track start and nothing more.

    A large burst flushes a realtime source's banked head start to the player,
    leaving its end-of-track crossfade nothing to mix, and overruns players
    with a small input buffer (Chromecast is the known case).
    """
    assert 1 <= float(OUTPUT_READRATE_INITIAL_BURST) <= 10


def test_the_burst_profile_covers_gapless_prefetch() -> None:
    """A player on the burst profile holds a large opening chunk before it plays gapless."""
    assert float(BURST_OUTPUT_READRATE_INITIAL_BURST) >= 10
