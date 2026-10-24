"""Tests for the Teufel Raumfeld player: position, grouping state and transport reads."""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import pytest
from async_upnp_client.exceptions import UpnpActionError
from music_assistant_models.config_entries import MULTI_VALUE_SPLITTER
from music_assistant_models.enums import IdentifierType, PlaybackState, PlayerFeature, RepeatMode
from music_assistant_models.errors import PlayerCommandFailed
from music_assistant_models.player import PlayerMedia

from music_assistant.constants import CONF_FLOW_MODE, CONF_SAMPLE_RATES
from music_assistant.providers.raumfeld.constants import PLAYER_CONFIG_ENTRIES
from music_assistant.providers.raumfeld.helpers import library_uri, play_mode_for
from music_assistant.providers.raumfeld.player import (
    ADVANCE_WATCH_ATTEMPTS,
    IDLE_POLL_INTERVAL,
    PLAYING_POLL_INTERVAL,
    ZONE_CREATE_ATTEMPTS,
    RaumfeldPlayer,
    _map_transport_state,
    _near_track_end,
)
from tests.common import MockProvider

if TYPE_CHECKING:
    import hassfeld

BASE_URL = "http://mass.local:8097"


def _flow_player() -> RaumfeldPlayer:
    """Build a RaumfeldPlayer with just the state the position path touches."""
    player = RaumfeldPlayer.__new__(RaumfeldPlayer)
    player._attr_elapsed_time = None
    player._attr_elapsed_time_last_updated = None
    player._attr_group_members = []
    player._advance_task_id = "raumfeld_advance_test"
    player._external_source = None
    player._attr_supported_features = set()
    player._library_sources = {}
    player._extra_data = {}
    # no sample rate setting: the room plays the safe default
    player._config = MagicMock(get_value=MagicMock(return_value=None))
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
    """Line-In (not our stream) reports its own clock."""
    player = _flow_player()
    player._apply_position(
        {"TrackURI": "http://192.168.1.40:8888/stream.flac", "RelTime": "0:00:30"},
        playing=True,
    )
    assert player._attr_elapsed_time == 30.0


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


def test_sample_rates_are_chosen_per_room() -> None:
    """Up to 24-bit/192kHz can be chosen per room; up to 24-bit/96kHz is on by default."""
    entry = next(e for e in PLAYER_CONFIG_ENTRIES if e.key == CONF_SAMPLE_RATES)
    assert not entry.hidden
    options = {cast("str", option.value) for option in entry.options}
    default = set(cast("list[str]", entry.default_value))
    assert f"192000{MULTI_VALUE_SPLITTER}24" in options
    assert f"96000{MULTI_VALUE_SPLITTER}24" in default
    assert f"176400{MULTI_VALUE_SPLITTER}24" not in default
    assert f"192000{MULTI_VALUE_SPLITTER}24" not in default


def test_a_room_plays_the_rates_chosen_for_it() -> None:
    """The player declares no rates of its own, so the room's sample rate setting applies."""
    assert _host_room_player().supported_sample_rates is None


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
    return PlayerMedia(uri="http://mass.local:8097/single/item.flac", title="Track")


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
    player._play_next = MagicMock()  # type: ignore[method-assign]
    return player, mass


def test_advances_when_the_track_ends() -> None:
    """A stop near the end with a queued item starts that item."""
    player, mass = _advancing_player()
    queued = player._next_media
    player._maybe_advance(playing=False, ended=True)
    player._play_next.assert_called_once_with(queued)  # type: ignore[attr-defined]
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
    player._attr_group_members = []
    player._attr_supported_features = set()
    player._attr_playback_state = PlaybackState.PLAYING
    player._attr_poll_interval = PLAYING_POLL_INTERVAL
    provider = MagicMock()
    provider.host.async_get_room_volume = AsyncMock(return_value=30)
    provider.host.get_room_power_state.return_value = "ACTIVE"
    provider.host.room_is_spotify_single_room.return_value = False
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


async def test_unload_cancels_its_timers() -> None:
    """Unloading cancels the timers mass holds for this player, which would otherwise run."""
    player, mass = _advancing_player()
    player._player_id = "raumfeld_test"
    player._on_unload_callbacks = []

    await player.on_unload()

    assert {call.args[0] for call in mass.cancel_timer.call_args_list} == {
        "raumfeld_advance_test",
        "external_pause_raumfeld_test",
    }
    assert player._advance_armed is False
    assert player._next_media is None


def _host_room_player() -> RaumfeldPlayer:
    """Build a real RaumfeldPlayer for the room whose renderer runs on the host."""
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
    return RaumfeldPlayer(
        provider,  # type: ignore[arg-type]
        "raumfeld_5065835ad69f",
        "TV",
    )


def test_room_on_the_host_gets_its_ip_identifier() -> None:
    """The room whose renderer runs on the host gets its IP, so its other outputs link to it."""
    player = _host_room_player()
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


async def test_no_zone_for_the_room_is_reported() -> None:
    """A room the host gives no zone raises a clear error instead of playing into nothing."""
    player = _room_player()
    host = MagicMock()
    host.roomlst_to_zoneudn.return_value = None
    provider = MagicMock()
    provider.host = host
    provider.get_zone_for_room.return_value = None
    player._provider = provider
    player.logger = MagicMock()
    with (
        patch(
            "music_assistant.providers.raumfeld.player.hassfeld_ws.async_connect_rooms_to_zone",
            AsyncMock(),
        ),
        patch("music_assistant.providers.raumfeld.player.ZONE_WAIT_TIMEOUT", 0),
        pytest.raises(PlayerCommandFailed, match="no zone for Bar"),
    ):
        await player._ensure_playable_zone()


async def test_a_group_is_created_as_one_zone() -> None:
    """A leader without a zone has the host make one with all rooms at once."""
    player = _room_player()
    host = MagicMock()
    host.roomlst_to_udnlst.side_effect = lambda rooms: [f"uuid:{room}" for room in rooms]
    host.location = "http://192.168.1.41:47365"
    host.async_add_room_to_zone = AsyncMock()
    provider = MagicMock()
    provider.host = host
    # no zone until the host is asked to create one
    provider.get_zone_for_room.return_value = None
    player._provider = provider
    player.logger = MagicMock()

    async def create(*_args: object, **_kwargs: object) -> None:
        provider.get_zone_for_room.return_value = ["Bar", "Sofa"]

    with patch("music_assistant.providers.raumfeld.player.hassfeld_ws") as webservice:
        webservice.async_connect_rooms_to_zone = AsyncMock(side_effect=create)
        await player._add_rooms_to_zone(["Sofa"])
    webservice.async_connect_rooms_to_zone.assert_awaited_once_with(
        player.mass.http_session,
        "http://192.168.1.41:47365",
        room_udns=["uuid:Bar", "uuid:Sofa"],
    )
    host.async_add_room_to_zone.assert_not_awaited()


async def test_rooms_the_host_does_not_add_are_reported() -> None:
    """A room that never lands in the leader's zone fails the group change, not silently."""
    player = _room_player()
    host = MagicMock()
    host.async_add_room_to_zone = AsyncMock()
    provider = MagicMock()
    provider.host = host
    provider.get_zone_for_room.return_value = ["Bar"]
    player._provider = provider
    player.logger = MagicMock()
    with (
        patch("music_assistant.providers.raumfeld.player.ZONE_WAIT_TIMEOUT", 0),
        pytest.raises(PlayerCommandFailed, match="did not add Sofa to the zone of Bar"),
    ):
        await player._add_rooms_to_zone(["Sofa"])
    assert host.async_add_room_to_zone.await_count == ZONE_CREATE_ATTEMPTS


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


def test_power_control_only_for_a_room_with_a_power_state() -> None:
    """A room the host reports no power state for gets no power control, so MA never wakes it."""
    player, host = _powered_player(None)
    player._attr_supported_features = {PlayerFeature.PLAY_MEDIA, PlayerFeature.POWER}
    player._update_power()
    assert PlayerFeature.POWER not in player._attr_supported_features
    host.get_room_power_state.return_value = "ACTIVE"
    player._update_power()
    assert PlayerFeature.POWER in player._attr_supported_features


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


_SPOTIFY_META: dict[str, str | None] = {
    "title": "Who's That",
    "artist": "The War On Drugs",
    "album": "Who's That",
    "image_url": "http://i.scdn.co/image/cover",
    "section": "Spotify",
    "item_id": "0/Spotify/Track",
}
_ALBUM_META: dict[str, str | None] = {
    "title": "Shine On You Crazy Diamond",
    "artist": "Pink Floyd",
    "album": "Wish You Were Here",
    "image_url": None,
    "section": "My Music",
    "item_id": "0/My Music/Albums/42/1",
}
_RADIO_META: dict[str, str | None] = {
    "title": "Sublime",
    "artist": None,
    "album": None,
    "image_url": None,
    "section": "RadioTime",
    "item_id": "0/RadioTime/Search/s-s25777",
}


