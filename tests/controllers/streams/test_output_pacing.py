"""Tests for the rate at which stream output is handed to a player."""

from __future__ import annotations

from music_assistant.controllers.streams.constants import output_pacing_args


def _value(args: list[str], key: str) -> float:
    return float(args[args.index(key) + 1])


def test_pacing_stays_ahead_of_playback() -> None:
    """
    At or below playback rate a player would underrun as soon as its burst ran out.

    The ceiling may be lowered per player, but never to realtime or slower.
    """
    assert _value(output_pacing_args(), "-readrate") > 1.0
    assert _value(output_pacing_args("gapless_burst"), "-readrate") > 1.0
    assert _value(output_pacing_args("low_latency"), "-readrate") > 1.0


def test_the_default_burst_stays_small() -> None:
    """
    The default burst covers a track start and nothing more.

    A large burst flushes a realtime source's banked head start to the player,
    leaving its end-of-track crossfade nothing to mix, and overruns players
    with a small input buffer (Chromecast is the known case).
    """
    assert 1 <= _value(output_pacing_args(), "-readrate_initial_burst") <= 10


def test_the_burst_profile_covers_gapless_prefetch() -> None:
    """A player on the burst profile holds a large opening chunk before it plays gapless."""
    assert _value(output_pacing_args("gapless_burst"), "-readrate_initial_burst") >= 10


def test_the_low_latency_burst_stays_under_a_second() -> None:
    """A live source's burst is listening delay: the player buffers it ahead of real time."""
    assert _value(output_pacing_args("low_latency"), "-readrate_initial_burst") <= 1
