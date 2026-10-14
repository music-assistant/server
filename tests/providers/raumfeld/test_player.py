"""Tests for the Teufel Raumfeld player: position, grouping state and transport reads."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import pytest
from music_assistant_models.enums import IdentifierType, PlaybackState
from music_assistant_models.errors import PlayerCommandFailed

from music_assistant.constants import CONF_FLOW_MODE
from music_assistant.providers.raumfeld.constants import PLAYER_CONFIG_ENTRIES
from music_assistant.providers.raumfeld.player import (
    ADVANCE_WATCH_ATTEMPTS,
    IDLE_POLL_INTERVAL,
    PLAYING_POLL_INTERVAL,
    RaumfeldPlayer,
    _map_transport_state,
    _near_track_end,
)
from tests.common import MockProvider

if TYPE_CHECKING:
    import hassfeld
    from music_assistant_models.player import PlayerMedia

BASE_URL = "http://mass.local:8097"


def _flow_player() -> RaumfeldPlayer:
    """Build a RaumfeldPlayer with just the state the position path touches."""
    player = RaumfeldPlayer.__new__(RaumfeldPlayer)
    player._attr_elapsed_time = None
    player._attr_elapsed_time_last_updated = None
    mass = MagicMock()
    mass.streams.base_url = BASE_URL
    player.mass = mass
    return player


def _pos(reltime: str, uri: str = f"{BASE_URL}/flow/x/y/z.flac") -> dict[str, str]:
    return {"TrackURI": uri, "RelTime": reltime, "TrackDuration": "0:00:00"}


def test_flow_position_follows_the_zone_clock() -> None:
    """The zone clock runs on across the queue and is reported as the flow position."""
    player = _flow_player()
    player._apply_position(_pos("0:00:10"), playing=True)
    assert player._attr_elapsed_time == 10.0
    player._apply_position(_pos("0:07:30"), playing=True)
    assert player._attr_elapsed_time == 450.0


def test_flow_position_follows_a_drop_instead_of_jumping_ahead() -> None:
    """A clock that drops (the stream replayed) is followed, never banked into a jump ahead."""
    player = _flow_player()
    player._apply_position(_pos("0:03:00"), playing=True)
    player._apply_position(_pos("0:00:02"), playing=True)
    assert player._attr_elapsed_time == 2.0


def test_external_source_position_is_used_directly() -> None:
    """Line-In (not our stream) reports its own clock and is reflected as current media."""
    player = _flow_player()
    with patch.object(RaumfeldPlayer, "set_current_media") as set_current_media:
        player._apply_position(
            {"TrackURI": "http://192.168.1.40:8888/stream.flac", "RelTime": "0:00:30"},
            playing=True,
        )
    assert player._attr_elapsed_time == 30.0
    set_current_media.assert_called_once()


def test_position_not_updated_while_stopped() -> None:
    """A stopped renderer reports an unreliable clock, so nothing is anchored."""
    player = _flow_player()
    player._apply_position(_pos("0:00:10"), playing=False)
    assert player._attr_elapsed_time is None


async def test_unanswered_transport_read_is_not_a_reading() -> None:
    """Hassfeld swallows a UPnP timeout and returns None; that is not an idle zone."""
    player = RaumfeldPlayer.__new__(RaumfeldPlayer)
    host = MagicMock()
    host.async_get_transport_info = AsyncMock(return_value=None)
    assert await player._read_transport(cast("hassfeld.RaumfeldHost", host), ["Bar"]) is None


async def test_transport_reading_is_passed_through() -> None:
    """A real reading still reaches the caller unchanged."""
    player = RaumfeldPlayer.__new__(RaumfeldPlayer)
    host = MagicMock()
    host.async_get_transport_info = AsyncMock(return_value={"CurrentTransportState": "PLAYING"})
    result = await player._read_transport(cast("hassfeld.RaumfeldHost", host), ["Bar"])
    assert result == {"CurrentTransportState": "PLAYING"}


def test_transport_state_mapping() -> None:
    """Transport states map to the right MA playback states."""
    assert _map_transport_state("PLAYING") == PlaybackState.PLAYING
    assert _map_transport_state("TRANSITIONING") == PlaybackState.PLAYING
    assert _map_transport_state("PAUSED_PLAYBACK") == PlaybackState.PAUSED
    assert _map_transport_state("STOPPED") == PlaybackState.IDLE
    assert _map_transport_state(None) == PlaybackState.IDLE


def test_flow_mode_is_on_by_default() -> None:
    """Flow mode is a setting now, and it defaults to on."""
    entry = next(e for e in PLAYER_CONFIG_ENTRIES if e.key == CONF_FLOW_MODE)
    assert entry.default_value is True
    assert not entry.hidden


def test_near_track_end() -> None:
    """Near the end within the window, or when the duration is unknown."""
    assert _near_track_end({"RelTime": "0:03:55", "TrackDuration": "0:04:00"}) is True
    assert _near_track_end({"RelTime": "0:01:00", "TrackDuration": "0:04:00"}) is False
    assert _near_track_end({"RelTime": "0:01:00", "TrackDuration": "0:00:00"}) is True
    assert _near_track_end({"TrackDuration": "0:04:00"}) is False


def _media() -> PlayerMedia:
    return cast("PlayerMedia", MagicMock())


def _advancing_player() -> tuple[RaumfeldPlayer, MagicMock]:
    """Build a player playing one queue item (flow mode off) with the next one queued."""
    player = RaumfeldPlayer.__new__(RaumfeldPlayer)
    player._advance_armed = True
    player._prev_playing = True
    player._near_end = True
    player._next_media = _media()
    player._advance_task_id = "raumfeld_advance_test"
    mass = MagicMock()
    player.mass = mass
    player.play_media = MagicMock()  # type: ignore[method-assign]
    return player, mass


def test_advances_when_the_track_ends() -> None:
    """A stop near the end with a queued item starts that item."""
    player, mass = _advancing_player()
    queued = player._next_media
    player._maybe_advance(playing=False, ended=True)
    player.play_media.assert_called_once_with(queued)  # type: ignore[attr-defined]
    mass.create_task.assert_called_once()
    assert player._next_media is None


def test_no_advance_on_a_pause_mid_track() -> None:
    """A pause (not a stop) away from the end never skips to the next item."""
    player, mass = _advancing_player()
    player._near_end = False
    player._maybe_advance(playing=False, ended=False)
    mass.create_task.assert_not_called()
    assert player._next_media is not None


def test_no_advance_during_the_startup_gap() -> None:
    """The not-yet-playing gap after a play command is not taken for a finished track."""
    player, mass = _advancing_player()
    player._prev_playing = False  # as _mark_play_started leaves it
    player._maybe_advance(playing=False, ended=True)
    mass.create_task.assert_not_called()


def test_disarms_when_the_last_track_ends() -> None:
    """A track ending with nothing queued disarms instead of restarting anything."""
    player, mass = _advancing_player()
    player._next_media = None
    player._maybe_advance(playing=False, ended=True)
    mass.create_task.assert_not_called()
    assert player._advance_armed is False


def _watching_player(state: str) -> tuple[RaumfeldPlayer, MagicMock]:
    """Build an advancing player whose zone reports the given transport state."""
    player, mass = _advancing_player()
    player._active_zone = MagicMock(return_value=["Bar"])  # type: ignore[method-assign]
    player._read_transport = AsyncMock(  # type: ignore[method-assign]
        return_value={"CurrentTransportState": state}
    )
    player._provider = MagicMock()
    return player, mass


async def test_end_watch_advances_as_soon_as_the_track_stops() -> None:
    """The end watch starts the next item on the stop without waiting for a poll."""
    player, mass = _watching_player("STOPPED")
    await player._watch_for_track_end(0)
    mass.create_task.assert_called_once()
    assert player._next_media is None
    # claimed, so a poll landing in between cannot disarm the advance
    assert player._prev_playing is False


async def test_end_watch_waits_while_the_track_still_plays() -> None:
    """A track running past its metadata duration is not cut short."""
    player, mass = _watching_player("PLAYING")
    await player._watch_for_track_end(0)
    mass.create_task.assert_not_called()
    mass.call_later.assert_called_once()


async def test_end_watch_gives_up_after_the_last_attempt() -> None:
    """After the window closes the polled fallback takes over, rather than looping."""
    player, mass = _watching_player("PLAYING")
    await player._watch_for_track_end(ADVANCE_WATCH_ATTEMPTS - 1)
    mass.call_later.assert_not_called()
    mass.create_task.assert_not_called()


def test_clear_next_disarms_and_cancels_the_watch() -> None:
    """A stop or a new item drops the queued item and any pending end watch."""
    player, mass = _advancing_player()
    player._clear_next()
    assert player._advance_armed is False
    assert player._next_media is None
    mass.cancel_timer.assert_called_once_with("raumfeld_advance_test")


def _follower(leader_state: PlaybackState) -> RaumfeldPlayer:
    """Build a grouped follower whose leader reports the given playback state."""
    player = RaumfeldPlayer.__new__(RaumfeldPlayer)
    player.room = "Bank"
    player._player_id = "raumfeld_follower"
    player._attr_playback_state = PlaybackState.PLAYING
    player._attr_poll_interval = PLAYING_POLL_INTERVAL
    provider = MagicMock()
    provider.host.async_get_room_volume = AsyncMock(return_value=30)
    provider.host.get_room_power_state.return_value = "ACTIVE"
    provider.room_renderer_location.return_value = None
    player._provider = provider
    player._power_changed_at = 0.0
    leader = MagicMock()
    leader.playback_state = leader_state
    mass = MagicMock()
    mass.players.get_player = MagicMock(return_value=leader)
    player.mass = mass
    return player


async def test_follower_mirrors_a_stopped_leader() -> None:
    """A follower of a stopped group reads as idle and slows down, not as playing."""
    player = _follower(PlaybackState.IDLE)
    with (
        patch.object(RaumfeldPlayer, "synced_to", new_callable=PropertyMock) as synced_to,
        patch.object(RaumfeldPlayer, "update_state"),
    ):
        synced_to.return_value = "raumfeld_leader"
        await player.poll()
    assert player._attr_playback_state == PlaybackState.IDLE
    assert player._attr_poll_interval == IDLE_POLL_INTERVAL
    assert player._attr_volume_level == 30


async def test_follower_mirrors_a_playing_leader() -> None:
    """A follower of a playing group reads as playing at the playing poll rate."""
    player = _follower(PlaybackState.PLAYING)
    with (
        patch.object(RaumfeldPlayer, "synced_to", new_callable=PropertyMock) as synced_to,
        patch.object(RaumfeldPlayer, "update_state"),
    ):
        synced_to.return_value = "raumfeld_leader"
        await player.poll()
    assert player._attr_playback_state == PlaybackState.PLAYING
    assert player._attr_poll_interval == PLAYING_POLL_INTERVAL


def test_player_id_to_room_ignores_other_providers() -> None:
    """Only a Raumfeld player resolves to a room, not any player with a room attribute."""
    player = RaumfeldPlayer.__new__(RaumfeldPlayer)
    foreign = MagicMock()
    foreign.room = "Kitchen"
    mass = MagicMock()
    mass.players.get_player = MagicMock(return_value=foreign)
    player.mass = mass
    assert player._player_id_to_room("dlna_kitchen") is None


async def test_unload_cancels_both_timers() -> None:
    """Unloading cancels the end watch and the group resync, which mass would otherwise run."""
    player, mass = _advancing_player()
    player._resync_task_id = "raumfeld_resync_test"
    player._on_unload_callbacks = []

    await player.on_unload()

    assert {call.args[0] for call in mass.cancel_timer.call_args_list} == {
        "raumfeld_advance_test",
        "raumfeld_resync_test",
    }
    assert player._advance_armed is False
    assert player._next_media is None


def test_room_on_the_host_gets_its_ip_identifier() -> None:
    """The room whose renderer runs on the host gets its IP, so its other outputs link to it."""
    provider = MockProvider("raumfeld")
    provider.resolve_room_renderer = MagicMock(  # type: ignore[attr-defined]
        return_value=("5065835a-d69f-4a1b-8c2d-5065835ad69f", "192.168.1.41")
    )
    provider.line_in = MagicMock(return_value=None)  # type: ignore[attr-defined]
    provider.room_model = MagicMock(  # type: ignore[attr-defined]
        return_value="Raumfeld Soundbar"
    )
    host = MagicMock()
    host.get_room_power_state.return_value = "MANUAL_STANDBY"
    provider.host = host  # type: ignore[attr-defined]
    player = RaumfeldPlayer(
        provider,  # type: ignore[arg-type]
        "raumfeld_5065835ad69f",
        "TV",
    )
    assert player.device_info.identifiers[IdentifierType.IP_ADDRESS] == "192.168.1.41"
    assert player.device_info.model == "Raumfeld Soundbar"
    assert player.powered is False


_RENDERER = "http://192.168.1.42:54730/renderer.xml"


def _room_player(location: str | None = _RENDERER) -> RaumfeldPlayer:
    """Build a RaumfeldPlayer for a room whose own renderer is at the given location."""
    player = _flow_player()
    player.room = "Bar"
    provider = MagicMock()
    provider.room_renderer_location.return_value = location
    player._provider = provider
    return player


async def test_mute_goes_to_the_rooms_own_renderer() -> None:
    """Muting addresses the room's renderer, so it works without a zone and for one room."""
    player = _room_player()
    with (
        patch("music_assistant.providers.raumfeld.player.hassfeld_upnp") as upnp,
        patch.object(player, "update_state"),
    ):
        upnp.async_set_mute = AsyncMock()
        await player.volume_mute(True)
    upnp.async_set_mute.assert_awaited_once_with(player.mass.http_session, _RENDERER, True)
    assert player._attr_volume_muted is True