_ZONE = "http://192.168.1.41:52552/zone.xml"


def _external_player() -> tuple[RaumfeldPlayer, MagicMock]:
    """Build a RaumfeldPlayer with the state the external-source handling touches."""
    player = _room_player()
    player._attr_supported_features = {PlayerFeature.PLAY_MEDIA, PlayerFeature.VOLUME_SET}
    player._attr_source_list = []
    player._static_sources = []
    player._attr_active_source = None
    player._attr_current_media = None
    player._attr_playback_state = PlaybackState.PLAYING
    player._external_via_room = False
    player._play_started_at = 0.0
    player._line_in = None
    player._next_media = None
    player._advance_armed = False
    player._advance_task_id = "raumfeld_advance_test"
    player.logger = MagicMock()
    host = MagicMock()
    for command in (
        "async_room_pause",
        "async_zone_pause",
        "async_room_play",
        "async_zone_play",
        "async_room_next_track",
        "async_zone_next_track",
    ):
        setattr(host, command, AsyncMock())
    provider = MagicMock()
    provider.host = host
    provider.get_zone_for_room.return_value = ["Bar"]
    provider.zone_location.return_value = _ZONE
    provider.room_renderer_location.return_value = _RENDERER
    provider.room_transport_url.return_value = "http://192.168.1.42:54730/AVTransport/ctrl"
    player._provider = provider
    return player, host


def test_spotify_connect_offers_the_controls_the_host_allows() -> None:
    """An external source is reported with exactly the controls the host lists for it."""
    player, _ = _external_player()
    player._set_external(
        _SPOTIFY_META, "Pause,Previous,Next,Seek,Repeat,Shuffle", 315, via_room=True
    )
    assert player._attr_active_source == "spotify"
    assert [source.name for source in player._attr_source_list] == ["Spotify"]
    source = player._attr_source_list[0]
    assert (source.passive, source.can_play_pause, source.can_next_previous, source.can_seek) == (
        True,
        True,
        True,
        True,
    )
    assert {
        PlayerFeature.PAUSE,
        PlayerFeature.NEXT_PREVIOUS,
        PlayerFeature.SEEK,
    } <= player._attr_supported_features
    media = player._attr_current_media
    assert media is not None
    assert (media.title, media.artist, media.duration) == ("Who's That", "The War On Drugs", 315)
    assert media.uri == "raumfeld://0/Spotify/Track"


def test_radio_offers_no_transport_controls() -> None:
    """A source the host only allows to stop gets no pause, skip or seek."""
    player, _ = _external_player()
    player._set_external(_RADIO_META, "Stop", None, via_room=False)
    assert player._attr_active_source == "tunein"
    source = player._attr_source_list[0]
    assert (source.can_play_pause, source.can_next_previous, source.can_seek) == (
        False,
        False,
        False,
    )
    assert not player._attr_supported_features & {
        PlayerFeature.PAUSE,
        PlayerFeature.NEXT_PREVIOUS,
        PlayerFeature.SEEK,
    }


def test_controls_are_dropped_when_the_external_source_ends() -> None:
    """Pause must not stay available for Music Assistant's own streams."""
    player, _ = _external_player()
    player._set_external(_SPOTIFY_META, "Pause,Next,Seek", 315, via_room=True)
    player._clear_external()
    assert player._attr_active_source is None
    assert player._attr_source_list == []
    assert player._external_via_room is False
    assert player._attr_supported_features == {PlayerFeature.PLAY_MEDIA, PlayerFeature.VOLUME_SET}


async def test_own_stream_is_not_an_external_source() -> None:
    """Music Assistant's own stream drops a previously active external source."""
    player, _ = _external_player()
    player._set_external(_RADIO_META, "Stop", None, via_room=False)
    await player._update_zone_source({"TrackURI": f"{BASE_URL}/flow/x/y/z.flac"}, ["Bar"], 0)
    assert player._external_source is None
    assert player._attr_active_source is None


async def test_zone_playback_from_the_raumfeld_app_is_followed() -> None:
    """Playback the host started is reported, without the address it streams from."""
    player, _ = _external_player()
    track_uri = "https://audio.example.com/track.flac?token=secret"
    with patch.object(player, "_read_zone_actions", AsyncMock(return_value="Pause,Next")):
        await player._update_zone_source({"TrackURI": track_uri, "TrackMetaData": ""}, ["Bar"], 0)
    assert player._attr_active_source == "raumfeld"
    assert PlayerFeature.PAUSE in player._attr_supported_features
    assert player._attr_current_media is not None
    assert player._attr_current_media.uri == "raumfeld://raumfeld"


async def test_pause_goes_to_the_renderer_that_plays() -> None:
    """Spotify Connect is paused on the room itself, other sources on the zone."""
    player, _ = _external_player()
    with (
        patch.object(player, "update_state"),
        patch.object(player, "_transport_action", AsyncMock()) as action,
    ):
        player._set_external(_SPOTIFY_META, "Pause", 315, via_room=True)
        await player.pause()
        action.assert_awaited_once_with(_RENDERER, "Pause")
        assert player._attr_playback_state == PlaybackState.PAUSED
        player._set_external(_RADIO_META, "Pause", None, via_room=False)
        with patch.object(player, "_active_zone", return_value=["Bar"]):
            await player.pause()
    action.assert_awaited_with(_ZONE, "Pause")


async def test_resume_continues_the_external_source() -> None:
    """Play on a paused external source resumes it instead of starting the queue."""
    player, _ = _external_player()
    player._set_external(_SPOTIFY_META, "Play", 315, via_room=True)
    player._attr_playback_state = PlaybackState.PAUSED
    with (
        patch.object(player, "update_state"),
        patch.object(player, "_transport_action", AsyncMock()) as action,
    ):
        await player.play()
    action.assert_awaited_once_with(_RENDERER, "Play", Speed="1")
    assert player._attr_playback_state == PlaybackState.PLAYING


async def test_seek_sends_an_absolute_time() -> None:
    """Seeking addresses the playing renderer with an HH:MM:SS target."""
    player, _ = _external_player()
    player._set_external(_SPOTIFY_META, "Seek", 315, via_room=True)
    with (
        patch.object(player, "update_state"),
        patch.object(player, "_transport_action", AsyncMock()) as action,
    ):
        await player.seek(3725)
    action.assert_awaited_once_with(_RENDERER, "Seek", Unit="ABS_TIME", Target="01:02:05")
    assert player._attr_elapsed_time == 3725.0


async def test_a_rejected_command_is_reported() -> None:
    """A command the renderer rejects fails, and the state it would have set is not set."""
    player, _ = _external_player()
    player._set_external(_SPOTIFY_META, "Pause", 315, via_room=True)
    with (
        patch.object(player, "_transport_action", AsyncMock(side_effect=TimeoutError)),
        pytest.raises(PlayerCommandFailed, match="Pause failed on Bar"),
    ):
        await player.pause()
    assert player._attr_playback_state == PlaybackState.PLAYING


async def test_a_command_without_anything_playing_is_reported() -> None:
    """A command for a source whose zone is gone fails instead of doing nothing."""
    player, _ = _external_player()
    player._set_external(_RADIO_META, "Pause", None, via_room=False)
    with (
        patch.object(player, "_active_zone", return_value=None),
        pytest.raises(PlayerCommandFailed, match="nothing is playing"),
    ):
        await player.next_track()


def test_paused_external_source_shows_where_it_stopped() -> None:
    """A source paused from its own app reports the position it paused at."""
    player, _ = _external_player()
    player._set_external(_SPOTIFY_META, "Play", 315, via_room=True)
    player._apply_position({"RelTime": "0:01:10"}, playing=False)
    assert player._attr_elapsed_time == 70.0


async def test_a_stopped_zone_source_stays_loaded() -> None:
    """A source the host still has loaded stays the active source while it is stopped."""
    player, _ = _external_player()
    player._attr_playback_state = PlaybackState.IDLE
    with patch.object(player, "_read_zone_actions", AsyncMock(return_value="Play")):
        await player._update_zone_source(
            {"TrackURI": "https://radio.example.com/stream", "TrackMetaData": ""},
            ["Bar"],
            0,
        )
    assert player._attr_active_source == "raumfeld"
    assert player._attr_source_list[0].can_play_pause is True
    # MA only offers to resume a source that is paused
    assert player._attr_playback_state == PlaybackState.PAUSED


