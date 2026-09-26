"""Tests for the Teufel Raumfeld flow-position and transport-read helpers."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, MagicMock, patch

from music_assistant_models.enums import PlaybackState

from music_assistant.providers.raumfeld.player import (
    FLOW_RESET_THRESHOLD,
    RaumfeldPlayer,
    _map_transport_state,
)

if TYPE_CHECKING:
    import hassfeld

BASE_URL = "http://mass.local:8097"


def _flow_player() -> RaumfeldPlayer:
    """Build a RaumfeldPlayer with just the state the position path touches."""
    player = RaumfeldPlayer.__new__(RaumfeldPlayer)
    player._flow_offset = 0.0
    player._last_reltime = 0.0
    player._attr_elapsed_time = None
    player._attr_elapsed_time_last_updated = None
    mass = MagicMock()
    mass.streams.base_url = BASE_URL
    player.mass = mass
    return player


def _pos(reltime: str, uri: str = f"{BASE_URL}/flow/x/y/z.flac") -> dict[str, str]:
    return {"TrackURI": uri, "RelTime": reltime, "TrackDuration": "0:00:00"}


def test_flow_position_is_continuous_within_a_track() -> None:
    """While a track plays, the reported position tracks the zone clock."""
    player = _flow_player()
    player._apply_position(_pos("0:00:10"), playing=True)
    assert player._attr_elapsed_time == 10.0
    assert player._flow_offset == 0.0


def test_flow_position_survives_a_mid_flow_drop() -> None:
    """If the zone clock drops back mid-flow, the position MA sees must keep climbing."""
    player = _flow_player()
    # first track plays up to 3:00
    player._apply_position(_pos("0:03:00"), playing=True)
    assert player._attr_elapsed_time == 180.0
    # the zone clock unexpectedly drops back to ~0 without a new play command
    player._apply_position(_pos("0:00:02"), playing=True)
    # the finished track's 180s is banked, so the flow position is 180 + 2
    assert player._flow_offset == 180.0
    assert player._attr_elapsed_time == 182.0


def test_flow_position_ignores_sub_threshold_dips() -> None:
    """A tiny backwards step (whole-second rounding) is not banked as a drop."""
    player = _flow_player()
    player._apply_position(_pos("0:01:00"), playing=True)
    dip = f"0:00:{60 - int(FLOW_RESET_THRESHOLD):02d}"  # within the threshold
    player._apply_position(_pos(dip), playing=True)
    assert player._flow_offset == 0.0


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