async def test_mute_fails_without_a_known_renderer() -> None:
    """A room whose renderer is unknown reports the failure instead of pretending to mute."""
    player = _room_player(location=None)
    player._attr_volume_muted = False
    with pytest.raises(PlayerCommandFailed):
        await player.volume_mute(True)
    assert player._attr_volume_muted is False


async def test_volume_update_reads_mute_and_keeps_unread_values() -> None:
    """A poll picks up a mute set elsewhere, and a failed read leaves the last value."""
    player = _room_player()
    player._attr_volume_level = 30
    player._attr_volume_muted = False
    host = MagicMock()
    host.async_get_room_volume = AsyncMock(return_value=42)
    with patch("music_assistant.providers.raumfeld.player.hassfeld_upnp") as upnp:
        upnp.async_get_mute = AsyncMock(return_value=True)
        await player._update_volume(host)
        assert (player._attr_volume_level, player._attr_volume_muted) == (42, True)
        host.async_get_room_volume = AsyncMock(return_value=None)
        upnp.async_get_mute = AsyncMock(return_value=None)
        await player._update_volume(host)
    assert (player._attr_volume_level, player._attr_volume_muted) == (42, True)


def _powered_player(power_state: str | None) -> tuple[RaumfeldPlayer, MagicMock]:
    """Build a RaumfeldPlayer whose host reports the given power state for its room."""
    player = _room_player()
    host = MagicMock()
    host.get_room_power_state.return_value = power_state
    host.async_enter_manual_standby = AsyncMock()
    host.async_leave_standby = AsyncMock()
    provider = MagicMock()
    provider.host = host
    player._provider = provider
    player._power_changed_at = 0.0
    player._attr_powered = None
    player.logger = MagicMock()
    return player, host