async def test_this_rooms_line_in_is_not_an_external_source() -> None:
    """The room's own Line-In is a source of its own, not something the host started."""
    player, _ = _external_player()
    player._line_in = ("http://192.168.1.42:8888/stream.flac", "Line-in")
    await player._update_zone_source(
        {"TrackURI": "http://192.168.1.42:8888/stream.flac", "TrackMetaData": ""},
        ["Bar"],
        0,
    )
    assert player._external_source is None


async def test_stopping_a_zone_source_keeps_it_for_resume() -> None:
    """Stop leaves a zone source loaded, and playing its media again resumes it."""
    player, host = _external_player()
    host.async_zone_stop = AsyncMock()
    player._set_external(_RADIO_META, "Stop", None, via_room=False)
    media = player._attr_current_media
    assert media is not None
    with (
        patch.object(player, "update_state"),
        patch.object(player, "_active_zone", return_value=["Bar"]),
        patch.object(player, "_transport_action", AsyncMock()) as action,
    ):
        await player.stop()
        stopped = (player._attr_active_source, player._attr_playback_state)
        await player.play_media(media)
    # idle right away, so MA does not wait to switch sources or announce; the next poll
    # reports the loaded source as paused again
    assert stopped == ("tunein", PlaybackState.IDLE)
    action.assert_awaited_with(_ZONE, "Play", Speed="1")
    assert player._attr_playback_state == PlaybackState.PLAYING


async def test_stopping_spotify_connect_ends_the_source() -> None:
    """Spotify Connect ends its session on a stop, so nothing stays loaded."""
    player, _ = _external_player()
    player._set_external(_SPOTIFY_META, "Pause", 315, via_room=True)
    with (
        patch.object(player, "update_state"),
        patch.object(player, "_transport_action", AsyncMock()) as action,
    ):
        await player.stop()
    action.assert_awaited_once_with(_RENDERER, "Stop")
    assert player._attr_active_source is None
    assert player._external_source is None


def test_skipping_needs_both_next_and_previous() -> None:
    """One capability covers both commands, so a host allowing only one gets neither."""
    player, _ = _external_player()
    player._set_external(_SPOTIFY_META, "Pause,Next", 315, via_room=True)
    assert player._attr_source_list[0].can_next_previous is False
    assert PlayerFeature.NEXT_PREVIOUS not in player.supported_features


async def test_an_external_source_music_assistant_gave_up_on_is_dropped() -> None:
    """After the core's idle timeout the source's controls and routing go too."""
    player, host = _external_player()
    host.async_zone_stop = AsyncMock()
    player._set_external(_RADIO_META, "Play", None, via_room=False)
    assert PlayerFeature.PAUSE in player.supported_features
    # what the core's mark_external_source_ended leaves behind
    player._attr_active_source = None
    player._attr_playback_state = PlaybackState.IDLE
    assert PlayerFeature.PAUSE not in player.supported_features
    with (
        patch.object(player, "update_state"),
        patch.object(player, "_active_zone", return_value=["Bar"]),
        patch.object(player, "_transport_action", AsyncMock()) as action,
    ):
        await player.stop()
    # a plain stop of the zone, not a pause of the expired source
    action.assert_awaited_once_with(_ZONE, "Stop")
    assert player._external_source is None
    assert player._attr_playback_state == PlaybackState.IDLE


def test_ending_an_external_source_drops_its_media() -> None:
    """The item reference of an ended source cannot be handed to MA as something to play."""
    player, _ = _external_player()
    player._set_external(_SPOTIFY_META, "Pause", 315, via_room=True)
    player._clear_external()
    assert player._attr_current_media is None


async def test_line_in_started_from_the_raumfeld_app_is_shown() -> None:
    """This room's Line-In playing is reported as the Line-In source, with its media."""
    player, _ = _external_player()
    player._line_in = ("http://192.168.1.42:8888/stream.flac", "Speaker Bar")
    player._set_external(_RADIO_META, "Stop", None, via_room=False)
    await player._update_zone_source(
        {"TrackURI": "http://192.168.1.42:8888/stream.flac", "TrackMetaData": ""},
        ["Bar"],
        0,
    )
    assert player._external_source is None
    assert player._attr_active_source == "line_in"
    assert player._attr_current_media is not None
    assert player._attr_current_media.title == "Line-in"


def test_a_follower_taken_by_spotify_connect_leaves_its_group() -> None:
    """Spotify Connect takes a room out of its zone, so it leaves the leader's group too."""
    player = _flow_player()
    player._player_id = "raumfeld_follower"
    leader = RaumfeldPlayer.__new__(RaumfeldPlayer)
    leader._player_id = "raumfeld_leader"
    leader._attr_group_members = ["raumfeld_leader", "raumfeld_follower"]
    mass = MagicMock()
    mass.players.get_player.return_value = leader
    player.mass = mass
    with (
        patch.object(RaumfeldPlayer, "synced_to", new_callable=PropertyMock) as synced_to,
        patch.object(RaumfeldPlayer, "update_state"),
    ):
        synced_to.return_value = "raumfeld_leader"
        player._leave_group()
    assert leader._attr_group_members == []


async def test_a_leader_taken_by_spotify_connect_dissolves_its_group() -> None:
    """A leader Spotify Connect took out of its zone no longer leads the other rooms."""
    player = _follower(PlaybackState.PLAYING)
    player._player_id = "raumfeld_leader"
    player._attr_group_members = ["raumfeld_leader", "raumfeld_follower"]
    provider = MagicMock()
    provider.host.room_is_spotify_single_room.return_value = True
    provider.host.async_get_room_volume = AsyncMock(return_value=30)
    provider.get_zone_for_room.return_value = None
    provider.room_renderer_location.return_value = None
    player._provider = provider
    player._external_source = None
    player._static_sources = []
    with (
        patch.object(RaumfeldPlayer, "synced_to", new_callable=PropertyMock) as synced_to,
        patch.object(RaumfeldPlayer, "update_state"),
        patch.object(player, "_poll_room_playback", AsyncMock()),
    ):
        synced_to.return_value = None
        await player.poll()
    assert player._attr_group_members == []


def test_adopting_an_external_source_drops_the_queued_next_item() -> None:
    """With flow mode off, the next MA item must not start when the external track ends."""
    player, _ = _external_player()
    with patch.object(player, "_clear_next") as clear_next:
        player._advance_armed = True
        player._set_external(_SPOTIFY_META, "Pause", 315, via_room=True)
    clear_next.assert_called_once()


async def test_a_reading_older_than_a_play_command_is_not_adopted() -> None:
    """A poll that read the zone before MA started its own item leaves that item alone."""
    player, _ = _external_player()

    async def actions_while_play_media_runs(_zone: list[str]) -> str:
        player._playback_generation += 1
        return "Pause,Next,Previous"

    with (
        patch.object(player, "_read_zone_actions", side_effect=actions_while_play_media_runs),
        patch.object(player, "_clear_next") as clear_next,
    ):
        await player._update_zone_source(
            {"TrackURI": "https://radio.example.com/stream", "TrackMetaData": ""},
            ["Bar"],
            0,
        )
    assert player._external_source is None
    clear_next.assert_not_called()


async def test_resuming_an_external_source_does_not_count_the_pause() -> None:
    """The position frozen at the pause runs on from the resume, not from the pause."""
    player, _ = _external_player()
    player._set_external(_SPOTIFY_META, "Play", 315, via_room=True)
    player._attr_playback_state = PlaybackState.PAUSED
    player._attr_elapsed_time = 70.0
    player._attr_elapsed_time_last_updated = time.time() - 10
    with (
        patch.object(player, "update_state"),
        patch.object(player, "_transport_action", AsyncMock()),
    ):
        await player.play()
    assert player._attr_elapsed_time == 70.0
    assert time.time() - player._attr_elapsed_time_last_updated < 1


async def test_a_poll_older_than_a_play_command_is_not_applied() -> None:
    """Readings taken before MA started its own item do not overwrite it."""
    player, _ = _external_player()
    player._attr_poll_interval = 1

    async def position_while_play_media_runs(*_args: object) -> dict[str, str]:
        player._playback_generation += 1
        return {"TrackURI": "https://radio.example.com/stream", "RelTime": "0:00:05"}

    with (
        patch.object(RaumfeldPlayer, "synced_to", new_callable=PropertyMock) as synced_to,
        patch.object(player, "update_state"),
        patch.object(player, "_update_power"),
        patch.object(player, "_plays_spotify_connect", return_value=False),
        patch.object(player, "_active_zone", return_value=["Bar"]),
        patch.object(player, "_update_volume", AsyncMock()),
        patch.object(player, "_read_transport", AsyncMock(return_value={})),
        patch.object(player, "_read_position", side_effect=position_while_play_media_runs),
        patch.object(player, "_update_zone_source", AsyncMock()) as update_source,
    ):
        synced_to.return_value = None
        await player.poll()
    update_source.assert_not_awaited()


