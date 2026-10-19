"""Tests for the Teufel Raumfeld player: position, grouping state and transport reads."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import pytest
from music_assistant_models.enums import IdentifierType, PlaybackState, PlayerFeature
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
    player._external_source = None
    player._extra_data = {}
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
    player._resync_task_id = "raumfeld_resync_test"
    player._on_unload_callbacks = []

    await player.on_unload()

    assert {call.args[0] for call in mass.cancel_timer.call_args_list} == {
        "raumfeld_advance_test",
        "raumfeld_resync_test",
        "external_pause_raumfeld_test",
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


_SPOTIFY_META: dict[str, str | None] = {
    "title": "Who's That",
    "artist": "The War On Drugs",
    "album": "Who's That",
    "image_url": "http://i.scdn.co/image/cover",
    "section": "Spotify",
    "item_id": "0/Spotify/Track",
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
    action.assert_not_awaited()
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
    ):
        await player.stop()
        action.assert_not_awaited()
        assert player._attr_active_source == "spotify"
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
