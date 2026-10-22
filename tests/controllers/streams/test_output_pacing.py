"""Tests for the rate at which stream output is handed to a player."""

from __future__ import annotations

from typing import cast
from unittest.mock import MagicMock

import pytest

from music_assistant.constants import CONF_ENTRY_CROSSFADE_DURATION, CONF_ENTRY_FLOW_STREAM_LEAD
from music_assistant.controllers.streams.constants import PacingProfile, output_pacing_args
from music_assistant.controllers.streams.controller import StreamsController

# A track of ordinary length from a source that delivers just-in-time, at the low end
# of what the Spotify Soloist backend delivered when measured (1.07x to 1.1x).
TRACK_SECONDS = 200
SLOW_SOURCE_RATE = 1.075
# The longest such a source took to have a track ready: up to 1.7 seconds to start,
# plus the first second of audio its stream waits for.
SLOW_SOURCE_START_SECONDS = 2.7


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


def test_the_near_realtime_pacing_is_the_measured_one() -> None:
    """
    The pace and the burst were picked from measurements on real speakers.

    Another value needs its own measurements: a higher pace takes the crossfades of a
    slow source away, a lower one or a smaller burst takes the player's cushion.
    """
    assert output_pacing_args(PacingProfile.NEAR_REALTIME) == [
        "-readrate",
        "1.01",
        "-readrate_initial_burst",
        "3",
    ]


def test_a_track_of_a_slow_source_earns_the_room_for_a_fade() -> None:
    """
    A source fills its next track as soon as the current one is in.

    The room for a fade is how much longer the stream of a track takes than its fill,
    and a fade of the default length needs that room at the end of the first track
    already. A stream per track is the tight case, as its burst comes out of that room
    with every track, where the flow stream opens with one burst for a whole session.
    """
    args = output_pacing_args(PacingProfile.NEAR_REALTIME)
    stream_seconds = (TRACK_SECONDS - _value(args, "-readrate_initial_burst")) / _value(
        args, "-readrate"
    )
    fill_seconds = TRACK_SECONDS / SLOW_SOURCE_RATE
    default_fade_seconds = cast("int", CONF_ENTRY_CROSSFADE_DURATION.default_value)
    assert stream_seconds - fill_seconds >= default_fade_seconds


def test_the_opening_burst_outlasts_a_source_starting_up() -> None:
    """
    A track change without a fade holds the stream back until the next track's source delivers.

    At the first track change of any stream the burst is all a player is sure to
    hold, however short that first track is.
    """
    burst = _value(output_pacing_args(PacingProfile.NEAR_REALTIME), "-readrate_initial_burst")
    assert burst > SLOW_SOURCE_START_SECONDS


def test_the_low_latency_burst_stays_under_a_second() -> None:
    """A live source's burst is listening delay: the player buffers it ahead of real time."""
    assert _value(output_pacing_args(PacingProfile.LOW_LATENCY), "-readrate_initial_burst") <= 1


def test_the_flow_lead_profile_gives_more_lead_and_still_banks() -> None:
    """
    A player that asked for a larger lead gets a pace above the flow stream's usual one.

    It still stays well under what a just-in-time source delivers, so such a source keeps
    banking ahead for its crossfades, only less than at the usual pace.
    """
    lead = _value(output_pacing_args(PacingProfile.FLOW_LEAD), "-readrate")
    assert lead > _value(output_pacing_args(PacingProfile.NEAR_REALTIME), "-readrate")
    assert lead < SLOW_SOURCE_RATE
    assert _value(output_pacing_args(PacingProfile.FLOW_LEAD), "-readrate_initial_burst") <= 5


def test_the_flow_lead_pacing_is_the_measured_one() -> None:
    """The pace a Raumfeld zone renderer played on without re-requesting the flow stream."""
    assert output_pacing_args(PacingProfile.FLOW_LEAD) == [
        "-readrate",
        "1.03",
        "-readrate_initial_burst",
        "3",
    ]


@pytest.mark.parametrize(
    ("setting", "profile"),
    [
        (None, PacingProfile.NEAR_REALTIME),
        (False, PacingProfile.NEAR_REALTIME),
        (True, PacingProfile.FLOW_LEAD),
    ],
)
def test_the_flow_stream_takes_the_lead_a_player_asked_for(
    setting: bool | None, profile: PacingProfile
) -> None:
    """The flow stream keeps its usual pace unless the player's setting asks for a lead."""
    controller = StreamsController.__new__(StreamsController)
    player = MagicMock()
    player.get_config_value.side_effect = lambda _key, default: (
        default if setting is None else setting
    )
    assert controller._flow_pacing(player) == profile


def test_changing_the_flow_lead_restarts_a_playing_queue() -> None:
    """The pace is set when the flow stream starts, so a change has to restart it."""
    assert CONF_ENTRY_FLOW_STREAM_LEAD.requires_reload