async def test_a_play_command_during_the_source_read_drops_the_whole_reading() -> None:
    """Position and end-watch data read before MA started its own item are not applied."""
    player, _ = _external_player()

    async def source_read_while_play_media_runs(*_args: object) -> None:
        player._playback_generation += 1

    with (
        patch.object(player, "update_state"),
        patch.object(player, "_update_volume", AsyncMock()),
        patch.object(player, "_read_transport", AsyncMock(return_value={})),
        patch.object(player, "_read_position", AsyncMock(return_value={"RelTime": "0:05:00"})),
        patch.object(player, "_update_zone_source", side_effect=source_read_while_play_media_runs),
        patch.object(player, "_apply_position") as apply_position,
    ):
        await player._poll_zone(MagicMock(), ["Bar"])
    apply_position.assert_not_called()


def _flow_mode_on() -> Any:
    """Patch the flow mode setting on, as play_media reads it from the player config."""
    return patch.object(RaumfeldPlayer, "flow_mode", new_callable=PropertyMock, return_value=True)


def _zone_player() -> tuple[RaumfeldPlayer, AsyncMock]:
    """Build a player whose zone is addressable, with its transport actions mocked."""
    player, _ = _external_player()
    player._player_id = "raumfeld_bar"
    player._attr_playback_state = PlaybackState.IDLE
    mass = MagicMock()
    mass.streams.base_url = BASE_URL
    mass.streams.resolve_stream_url = AsyncMock(return_value=f"{BASE_URL}/flow/x.flac")
    player.mass = mass
    action = AsyncMock()
    player._transport_action = action  # type: ignore[method-assign]
    player._ensure_playable_zone = AsyncMock(return_value=["Bar"])  # type: ignore[method-assign]
    player._wake_rooms = AsyncMock()  # type: ignore[method-assign]
    return player, action


async def test_play_media_points_the_zone_at_the_stream() -> None:
    """The stream is set on the zone and started there."""
    player, action = _zone_player()
    with patch.object(player, "update_state"), _flow_mode_on():
        await player.play_media(_media())
    assert [c.args for c in action.await_args_list] == [
        (_ZONE, "SetAVTransportURI"),
        (_ZONE, "Play"),
    ]
    assert action.await_args_list[0].kwargs["CurrentURI"] == f"{BASE_URL}/flow/x.flac"
    assert player._attr_playback_state == PlaybackState.PLAYING


async def test_a_rejected_play_is_reported_instead_of_shown_as_playing() -> None:
    """A stream the host refuses fails the command and the player does not read as playing."""
    player, action = _zone_player()
    action.side_effect = [None, UpnpActionError(error_code=501, error_desc="Action Failed")]
    with _flow_mode_on(), pytest.raises(PlayerCommandFailed, match="Play failed on Bar"):
        await player.play_media(_media())
    assert player._attr_playback_state == PlaybackState.IDLE


async def test_a_stop_before_switching_streams_may_fail() -> None:
    """Re-pointing a playing zone goes on when its stop is refused."""
    player, action = _zone_player()
    player._attr_playback_state = PlaybackState.PLAYING
    action.side_effect = [UpnpActionError(error_code=501), None, None]
    with patch.object(player, "update_state"), _flow_mode_on():
        await player.play_media(_media())
    assert [c.args[1] for c in action.await_args_list] == ["Stop", "SetAVTransportURI", "Play"]


async def test_stopping_a_zone_that_already_stopped_is_fine() -> None:
    """A stop the zone answers with 'transition not available' is not an error."""
    player, action = _zone_player()
    player._attr_playback_state = PlaybackState.PLAYING
    action.side_effect = UpnpActionError(error_code=701, error_desc="Transition not available")
    with (
        patch.object(player, "update_state"),
        patch.object(player, "_active_zone", return_value=["Bar"]),
    ):
        await player.stop()
    assert player._attr_playback_state == PlaybackState.IDLE


async def test_a_failed_stop_is_reported() -> None:
    """A stop the zone refuses otherwise fails, rather than reporting the room as idle."""
    player, action = _zone_player()
    player._attr_playback_state = PlaybackState.PLAYING
    action.side_effect = UpnpActionError(error_code=501, error_desc="Action Failed")
    with (
        patch.object(player, "_active_zone", return_value=["Bar"]),
        pytest.raises(PlayerCommandFailed, match="Stop failed on Bar"),
    ):
        await player.stop()
    assert player._attr_playback_state == PlaybackState.PLAYING


def test_a_room_in_a_zone_does_not_play_spotify_connect() -> None:
    """A stale Spotify Connect flag does not take a grouped room out of its group."""
    player, host = _external_player()
    host.room_is_spotify_single_room.return_value = True
    with patch.object(player, "_active_zone", return_value=["Bank", "Bar"]):
        assert player._plays_spotify_connect() is False
    with patch.object(player, "_active_zone", return_value=None):
        assert player._plays_spotify_connect() is True


async def test_a_poll_during_a_play_command_is_not_applied() -> None:
    """Readings a poll takes while MA is still starting its own item are discarded."""
    player, _ = _external_player()
    player._playback_changes = 1
    with patch.object(player, "_read_zone_actions", AsyncMock(return_value="Pause")):
        await player._update_zone_source(
            {"TrackURI": "https://radio.example.com/stream", "TrackMetaData": ""},
            ["Bar"],
            player._playback_generation,
        )
    assert player._external_source is None


async def test_a_play_command_invalidates_polls_that_overlap_it() -> None:
    """A command bumps the generation when it starts and again when it ends."""
    player, _ = _external_player()
    player._set_external(_SPOTIFY_META, "Play", 315, via_room=True)
    player._attr_playback_state = PlaybackState.PAUSED
    generation = player._playback_generation
    with (
        patch.object(player, "update_state"),
        patch.object(player, "_transport_action", AsyncMock()),
    ):
        await player.play()
    assert player._playback_changes == 0
    assert player._playback_generation == generation + 2


def _taken_leader(queue_state: PlaybackState) -> tuple[RaumfeldPlayer, MagicMock]:
    """Build a group leader whose own queue is in the given state."""
    player = _flow_player()
    player._player_id = "raumfeld_bar"
    player._attr_group_members = ["raumfeld_bar", "raumfeld_bank"]
    queue = MagicMock(queue_id="raumfeld_bar", state=queue_state)
    mass = MagicMock()
    mass.player_queues.get_active_queue.return_value = queue
    player.mass = mass
    return player, mass


def test_a_leader_taken_by_spotify_connect_hands_its_queue_over() -> None:
    """The rooms that play on get the queue, the way MA does when a leader leaves."""
    player, mass = _taken_leader(PlaybackState.PLAYING)
    with (
        patch.object(RaumfeldPlayer, "synced_to", new_callable=PropertyMock) as synced_to,
        patch.object(RaumfeldPlayer, "update_state"),
    ):
        synced_to.return_value = None
        assert player._leave_group() is True
        # a later poll does not start a second handover
        assert player._leave_group() is True
    mass.players.cmd_ungroup.assert_called_once_with("raumfeld_bar")
    assert player._attr_group_members == ["raumfeld_bar", "raumfeld_bank"]


def test_a_leader_with_nothing_playing_just_leaves_its_group() -> None:
    """Without a playing queue there is nothing to hand over."""
    player, mass = _taken_leader(PlaybackState.IDLE)
    with (
        patch.object(RaumfeldPlayer, "synced_to", new_callable=PropertyMock) as synced_to,
        patch.object(RaumfeldPlayer, "update_state"),
    ):
        synced_to.return_value = None
        assert player._leave_group() is False
    mass.players.cmd_ungroup.assert_not_called()
    assert player._attr_group_members == []


