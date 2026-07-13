"""Replay-based DACP compliance tests for the AirPlay handler."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from music_assistant.providers.airplay.provider import AirPlayProvider

from .conftest import make_player
from .helpers import build_dacp_request, replay

pytestmark = pytest.mark.asyncio


async def test_unknown_active_remote_is_ignored(
    airplay_provider: AirPlayProvider, mock_mass: MagicMock
) -> None:
    """A request whose Active-Remote matches no player must be a no-op (and not crash)."""
    make_player(mock_mass, active_remote="123")
    raw = build_dacp_request("/ctrl-int/1/play", active_remote="999")

    await replay(airplay_provider, raw)

    mock_mass.players.cmd_play.assert_not_called()


@pytest.mark.parametrize(
    ("path", "cmd"),
    [
        ("/ctrl-int/1/playpause", "cmd_play_pause"),
        ("/ctrl-int/1/stop", "cmd_stop"),
        ("/ctrl-int/1/nextitem", "cmd_next_track"),
        ("/ctrl-int/1/previtem", "cmd_previous_track"),
        ("/ctrl-int/1/volumeup", "cmd_volume_up"),
        ("/ctrl-int/1/volumedown", "cmd_volume_down"),
    ],
)
async def test_transport_commands_dispatch(
    airplay_provider: AirPlayProvider, mock_mass: MagicMock, path: str, cmd: str
) -> None:
    """Simple transport paths dispatch the matching player command with the player id."""
    player = make_player(mock_mass)
    await replay(airplay_provider, build_dacp_request(path))
    getattr(mock_mass.players, cmd).assert_called_once_with(player.player_id)


async def test_play_when_not_playing_dispatches_play(
    airplay_provider: AirPlayProvider, mock_mass: MagicMock
) -> None:
    """/play starts playback when the player is not already playing."""
    player = make_player(mock_mass, playing=False)
    await replay(airplay_provider, build_dacp_request("/ctrl-int/1/play"))
    mock_mass.players.cmd_play.assert_called_once_with(player.player_id)


async def test_play_when_already_playing_is_ignored(
    airplay_provider: AirPlayProvider, mock_mass: MagicMock
) -> None:
    """/play is a confirmation echo and must be ignored when already playing."""
    make_player(mock_mass, playing=True)
    await replay(airplay_provider, build_dacp_request("/ctrl-int/1/play"))
    mock_mass.players.cmd_play.assert_not_called()


async def test_pause_when_playing_dispatches_pause(
    airplay_provider: AirPlayProvider, mock_mass: MagicMock
) -> None:
    """/pause pauses when the player is playing."""
    player = make_player(mock_mass, playing=True)
    await replay(airplay_provider, build_dacp_request("/ctrl-int/1/pause"))
    mock_mass.players.cmd_pause.assert_called_once_with(player.player_id)


async def test_pause_when_not_playing_is_ignored(
    airplay_provider: AirPlayProvider, mock_mass: MagicMock
) -> None:
    """/pause does nothing when the player is not playing."""
    make_player(mock_mass, playing=False)
    await replay(airplay_provider, build_dacp_request("/ctrl-int/1/pause"))
    mock_mass.players.cmd_pause.assert_not_called()


async def test_shuffle_songs_toggles_queue_shuffle(
    airplay_provider: AirPlayProvider, mock_mass: MagicMock
) -> None:
    """/shuffle_songs flips the active queue's shuffle state."""
    make_player(mock_mass)
    await replay(airplay_provider, build_dacp_request("/ctrl-int/1/shuffle_songs"))
    mock_mass.player_queues.set_shuffle.assert_called_once_with("q1", True)


async def test_discrete_pause_schedules_debounced_pause(
    airplay_provider: AirPlayProvider, mock_mass: MagicMock
) -> None:
    """/discrete-pause while playing schedules a debounced cmd_pause (not an immediate one)."""
    player = make_player(mock_mass, playing=True)
    await replay(airplay_provider, build_dacp_request("/ctrl-int/1/discrete-pause"))

    mock_mass.players.cmd_pause.assert_not_called()
    mock_mass.call_later.assert_called_once_with(
        1.0,
        mock_mass.players.cmd_pause,
        player.player_id,
        task_id=f"debounced_pause_{player.player_id}",
    )


async def test_discrete_pause_when_not_playing_does_nothing(
    airplay_provider: AirPlayProvider,
    mock_mass: MagicMock,
) -> None:
    """/discrete-pause is ignored when the player is not playing."""
    make_player(mock_mass, playing=False)
    await replay(airplay_provider, build_dacp_request("/ctrl-int/1/discrete-pause"))
    mock_mass.call_later.assert_not_called()


