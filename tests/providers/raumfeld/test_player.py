"""Tests for the Teufel Raumfeld player: position, grouping state and transport reads."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

from music_assistant_models.enums import PlaybackState

from music_assistant.providers.raumfeld.player import (
    IDLE_POLL_INTERVAL,
    PLAYING_POLL_INTERVAL,
    RaumfeldPlayer,
    _map_transport_state,
)

if TYPE_CHECKING:
    import hassfeld

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


def test_requires_flow_mode() -> None:
    """The provider always plays through flow mode."""
    assert RaumfeldPlayer.__new__(RaumfeldPlayer).requires_flow_mode is True


def _follower(leader_state: PlaybackState) -> RaumfeldPlayer:
    """Build a grouped follower whose leader reports the given playback state."""
    player = RaumfeldPlayer.__new__(RaumfeldPlayer)
    player.room = "Bank"
    player._player_id = "raumfeld_follower"
    player._attr_playback_state = PlaybackState.PLAYING
    player._attr_poll_interval = PLAYING_POLL_INTERVAL
    provider = MagicMock()
    provider.host.async_get_room_volume = AsyncMock(return_value=30)
    player._provider = provider
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