async def test_the_stop_of_a_handover_leaves_spotify_connect_playing() -> None:
    """MA ending this room's queue during the handover does not stop Spotify Connect."""
    player, _ = _external_player()
    player._set_external(_SPOTIFY_META, "Pause,Stop", 315, via_room=True)
    player._handover_until = time.time() + 30
    with (
        patch.object(player, "update_state"),
        patch.object(player, "_transport_action", AsyncMock()) as action,
        patch.object(player, "_clear_next") as clear_next,
    ):
        await player.stop()
        action.assert_not_awaited()
        assert player._attr_active_source == "spotify"
        # with flow mode off, the queue's next item goes along with it
        clear_next.assert_called_once()
        # the next stop is a stop again
        await player.stop()
    action.assert_awaited_once_with(_RENDERER, "Stop")


async def test_pause_and_seek_invalidate_overlapping_polls() -> None:
    """A poll that read the zone before a pause or seek does not undo what they set."""
    player, _ = _external_player()
    player._set_external(_SPOTIFY_META, "Pause,Seek", 315, via_room=True)
    with (
        patch.object(player, "update_state"),
        patch.object(player, "_transport_action", AsyncMock()),
    ):
        generation = player._playback_generation
        await player.pause()
        assert not player._reading_is_current(generation)
        generation = player._playback_generation
        await player.seek(60)
        assert not player._reading_is_current(generation)
    assert player._playback_changes == 0


async def test_a_zone_switching_items_keeps_the_source() -> None:
    """The moment a zone reports no item while it switches does not end the source."""
    player, _ = _external_player()
    player._set_external(_RADIO_META, "Stop", None, via_room=False)
    with (
        patch.object(player, "update_state"),
        patch.object(player, "_update_volume", AsyncMock()),
        patch.object(
            player,
            "_read_transport",
            AsyncMock(return_value={"CurrentTransportState": "TRANSITIONING"}),
        ),
        patch.object(player, "_read_position", AsyncMock(return_value={"TrackURI": ""})),
    ):
        await player._poll_zone(MagicMock(), ["Bar"])
    assert player._attr_active_source == "tunein"


def test_the_other_rooms_stay_grouped_when_spotify_takes_the_leader() -> None:
    """Without a queue to hand over, the remaining rooms keep their group under one of them."""
    player, mass = _taken_leader(PlaybackState.IDLE)
    player._attr_group_members = ["raumfeld_bar", "raumfeld_bank", "raumfeld_kitchen"]
    bank = RaumfeldPlayer.__new__(RaumfeldPlayer)
    bank._player_id = "raumfeld_bank"
    bank._attr_group_members = []
    bank.mass = mass
    # leading rooms it did not lead before has its rate checked, which is not run here
    mass.create_task.side_effect = lambda coro: coro.close()
    mass.players.get_player.return_value = bank
    with (
        patch.object(RaumfeldPlayer, "synced_to", new_callable=PropertyMock) as synced_to,
        patch.object(RaumfeldPlayer, "update_state"),
    ):
        synced_to.return_value = None
        assert player._leave_group() is False
    assert player._attr_group_members == []
    assert bank._attr_group_members == ["raumfeld_bank", "raumfeld_kitchen"]


_SPOTIFY_TRACK_DIDL = (
    '<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" '
    'xmlns:raumfeld="urn:schemas-raumfeld-com:meta-data/raumfeld" '
    'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/" '
    'xmlns:dc="http://purl.org/dc/elements/1.1/">'
    '<item id="0/Spotify/Track" parentID="0/Spotify" restricted="0">'
    "<raumfeld:section>Spotify</raumfeld:section>"
    "<dc:title>Who's That</dc:title>"
    "<upnp:artist>The War On Drugs</upnp:artist>"
    "</item></DIDL-Lite>"
)


def _renderer_state(transport_state: str) -> dict[str, str]:
    """Return what a room's renderer reports in LastChange while Spotify Connect plays."""
    return {
        "TransportState": transport_state,
        "CurrentTransportActions": "Pause,Previous,Next,Seek",
        "AVTransportURIMetaData": _SPOTIFY_TRACK_DIDL,
        "CurrentTrackDuration": "0:05:15",
    }


async def _poll_renderer(
    player: RaumfeldPlayer, state: dict[str, str] | None, rel_time: str
) -> None:
    """Run one room-renderer poll on the given LastChange state and position."""
    with (
        patch.object(player, "update_state"),
        patch.object(player, "_update_volume", AsyncMock()),
        patch.object(player, "_read_room_state", AsyncMock(return_value=state)),
        patch.object(
            player,
            "_call_transport",
            AsyncMock(return_value={"RelTime": rel_time, "TrackDuration": "0:05:15"}),
        ),
    ):
        await player._poll_room_playback(MagicMock())


async def test_renderer_poll_adopts_spotify_connect() -> None:
    """A renderer playing Spotify Connect is reported as that source, with its track."""
    player, _ = _external_player()
    player._attr_playback_state = PlaybackState.IDLE
    await _poll_renderer(player, _renderer_state("PLAYING"), "0:01:00")
    assert player._attr_playback_state == PlaybackState.PLAYING
    assert player._attr_active_source == "spotify"
    assert player._external_via_room is True
    assert player._attr_current_media is not None
    assert player._attr_current_media.title == "Who's That"
    assert player._attr_elapsed_time == 60.0
    assert PlayerFeature.PAUSE in player.supported_features


async def test_renderer_poll_keeps_a_paused_spotify_session() -> None:
    """A paused session stays the active source, at the position it paused at."""
    player, _ = _external_player()
    await _poll_renderer(player, _renderer_state("PLAYING"), "0:01:00")
    await _poll_renderer(player, _renderer_state("PAUSED_PLAYBACK"), "0:01:30")
    assert player._attr_playback_state == PlaybackState.PAUSED
    assert player._attr_active_source == "spotify"
    assert player._attr_elapsed_time == 90.0


async def test_renderer_poll_drops_an_ended_spotify_session() -> None:
    """A renderer that stopped no longer reports Spotify Connect as the source."""
    player, _ = _external_player()
    await _poll_renderer(player, _renderer_state("PLAYING"), "0:01:00")
    await _poll_renderer(player, _renderer_state("STOPPED"), "0:00:00")
    assert player._attr_playback_state == PlaybackState.IDLE
    assert player._external_source is None
    assert player._attr_active_source is None


async def test_renderer_poll_overlapping_a_command_is_not_applied() -> None:
    """A renderer reading taken while a command changed what plays is discarded."""
    player, _ = _external_player()
    player._attr_playback_state = PlaybackState.IDLE

    async def state_while_a_command_runs(*_args: object) -> dict[str, str]:
        player._playback_generation += 1
        return _renderer_state("PLAYING")

    with (
        patch.object(player, "update_state"),
        patch.object(player, "_update_volume", AsyncMock()),
        patch.object(player, "_read_room_state", side_effect=state_while_a_command_runs),
        patch.object(player, "_call_transport", AsyncMock(return_value={"RelTime": "0:01:00"})),
    ):
        await player._poll_room_playback(MagicMock())
    assert player._external_source is None
    assert player._attr_playback_state == PlaybackState.IDLE


def test_a_paused_queue_is_handed_over_too() -> None:
    """A leader taken while its queue is paused hands that queue over, as MA does."""
    player, mass = _taken_leader(PlaybackState.PAUSED)
    with (
        patch.object(RaumfeldPlayer, "synced_to", new_callable=PropertyMock) as synced_to,
        patch.object(RaumfeldPlayer, "update_state"),
    ):
        synced_to.return_value = None
        assert player._leave_group() is True
    mass.players.cmd_ungroup.assert_called_once_with("raumfeld_bar")


async def test_a_negative_seek_is_reported_as_the_start() -> None:
    """A seek before the start goes to 0:00, and the player reports that position."""
    player, _ = _external_player()
    player._set_external(_SPOTIFY_META, "Seek", 315, via_room=True)
    with (
        patch.object(player, "update_state"),
        patch.object(player, "_transport_action", AsyncMock()) as action,
    ):
        await player.seek(-5)
    action.assert_awaited_once_with(_RENDERER, "Seek", Unit="ABS_TIME", Target="00:00:00")
    assert player._attr_elapsed_time == 0.0


async def test_stopping_a_paused_source_keeps_its_position() -> None:
    """A stop on a source that is already paused does not count the paused time as played."""
    player, host = _external_player()
    host.async_zone_stop = AsyncMock()
    player._set_external(_RADIO_META, "Play", None, via_room=False)
    player._attr_playback_state = PlaybackState.PAUSED
    player._attr_elapsed_time = 70.0
    player._attr_elapsed_time_last_updated = time.time() - 10
    with (
        patch.object(player, "update_state"),
        patch.object(player, "_active_zone", return_value=["Bar"]),
        patch.object(player, "_transport_action", AsyncMock()),
    ):
        await player.stop()
    assert player._attr_elapsed_time == 70.0