async def test_prevent_playback_cancels_pending_discrete_pause(
    airplay_provider: AirPlayProvider,
    mock_mass: MagicMock,
) -> None:
    """device-prevent-playback=1 cancels the debounced pause timer for the player."""
    player = make_player(mock_mass, playing=True)
    await replay(
        airplay_provider,
        build_dacp_request("/ctrl-int/1/setproperty?device-prevent-playback=1"),
    )
    mock_mass.cancel_timer.assert_any_call(f"debounced_pause_{player.player_id}")


async def test_device_volume_updates_volume(
    airplay_provider: AirPlayProvider, mock_mass: MagicMock
) -> None:
    """dmcp.device-volume in dB updates the player volume via the converted 0-100 value."""
    player = make_player(mock_mass)
    # -15 dB maps to 50 on the 0-100 scale
    raw = build_dacp_request("/ctrl-int/1/setproperty?dmcp.device-volume=-15.000000")
    await replay(airplay_provider, raw)
    player.update_volume_from_device.assert_called_once_with(50)


async def test_device_volume_mute_threshold_mutes(
    airplay_provider: AirPlayProvider, mock_mass: MagicMock
) -> None:
    """dmcp.device-volume at/below the mute threshold mutes the player and sends VOLUME=0."""
    player = make_player(mock_mass)
    raw = build_dacp_request("/ctrl-int/1/setproperty?dmcp.device-volume=-144.000000")
    await replay(airplay_provider, raw)
    assert player._attr_volume_muted is True
    player.stream.send_cli_command.assert_called_once_with("VOLUME=0")


async def test_device_volume_ignored_for_apple_device(
    airplay_provider: AirPlayProvider, mock_mass: MagicMock
) -> None:
    """Apple devices echo our own volume; dmcp.device-volume must be ignored for them."""
    player = make_player(mock_mass, manufacturer="Apple")
    raw = build_dacp_request("/ctrl-int/1/setproperty?dmcp.device-volume=-15.000000")
    await replay(airplay_provider, raw)
    player.update_volume_from_device.assert_not_called()


async def test_dmcp_volume_button_updates_volume(
    airplay_provider: AirPlayProvider, mock_mass: MagicMock
) -> None:
    """dmcp.volume (hardware buttons) updates the player volume directly."""
    player = make_player(mock_mass)
    raw = build_dacp_request("/ctrl-int/1/setproperty?dmcp.volume=45")
    await replay(airplay_provider, raw)
    player.update_volume_from_device.assert_called_once_with(45)


_PREVENT_1 = "/ctrl-int/1/setproperty?device-prevent-playback=1"
_PREVENT_0 = "/ctrl-int/1/setproperty?device-prevent-playback=0"


async def test_prevent_playback_stops_unsynced_stream(
    airplay_provider: AirPlayProvider, mock_mass: MagicMock
) -> None:
    """prevent-playback=1 on an established, unsynced stream stops that stream."""
    player = make_player(mock_mass, synced_to=None)
    await replay(airplay_provider, build_dacp_request(_PREVENT_1))
    assert player.stream.prevent_playback is True
    player.stream.stop.assert_called_once()
    mock_mass.players.cmd_ungroup.assert_not_called()


async def test_prevent_playback_ungroups_synced_player(
    airplay_provider: AirPlayProvider, mock_mass: MagicMock
) -> None:
    """prevent-playback=1 on a synced player ungroups instead of stopping the stream."""
    player = make_player(mock_mass, synced_to="parent")
    await replay(airplay_provider, build_dacp_request(_PREVENT_1))
    mock_mass.players.cmd_ungroup.assert_called_once_with(player.player_id)
    player.stream.stop.assert_not_called()


async def test_prevent_playback_ignored_during_transition(
    airplay_provider: AirPlayProvider,
    mock_mass: MagicMock,
) -> None:
    """prevent-playback=1 during a stream transition is a stale message and ignored."""
    player = make_player(mock_mass, transitioning=True)
    await replay(airplay_provider, build_dacp_request(_PREVENT_1))
    player.stream.stop.assert_not_called()
    mock_mass.players.cmd_ungroup.assert_not_called()


async def test_prevent_playback_ignored_when_stream_not_connected(
    airplay_provider: AirPlayProvider,
    mock_mass: MagicMock,
) -> None:
    """prevent-playback=1 before the stream is established (Denon transient) is ignored."""
    player = make_player(mock_mass, stream_connected=False)
    await replay(airplay_provider, build_dacp_request(_PREVENT_1))
    player.stream.stop.assert_not_called()


async def test_prevent_playback_ignored_when_already_set(
    airplay_provider: AirPlayProvider, mock_mass: MagicMock
) -> None:
    """A duplicate prevent-playback=1 while already preventing is ignored."""
    player = make_player(mock_mass, prevent_playback=True)
    await replay(airplay_provider, build_dacp_request(_PREVENT_1))
    player.stream.stop.assert_not_called()