def test_only_a_switched_off_room_counts_as_powered_off() -> None:
    """A room that merely went idle stays on, so MA does not ungroup idle rooms."""
    for power_state, expected in (
        ("ACTIVE", True),
        ("AUTOMATIC_STANDBY", True),
        ("MANUAL_STANDBY", False),
    ):
        player, _ = _powered_player(power_state)
        player._update_power()
        assert player._attr_powered is expected


def test_unknown_power_state_keeps_the_last_value() -> None:
    """A room the host reports no power state for keeps what was known."""
    player, host = _powered_player(None)
    player._attr_powered = True
    player._update_power()
    assert player._attr_powered is True
    host.get_room_power_state.side_effect = KeyError("Bar")
    player._update_power()
    assert player._attr_powered is True


async def test_power_off_puts_the_room_in_manual_standby() -> None:
    """Powering off switches the room off, and the lagging host state does not undo it."""
    player, host = _powered_player("ACTIVE")
    with patch.object(player, "update_state"):
        await player.power(False)
    host.async_enter_manual_standby.assert_awaited_once_with("Bar")
    assert player._attr_powered is False
    # the host still reports the old state right after the command
    player._update_power()
    assert player._attr_powered is False


async def test_power_on_wakes_the_room() -> None:
    """Powering on takes the room out of standby."""
    player, host = _powered_player("MANUAL_STANDBY")

    async def leave_standby(_room: str) -> None:
        host.get_room_power_state.return_value = "ACTIVE"

    host.async_leave_standby.side_effect = leave_standby
    with patch.object(player, "update_state"):
        await player.power(True)
    host.async_leave_standby.assert_awaited_once_with("Bar")
    assert player._attr_powered is True