async def test_a_stop_for_an_announcement_reports_idle() -> None:
    """MA waits for an idle room before it announces, so a loaded source is not kept paused."""
    player, host = _external_player()
    host.async_zone_stop = AsyncMock()
    player._set_external(_RADIO_META, "Stop", None, via_room=False)
    player._extra_data = {"announcement_in_progress": True}
    with (
        patch.object(player, "update_state"),
        patch.object(player, "_active_zone", return_value=["Bar"]),
        patch.object(player, "_transport_action", AsyncMock()),
        patch.object(player, "_read_zone_actions", AsyncMock(return_value="Play")),
    ):
        await player.stop()
        assert player._attr_playback_state == PlaybackState.IDLE
        # a poll during the announcement does not turn the loaded source back to paused
        await player._update_zone_source(
            {"TrackURI": "https://radio.example.com/stream", "TrackMetaData": ""}, ["Bar"], 0
        )
    assert player._attr_playback_state == PlaybackState.IDLE


def _zone_transport(media_info: dict[str, str], position: dict[str, str]) -> AsyncMock:
    """Build a zone transport that answers the resume-context reads with the given info."""

    async def transport(_location: str, action: str, **_arguments: str) -> dict[str, str]:
        return {"GetMediaInfo": media_info, "GetPositionInfo": position}.get(action, {})

    return AsyncMock(side_effect=transport)


async def _announce_and_resume(
    media_info: dict[str, str], position: dict[str, str]
) -> tuple[RaumfeldPlayer, AsyncMock]:
    """Stop a zone source for an announcement, let the announcement take the zone, resume."""
    player, host = _external_player()
    host.async_zone_stop = AsyncMock()
    player._set_external(_RADIO_META, "Stop", None, via_room=False)
    media = player._attr_current_media
    assert media is not None
    action = _zone_transport(media_info, position)
    player._extra_data = {"announcement_in_progress": True}
    with (
        patch.object(player, "update_state"),
        patch.object(player, "_active_zone", return_value=["Bar"]),
        patch.object(player, "_ensure_playable_zone", AsyncMock(return_value=["Bar"])),
        patch.object(player, "_wake_rooms", AsyncMock()),
        patch.object(player, "_transport_action", action),
    ):
        await player.stop()
        # the announcement plays an MA stream on the zone, which ends the external source
        player._clear_external()
        player._extra_data = {}
        action.reset_mock()
        await player.play_media(media)
    return player, action


async def test_radio_is_restored_after_an_announcement() -> None:
    """MA's resume after an announcement starts the station the zone played before."""
    player, action = await _announce_and_resume(
        {"CurrentURI": "dlna-playsingle://server?iid=station", "CurrentURIMetaData": "<DIDL/>"},
        {"Track": "1", "RelTime": "0:00:10", "TrackDuration": "0:00:00"},
    )
    assert [c.args[1] for c in action.await_args_list] == ["SetAVTransportURI", "Play"]
    assert action.await_args_list[0].kwargs == {
        "CurrentURI": "dlna-playsingle://server?iid=station",
        "CurrentURIMetaData": "<DIDL/>",
    }
    assert player._attr_playback_state == PlaybackState.PLAYING


async def test_an_album_is_restored_at_its_track_and_position() -> None:
    """An album comes back at the track and position it was stopped at."""
    _, action = await _announce_and_resume(
        {"CurrentURI": "dlna-playcontainer://server?cid=album&fii=0", "CurrentURIMetaData": ""},
        {"Track": "3", "RelTime": "0:00:48", "TrackDuration": "0:04:00"},
    )
    assert [c.args[1] for c in action.await_args_list] == ["SetAVTransportURI", "Play", "Seek"]
    assert action.await_args_list[0].kwargs["CurrentURI"].endswith("fii=2")
    assert action.await_args_list[2].kwargs == {"Unit": "ABS_TIME", "Target": "0:00:48"}


async def test_spotify_connect_ended_by_an_announcement_is_not_replayed() -> None:
    """A Spotify session the announcement ended is left idle, not handed to MA's streams."""
    player, _ = _external_player()
    player._set_external(_SPOTIFY_META, "Pause,Stop", 315, via_room=True)
    media = player._attr_current_media
    assert media is not None
    player._extra_data = {"announcement_in_progress": True}
    mass = MagicMock()
    mass.streams.resolve_stream_url = AsyncMock()
    player.mass = mass
    with (
        patch.object(player, "update_state"),
        patch.object(player, "_transport_action", AsyncMock()) as action,
    ):
        await player.stop()
        player._extra_data = {}
        action.reset_mock()
        await player.play_media(media)
    action.assert_not_awaited()
    mass.streams.resolve_stream_url.assert_not_awaited()
    assert player._attr_playback_state == PlaybackState.IDLE


_BUTTONS = [
    (1, "0/RadioTime/Search/s-s25777", "NPO Radio 2", False),
    (3, "0/My Music/Albums/42", "Shine On", True),
]


def _button_player() -> tuple[RaumfeldPlayer, MagicMock]:
    """Build a RaumfeldPlayer with two station buttons offered as sources."""
    player, host = _external_player()
    with patch.object(RaumfeldPlayer, "update_state"):
        player.set_station_buttons(_BUTTONS)
    # a read just before playing finds the buttons unchanged
    cast("MagicMock", player._provider).refresh_station_buttons = AsyncMock()
    return player, host


def test_station_buttons_are_offered_as_sources() -> None:
    """Each assigned button is a source of its own, named after its number and title."""
    player, _ = _button_player()
    assert [(s.id, s.name) for s in player._attr_source_list] == [
        ("station_button_1", "Button 1: NPO Radio 2"),
        ("station_button_3", "Button 3: Shine On"),
    ]
    assert PlayerFeature.SELECT_SOURCE in player._attr_supported_features
    assert player._library_sources["station_button_3"] == ("0/My Music/Albums/42", True)


async def test_a_station_button_is_played_through_its_library_item() -> None:
    """Choosing a button has the zone play the button's item, shown as that button."""
    player, host = _button_player()
    host.media_server_udn = "uuid:media-server"
    host.async_browse_media_server = AsyncMock(return_value="<DIDL-Lite/>")
    with (
        patch.object(player, "_ensure_playable_zone", AsyncMock(return_value=["Bar"])),
        patch.object(player, "_wake_rooms", AsyncMock()),
        patch.object(player, "_stop_before_switching", AsyncMock()),
        patch.object(player, "_zone_command", AsyncMock()) as command,
        patch.object(player, "update_state"),
    ):
        await player.select_source("station_button_3")
    set_uri, play = command.await_args_list
    assert set_uri.args[1] == "SetAVTransportURI"
    assert set_uri.kwargs["CurrentURI"].startswith("dlna-playcontainer://")
    assert set_uri.kwargs["CurrentURIMetaData"] == "<DIDL-Lite/>"
    assert play.args[1] == "Play"
    assert player._attr_active_source == "station_button_3"
    assert player._attr_current_media is not None
    assert player._attr_current_media.title == "Shine On"


async def test_a_button_given_another_item_plays_the_new_one() -> None:
    """The buttons are read again before playing, so a reassigned button plays its new item."""
    player, host = _button_player()
    host.media_server_udn = "uuid:media-server"
    host.async_browse_media_server = AsyncMock(return_value="<DIDL-Lite/>")

    async def reassigned(target: RaumfeldPlayer) -> None:
        target.set_station_buttons([_BUTTONS[0], (3, "0/StationButtons/432", "WYWH 50", True)])

    cast("MagicMock", player._provider).refresh_station_buttons = AsyncMock(side_effect=reassigned)
    with (
        patch.object(player, "_ensure_playable_zone", AsyncMock(return_value=["Bar"])),
        patch.object(player, "_wake_rooms", AsyncMock()),
        patch.object(player, "_stop_before_switching", AsyncMock()),
        patch.object(player, "_zone_command", AsyncMock()) as command,
        patch.object(player, "update_state"),
    ):
        await player.select_source("station_button_3")
    assert command.await_args_list[0].kwargs["CurrentURI"] == library_uri(
        "uuid:media-server", "0/StationButtons/432", container=True
    )
    assert player._attr_current_media is not None
    assert player._attr_current_media.title == "WYWH 50"


