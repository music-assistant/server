"""Tests for the rate at which stream output is handed to a player."""

from __future__ import annotations

from music_assistant.controllers.streams.constants import PacingProfile, output_pacing_args


def _value(args: list[str], key: str) -> float:
    return float(args[args.index(key) + 1])


def test_pacing_stays_ahead_of_playback() -> None:
    """
    At or below playback rate a player would underrun as soon as its burst ran out.

    The ceiling may be lowered per player, but never to realtime or slower.
    """
    assert _value(output_pacing_args(), "-readrate") > 1.0
    assert _value(output_pacing_args(PacingProfile.NEAR_REALTIME), "-readrate") > 1.0
    assert _value(output_pacing_args(PacingProfile.LOW_LATENCY), "-readrate") > 1.0


def test_the_default_burst_covers_a_gapless_players_opening_chunk() -> None:
    """A player that holds a whole opening chunk before it plays gapless must get one."""
    assert _value(output_pacing_args(), "-readrate_initial_burst") >= 10


def test_the_near_realtime_profile_leaves_room_for_a_source_to_bank_ahead() -> None:
    """
    A just-in-time source delivers ~1.1x at best, and its bank is what a crossfade mixes.

    Drained at the default pace that bank never grows, so the profile has to stay
    below it and leave real margin under what the source can deliver. The burst
    comes out of that same bank, so it stays small too.
    """
    readrate = _value(output_pacing_args(PacingProfile.NEAR_REALTIME), "-readrate")
    assert readrate < _value(output_pacing_args(), "-readrate")
    # 1.1 is the fill rate, so anything close to it banks nothing
    assert readrate <= 1.05
    assert _value(output_pacing_args(PacingProfile.NEAR_REALTIME), "-readrate_initial_burst") <= 5


def test_the_low_latency_burst_stays_under_a_second() -> None:
    """A live source's burst is listening delay: the player buffers it ahead of real time."""
    assert _value(output_pacing_args(PacingProfile.LOW_LATENCY), "-readrate_initial_burst") <= 1
