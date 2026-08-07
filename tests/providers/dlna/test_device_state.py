"""Tests for the state the DLNA player reads from its device."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock

import pytest
from async_upnp_client.profiles.dlna import TransportState

from music_assistant.providers.dlna.player import DLNAPlayer
from tests.common import MockProvider

REPORTED_AT = datetime(2026, 8, 7, 12, 0, 0, tzinfo=UTC)
STALE_POSITION = 120.0
STALE_REPORTED_AT = 1000.0


async def _updated_player(**device_state: Any) -> DLNAPlayer:
    """
    Run a state update on a player that already knows a position, and return it.

    :param device_state: Attributes to override on the fully reporting mocked device.
    """
    provider = MockProvider("dlna", instance_id="dlna_test")
    provider.mass.streams.base_url = "http://192.168.1.2:8097"

    device = MagicMock()
    device.profile_device.available = True
    device.name = "Living Room Renderer"
    device.volume_level = 0.5
    device.is_volume_muted = False
    device.transport_state = TransportState.PLAYING
    device.current_track_uri = "http://192.168.1.10/stream.mp3"
    device.media_title = "Test Title"
    device.media_artist = "Test Artist"
    device.media_album_name = "Test Album"
    device.media_image_url = "http://192.168.1.10/cover.jpg"
    device.media_duration = 240
    device.media_position = 42
    device.media_position_updated_at = REPORTED_AT
    for name, value in device_state.items():
        setattr(device, name, value)

    player = DLNAPlayer(
        provider,  # type: ignore[arg-type]
        "uuid:dlna-player",
        "http://192.168.1.10/description.xml",
        device=device,
    )
    player._attr_elapsed_time = STALE_POSITION
    player._attr_elapsed_time_last_updated = STALE_REPORTED_AT

    await player.set_dynamic_attributes()
    return player


async def test_zero_position_replaces_the_previous_one() -> None:
    """A device restarting a track reports 0, which must not be read as 'unknown'."""
    player = await _updated_player(media_position=0)

    assert player.elapsed_time == 0.0
    assert player.elapsed_time_last_updated == REPORTED_AT.timestamp()


async def test_known_position_is_applied() -> None:
    """A position reported by the device is adopted together with its timestamp."""
    player = await _updated_player(media_position=42)

    assert player.elapsed_time == 42.0
    assert player.elapsed_time_last_updated == REPORTED_AT.timestamp()


async def test_missing_position_keeps_the_previous_one() -> None:
    """A device that reports no position at all leaves the known position alone."""
    player = await _updated_player(media_position=None)

    assert player.elapsed_time == STALE_POSITION
    assert player.elapsed_time_last_updated == STALE_REPORTED_AT


@pytest.mark.parametrize(("reported", "expected"), [(0.5, 50), (0.0, 0), (1.0, 100)])
async def test_volume_level_is_scaled_to_percent(reported: float, expected: int) -> None:
    """The device reports volume as a 0..1 fraction, the player as a percentage."""
    player = await _updated_player(volume_level=reported)

    assert player.volume_level == expected


async def test_unknown_volume_level_stays_unknown() -> None:
    """A device that does not report its volume must not read as volume 0."""
    player = await _updated_player(volume_level=None)

    assert player.volume_level is None


@pytest.mark.parametrize("reported", [True, False])
async def test_mute_state_is_applied(reported: bool) -> None:
    """A mute state reported by the device is adopted as-is."""
    player = await _updated_player(is_volume_muted=reported)

    assert player.volume_muted is reported


async def test_unknown_mute_state_stays_unknown() -> None:
    """A device that does not report its mute state must not read as unmuted."""
    player = await _updated_player(is_volume_muted=None)

    assert player.volume_muted is None