async def test_a_button_emptied_meanwhile_is_refused() -> None:
    """A button that lost its item since the last read plays nothing and says so."""
    player, _ = _button_player()

    async def emptied(target: RaumfeldPlayer) -> None:
        target.set_station_buttons([_BUTTONS[0]])

    cast("MagicMock", player._provider).refresh_station_buttons = AsyncMock(side_effect=emptied)
    with (
        patch.object(player, "_zone_command", AsyncMock()) as command,
        patch.object(player, "update_state"),
        pytest.raises(PlayerCommandFailed, match="nothing assigned"),
    ):
        await player.select_source("station_button_3")
    command.assert_not_awaited()


async def test_a_button_whose_item_is_gone_is_not_sent_to_the_zone() -> None:
    """An item the host cannot describe any more raises instead of loading nothing."""
    player, host = _button_player()
    host.media_server_udn = "uuid:media-server"
    host.async_browse_media_server = AsyncMock(return_value=None)
    with (
        patch.object(player, "_ensure_playable_zone", AsyncMock(return_value=["Bar"])),
        patch.object(player, "_zone_command", AsyncMock()) as command,
        patch.object(player, "update_state"),
        pytest.raises(PlayerCommandFailed, match="not available"),
    ):
        await player.select_source("station_button_3")
    command.assert_not_awaited()


async def test_an_unknown_source_is_refused() -> None:
    """A source that is neither the Line-In nor a station button raises."""
    player, _ = _button_player()
    with pytest.raises(PlayerCommandFailed, match="Unknown source"):
        await player.select_source("station_button_2")


def test_a_station_button_pressed_on_the_speaker_shows_as_that_button() -> None:
    """The host playing a button's item reports that button, not the service behind it."""
    player, _ = _button_player()
    with patch.object(RaumfeldPlayer, "update_state"):
        player._set_external(_RADIO_META, "Stop", None, via_room=False)
    assert player._attr_active_source == "station_button_1"
    assert [s.id for s in player._attr_source_list] == ["station_button_1", "station_button_3"]
    assert not player._attr_source_list[0].passive


def test_a_playing_button_stays_listed_once_when_the_buttons_are_read_again() -> None:
    """A refresh keeps the playing button's live source, with its controls, in its place."""
    player, _ = _button_player()
    with patch.object(RaumfeldPlayer, "update_state"):
        player._set_external(_RADIO_META, "Stop,Pause,Play", None, via_room=False)
        # renamed in the Raumfeld app meanwhile
        renamed = [(1, "0/RadioTime/Search/s-s25777", "NPO 2", False), _BUTTONS[1]]
        player.set_station_buttons(renamed)
    assert [s.id for s in player._attr_source_list] == ["station_button_1", "station_button_3"]
    assert player._attr_source_list[0].can_play_pause
    assert [s.name for s in player._static_sources] == ["Button 1: NPO 2", "Button 3: Shine On"]


def test_a_playing_button_that_was_removed_is_no_longer_offered() -> None:
    """A button taken off in the Raumfeld app stays listed while it plays, not selectable."""
    player, _ = _button_player()
    with patch.object(RaumfeldPlayer, "update_state"):
        player._set_external(_RADIO_META, "Stop,Pause,Play", None, via_room=False)
        player.set_station_buttons([_BUTTONS[1]])
    assert [(s.id, s.passive) for s in player._attr_source_list] == [
        ("station_button_3", False),
        ("station_button_1", True),
    ]


def test_a_chosen_button_stays_shown_when_another_button_has_the_same_station() -> None:
    """Two buttons with one station: the poll keeps the one that was chosen."""
    player, _ = _external_player()
    with patch.object(RaumfeldPlayer, "update_state"):
        player.set_station_buttons([_BUTTONS[0], (2, *_BUTTONS[0][1:])])
        player._attr_active_source = "station_button_2"
        player._set_external(_RADIO_META, "Stop", None, via_room=False)
        assert player._attr_active_source == "station_button_2"
        # with nothing chosen, the first button with the station is shown
        player._attr_active_source = None
        player._set_external(_RADIO_META, "Stop", None, via_room=False)
    assert player._attr_active_source == "station_button_1"


def test_an_album_button_pressed_on_the_speaker_shows_as_that_button() -> None:
    """An album button is recognised by the album the zone loaded, not the playing track."""
    player, _ = _button_player()
    track = {**_RADIO_META, "section": "My Music", "item_id": "0/My Music/Albums/42/7"}
    with patch.object(RaumfeldPlayer, "update_state"):
        player._set_external(
            track, "Stop", 240, via_room=False, loaded_container="0/My Music/Albums/42"
        )
    assert player._attr_active_source == "station_button_3"


async def test_the_poll_recognises_an_album_button_by_what_the_zone_loaded() -> None:
    """Polling a zone that plays an album button's album reports that button."""
    player, host = _button_player()
    host.media_server_udn = "uuid:media-server"
    album_uri = library_uri("uuid:media-server", "0/My Music/Albums/42", container=True)
    track_meta = (
        '<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/"'
        ' xmlns:dc="http://purl.org/dc/elements/1.1/"'
        ' xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/">'
        '<item id="0/My Music/Albums/42/7"><dc:title>Shine On Pt. 1</dc:title>'
        "<upnp:class>object.item.audioItem.musicTrack</upnp:class></item></DIDL-Lite>"
    )
    position = {
        "TrackURI": "http://192.168.1.41:47366/track7.flac",
        "TrackMetaData": track_meta,
        "TrackDuration": "0:04:00",
    }
    with (
        patch.object(player, "_read_zone_actions", AsyncMock(return_value="Stop,Pause")),
        patch.object(
            player, "_call_transport", AsyncMock(return_value={"CurrentURI": album_uri})
        ) as call,
        patch.object(RaumfeldPlayer, "update_state"),
    ):
        await player._update_zone_source(position, ["Bar"], player._playback_generation)
    call.assert_any_await(_ZONE, "GetMediaInfo")
    assert player._attr_active_source == "station_button_3"


async def _stop_a_playing_album_button(player: RaumfeldPlayer) -> AsyncMock:
    """Stop an album button for an announcement, let the announcement take the zone."""
    album_uri = "dlna-playcontainer://server?cid=0%2FMy%20Music%2FAlbums%2F42&fii=0"
    meta = {**_RADIO_META, "item_id": "0/My Music/Albums/42/7", "section": None}
    with patch.object(RaumfeldPlayer, "update_state"):
        player._set_external(
            meta, "Stop", 240, via_room=False, loaded_container="0/My Music/Albums/42"
        )
    assert player._attr_active_source == "station_button_3"
    action = _zone_transport(
        {"CurrentURI": album_uri, "CurrentURIMetaData": ""},
        {"Track": "3", "RelTime": "0:00:48", "TrackDuration": "0:04:00"},
    )
    player._extra_data = {"announcement_in_progress": True}
    with (
        patch.object(player, "update_state"),
        patch.object(player, "_active_zone", return_value=["Bar"]),
        patch.object(player, "_transport_action", action),
    ):
        await player.stop()
    player._clear_external()
    action.reset_mock()
    return action


async def test_an_album_button_goes_on_where_it_was_after_an_announcement() -> None:
    """MA selects the button again after an announcement: it resumes, not restarts."""
    player, host = _button_player()
    host.async_zone_stop = AsyncMock()
    host.async_browse_media_server = AsyncMock(return_value="<DIDL-Lite/>")
    action = await _stop_a_playing_album_button(player)
    with (
        patch.object(player, "update_state"),
        patch.object(player, "_ensure_playable_zone", AsyncMock(return_value=["Bar"])),
        patch.object(player, "_wake_rooms", AsyncMock()),
        patch.object(player, "_transport_action", action),
        patch.object(player, "_zone_command", AsyncMock()) as command,
    ):
        # the controller's resume after the announcement, flag still set
        await player.select_source("station_button_3")
    command.assert_not_awaited()
    assert [c.args[1] for c in action.await_args_list] == ["SetAVTransportURI", "Play", "Seek"]
    assert action.await_args_list[0].kwargs["CurrentURI"].endswith("fii=2")
    assert action.await_args_list[2].kwargs == {"Unit": "ABS_TIME", "Target": "0:00:48"}


async def test_an_album_button_chosen_by_the_user_starts_afresh() -> None:
    """Outside an announcement, choosing the button starts its album from the beginning."""
    player, host = _button_player()
    host.async_zone_stop = AsyncMock()
    host.media_server_udn = "uuid:media-server"
    host.async_browse_media_server = AsyncMock(return_value="<DIDL-Lite/>")
    action = await _stop_a_playing_album_button(player)
    player._extra_data = {}
    with (
        patch.object(player, "update_state"),
        patch.object(player, "_ensure_playable_zone", AsyncMock(return_value=["Bar"])),
        patch.object(player, "_wake_rooms", AsyncMock()),
        patch.object(player, "_stop_before_switching", AsyncMock()),
        patch.object(player, "_transport_action", action),
        patch.object(player, "_zone_command", AsyncMock()) as command,
    ):
        await player.select_source("station_button_3")
    action.assert_not_awaited()
    assert command.await_args_list[0].kwargs["CurrentURI"] == library_uri(
        "uuid:media-server", "0/My Music/Albums/42", container=True
    )