async def test_power_on_fails_when_the_room_stays_in_standby() -> None:
    """A room that does not wake up is not reported as powered on."""
    player, _ = _powered_player("MANUAL_STANDBY")
    player._attr_powered = False
    with (
        patch("music_assistant.providers.raumfeld.player.asyncio.sleep", AsyncMock()),
        patch("music_assistant.providers.raumfeld.player.WAKE_TIMEOUT", 0),
        pytest.raises(PlayerCommandFailed, match="did not leave standby"),
    ):
        await player.power(True)
    assert player._attr_powered is False


async def test_power_on_fails_when_the_wake_command_fails() -> None:
    """A host error on the wake command surfaces instead of reporting the room as on."""
    player, host = _powered_player("MANUAL_STANDBY")
    player._attr_powered = False
    host.async_leave_standby.side_effect = TimeoutError
    with pytest.raises(PlayerCommandFailed, match="Failed to wake Bar"):
        await player.power(True)
    assert player._attr_powered is False


async def test_power_on_fails_for_a_room_the_host_does_not_know() -> None:
    """A room without a power state is not reported as on, and gets no wake command."""
    player, host = _powered_player(None)
    player._attr_powered = False
    with pytest.raises(PlayerCommandFailed, match="no power state"):
        await player.power(True)
    host.async_leave_standby.assert_not_awaited()
    assert player._attr_powered is False