async def test_prevent_playback_zero_schedules_reset(
    airplay_provider: AirPlayProvider, mock_mass: MagicMock
) -> None:
    """prevent-playback=0 schedules a debounced reset of the prevent_playback flag."""
    player = make_player(mock_mass, prevent_playback=True)
    await replay(airplay_provider, build_dacp_request(_PREVENT_0))
    mock_mass.call_later.assert_called_once_with(
        5,
        setattr,
        player.stream,
        "prevent_playback",
        False,
        task_id=f"reset_prevent_playback_{player.player_id}",
    )


async def test_denon_transient_prevent_playback_pair_keeps_stream(
    airplay_provider: AirPlayProvider, mock_mass: MagicMock
) -> None:
    """
    A transient prevent-playback=1/=0 burst during RAOP setup must not tear down the stream.

    Regression for a Denon AVR-X2700H that emits this pair while the stream is still being
    established (https://github.com/music-assistant/support/issues/5033).
    """
    player = make_player(mock_mass, synced_to=None, stream_connected=False)

    await replay(airplay_provider, build_dacp_request(_PREVENT_1))
    await replay(airplay_provider, build_dacp_request(_PREVENT_0))

    player.stream.stop.assert_not_called()
    mock_mass.players.cmd_ungroup.assert_not_called()
    assert player.stream.prevent_playback is False


async def test_empty_request_returns_early(
    airplay_provider: AirPlayProvider, mock_mass: MagicMock
) -> None:
    """An empty request (e.g. Phorus PS10 ack) is handled without dispatching anything."""
    make_player(mock_mass)
    await replay(airplay_provider, b"")
    mock_mass.players.cmd_play.assert_not_called()


async def test_headers_only_request_does_not_crash(
    airplay_provider: AirPlayProvider, mock_mass: MagicMock
) -> None:
    """A request without a body terminator must parse and dispatch normally."""
    player = make_player(mock_mass, playing=False)
    raw = b"POST /ctrl-int/1/play HTTP/1.1\r\nActive-Remote: 123"
    await replay(airplay_provider, raw)
    mock_mass.players.cmd_play.assert_called_once_with(player.player_id)


async def test_malformed_header_line_is_skipped(
    airplay_provider: AirPlayProvider, mock_mass: MagicMock
) -> None:
    """A header line without a colon is skipped rather than crashing the handler."""
    player = make_player(mock_mass, playing=False)
    raw = (
        b"POST /ctrl-int/1/play HTTP/1.1\r\n"
        b"garbage-line-without-colon\r\n"
        b"Active-Remote: 123\r\n\r\n"
    )
    await replay(airplay_provider, raw)
    mock_mass.players.cmd_play.assert_called_once_with(player.player_id)


async def test_unknown_path_returns_204_without_command(
    airplay_provider: AirPlayProvider, mock_mass: MagicMock
) -> None:
    """An unrecognized path dispatches nothing but still returns HTTP 204."""
    make_player(mock_mass)
    response = await replay(airplay_provider, build_dacp_request("/ctrl-int/1/bogus"))
    mock_mass.players.cmd_play.assert_not_called()
    assert response.startswith(b"HTTP/1.0 204 No Content")


async def test_handled_request_returns_204(
    airplay_provider: AirPlayProvider, mock_mass: MagicMock
) -> None:
    """A handled request returns the spec-shaped HTTP 204 response."""
    make_player(mock_mass)
    response = await replay(airplay_provider, build_dacp_request("/ctrl-int/1/stop"))
    assert response.startswith(b"HTTP/1.0 204 No Content")


# These commands are documented in the community DACP spec
# (https://openairplay.github.io/airplay-spec/audio/remote_control.html) but MA does
# not implement them yet. The test locks in the current graceful no-op so the suite
# covers the full spec without failing. When one is implemented, move it out of this
# parametrize list into its own behavior test. See the spec doc "Known spec gaps".
_UNHANDLED_SPEC_COMMANDS = [
    "/ctrl-int/1/beginff",
    "/ctrl-int/1/beginrew",
    "/ctrl-int/1/mutetoggle",
    "/ctrl-int/1/playresume",
]


@pytest.mark.parametrize("path", _UNHANDLED_SPEC_COMMANDS)
async def test_documented_unhandled_commands_noop(
    airplay_provider: AirPlayProvider, mock_mass: MagicMock, path: str
) -> None:
    """Spec commands MA does not implement dispatch nothing but still return HTTP 204."""
    make_player(mock_mass)
    response = await replay(airplay_provider, build_dacp_request(path))

    players = mock_mass.players
    for cmd in (
        "cmd_play",
        "cmd_pause",
        "cmd_play_pause",
        "cmd_stop",
        "cmd_next_track",
        "cmd_previous_track",
        "cmd_volume_up",
        "cmd_volume_down",
    ):
        getattr(players, cmd).assert_not_called()
    assert response.startswith(b"HTTP/1.0 204 No Content")