@pytest.mark.parametrize(
    ("play_mode", "shuffle", "repeat"),
    [
        ("NORMAL", False, RepeatMode.OFF),
        ("SHUFFLE", True, RepeatMode.OFF),
        ("REPEAT_ALL", False, RepeatMode.ALL),
        ("RANDOM", True, RepeatMode.ALL),
        ("REPEAT_ONE", False, RepeatMode.ONE),
    ],
)
def test_an_external_source_shows_its_shuffle_and_repeat(
    play_mode: str, shuffle: bool, repeat: RepeatMode
) -> None:
    """The play mode the host reports shows as the source's shuffle and repeat state."""
    player, _ = _external_player()
    player._set_external(
        _SPOTIFY_META, "Pause,Next,Previous,Repeat,Shuffle", 315, via_room=True, play_mode=play_mode
    )
    source = player._attr_source_list[0]
    assert (source.can_shuffle, source.can_repeat) == (True, True)
    assert (source.shuffle_enabled, source.repeat_mode) == (shuffle, repeat)


def test_a_source_the_host_does_not_let_shuffle_offers_no_shuffle() -> None:
    """Without Shuffle and Repeat in the allowed actions the source offers neither."""
    player, _ = _external_player()
    player._set_external(_RADIO_META, "Stop", None, via_room=False, play_mode="NORMAL")
    source = player._attr_source_list[0]
    assert (source.can_shuffle, source.can_repeat) == (False, False)


@pytest.mark.parametrize(
    ("current", "command", "expected"),
    [
        ("NORMAL", ("shuffle", True), "SHUFFLE"),
        ("REPEAT_ALL", ("shuffle", True), "RANDOM"),
        ("RANDOM", ("shuffle", False), "REPEAT_ALL"),
        # the Raumfeld app turns repeat one into repeat all when shuffle goes on
        ("REPEAT_ONE", ("shuffle", True), "RANDOM"),
        ("SHUFFLE", ("repeat", RepeatMode.ALL), "RANDOM"),
        ("RANDOM", ("repeat", RepeatMode.OFF), "SHUFFLE"),
        # no mode shuffles and repeats one track
        ("SHUFFLE", ("repeat", RepeatMode.ONE), "REPEAT_ONE"),
    ],
)
async def test_shuffle_and_repeat_set_the_play_mode(
    current: str, command: tuple[str, Any], expected: str
) -> None:
    """Shuffle and repeat are combined into the one play mode the renderer takes."""
    player, _ = _external_player()
    player._set_external(
        _ALBUM_META,
        "Pause,Next,Previous,RepeatTrack,Repeat,Shuffle",
        240,
        via_room=False,
        play_mode=current,
    )
    name, value = command
    with (
        patch.object(player, "_external_command", AsyncMock()) as external,
        patch.object(player, "update_state"),
    ):
        if name == "shuffle":
            await player.set_shuffle(value)
        else:
            await player.set_repeat(value)
    external.assert_awaited_once_with("SetPlayMode", NewPlayMode=expected)
    source = player._external_source
    assert source is not None
    assert (
        play_mode_for(bool(source.shuffle_enabled), source.repeat_mode or RepeatMode.OFF)
        == expected
    )


async def test_repeat_one_moves_on_to_off_where_the_host_does_not_allow_it() -> None:
    """Spotify Connect lists Repeat but not RepeatTrack: the repeat button skips one."""
    player, _ = _external_player()
    player._set_external(
        _SPOTIFY_META, "Pause,Next,Previous,Repeat,Shuffle", 315, via_room=True, play_mode="RANDOM"
    )
    with (
        patch.object(player, "_external_command", AsyncMock()) as external,
        patch.object(player, "update_state"),
    ):
        await player.set_repeat(RepeatMode.ONE)
    # off, with shuffle kept
    external.assert_awaited_once_with("SetPlayMode", NewPlayMode="SHUFFLE")
    source = player._external_source
    assert source is not None
    assert source.repeat_mode == RepeatMode.OFF


async def test_the_zone_poll_reads_the_play_mode_of_an_external_source() -> None:
    """A zone that plays something from the Raumfeld app is asked for its play mode."""
    player, _ = _external_player()
    position = {"TrackURI": "http://192.168.1.41:47366/track7.flac", "TrackDuration": "0:04:00"}

    async def transport(_location: str, action: str) -> dict[str, str]:
        return {
            "GetCurrentTransportActions": {"Actions": "Pause,Next,Previous,Repeat,Shuffle"},
            "GetTransportSettings": {"PlayMode": "RANDOM"},
        }.get(action, {})

    with (
        patch.object(player, "_call_transport", AsyncMock(side_effect=transport)),
        patch.object(RaumfeldPlayer, "update_state"),
    ):
        await player._update_zone_source(position, ["Bar"], player._playback_generation)
    source = player._external_source
    assert source is not None
    assert (source.shuffle_enabled, source.repeat_mode) == (True, RepeatMode.ALL)


async def test_shuffle_and_repeat_at_once_do_not_undo_each_other() -> None:
    """Two changes arriving together end in the play mode that holds both."""
    player, _ = _external_player()
    player._set_external(
        _ALBUM_META,
        "Pause,Next,Previous,RepeatTrack,Repeat,Shuffle",
        240,
        via_room=False,
        play_mode="NORMAL",
    )
    sent: list[str] = []

    async def slow_command(_action: str, **arguments: str) -> None:
        # the renderer takes a moment to answer
        await asyncio.sleep(0)
        sent.append(arguments["NewPlayMode"])

    with (
        patch.object(player, "_external_command", AsyncMock(side_effect=slow_command)),
        patch.object(player, "update_state"),
    ):
        await asyncio.gather(player.set_shuffle(True), player.set_repeat(RepeatMode.ALL))
    assert sent == ["SHUFFLE", "RANDOM"]


async def test_a_poll_from_before_a_play_mode_change_is_not_applied() -> None:
    """A room poll that read the old play mode while the change ran leaves the new one."""
    player, _ = _external_player()
    player._set_external(
        _SPOTIFY_META, "Pause,Next,Previous,Repeat,Shuffle", 315, via_room=True, play_mode="NORMAL"
    )
    position_asked = asyncio.Event()
    release = asyncio.Event()

    async def slow_position(_location: str, _action: str) -> dict[str, str]:
        position_asked.set()
        await release.wait()
        return {"TrackDuration": "0:05:15", "RelTime": "0:00:20"}

    old_state = {
        "TransportState": "PLAYING",
        "CurrentTransportActions": "Pause,Next,Previous,Repeat,Shuffle",
        "CurrentPlayMode": "NORMAL",
        "AVTransportURIMetaData": _SPOTIFY_TRACK_DIDL,
    }
    with (
        patch.object(player, "_update_volume", AsyncMock()),
        patch.object(player, "_read_room_state", AsyncMock(return_value=old_state)),
        patch.object(player, "_call_transport", AsyncMock(side_effect=slow_position)),
        patch.object(player, "_external_command", AsyncMock()),
        patch.object(RaumfeldPlayer, "update_state"),
    ):
        poll = asyncio.create_task(player._poll_room_playback(MagicMock()))
        await position_asked.wait()
        await player.set_shuffle(True)
        release.set()
        await poll
    source = player._external_source
    assert source is not None
    assert source.shuffle_enabled is True


async def test_a_play_mode_change_after_ma_took_over_is_refused() -> None:
    """A change that waited while MA's own playback replaced the source sends nothing."""
    player, _ = _external_player()
    player._set_external(
        _ALBUM_META,
        "Pause,Next,Previous,RepeatTrack,Repeat,Shuffle",
        240,
        via_room=False,
        play_mode="NORMAL",
    )
    lock = player._play_mode_guard()
    await lock.acquire()
    with (
        patch.object(player, "_external_command", AsyncMock()) as external,
        patch.object(player, "update_state"),
    ):
        change = asyncio.create_task(player.set_repeat(RepeatMode.ALL))
        await asyncio.sleep(0)
        # play_media() starts MA's own stream meanwhile
        player._clear_external()
        lock.release()
        with pytest.raises(PlayerCommandFailed, match="no longer plays"):
            await change
    external.assert_not_awaited()