async def test_power_on_fails_when_the_room_vanishes_while_waking() -> None:
    """A room the host stops reporting during the wake is not taken for awake."""
    player, host = _powered_player("MANUAL_STANDBY")
    player._attr_powered = False

    async def leave_standby(_room: str) -> None:
        host.get_room_power_state.return_value = None

    host.async_leave_standby.side_effect = leave_standby
    with (
        patch("music_assistant.providers.raumfeld.player.asyncio.sleep", AsyncMock()),
        patch("music_assistant.providers.raumfeld.player.WAKE_TIMEOUT", 0.05),
        pytest.raises(PlayerCommandFailed, match="did not leave standby"),
    ):
        await player.power(True)
    assert player._attr_powered is False


async def test_power_off_failure_is_reported() -> None:
    """A host error while powering off surfaces and leaves the power state untouched."""
    player, host = _powered_player("ACTIVE")
    player._attr_powered = True
    host.async_enter_manual_standby.side_effect = TimeoutError
    with pytest.raises(PlayerCommandFailed):
        await player.power(False)
    assert player._attr_powered is True


async def test_no_zone_for_the_room_is_reported() -> None:
    """A room the host gives no zone raises a clear error instead of playing into nothing."""
    player = _flow_player()
    player.room = "Bar"
    host = MagicMock()
    host.async_create_zone = AsyncMock()
    host.roomlst_to_zoneudn.return_value = None
    provider = MagicMock()
    provider.host = host
    provider.get_zone_for_room.return_value = None
    player._provider = provider
    with (
        patch("music_assistant.providers.raumfeld.player.asyncio.sleep", AsyncMock()),
        pytest.raises(PlayerCommandFailed, match="no zone for Bar"),
    ):
        await player._ensure_playable_zone()
