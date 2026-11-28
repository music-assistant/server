"""
The playback state a player publishes while an announcement holds its queue.

The default announcement stops the device for the clip. A queue that is held for it plays on after
the clip, so its player does not leave playing for the stops of the device, while what the device
itself reports stays available for everything that waits on the device.
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from music_assistant_models.enums import PlaybackState

from music_assistant.constants import ATTR_ANNOUNCEMENT_HELD_QUEUE, ATTR_DEVICE_PLAYBACK_STATE
from music_assistant.controllers.players import PlayerController
from music_assistant.models.player import Player
from tests.common import MockProtocol, MockProvider, attach_mock, mock_player_mass


@pytest.fixture
def mock_mass() -> MagicMock:
    """Create a mock MusicAssistant instance."""
    return mock_player_mass()


@pytest.fixture
def protocol(mock_mass: MagicMock) -> MockProtocol:
    """Create a player that plays."""
    provider = MockProvider("test_provider", mass=mock_mass)
    player, protocol = attach_mock(provider, "protocol_1", "Player 1", player_id="player_1")
    protocol._attr_playback_state = PlaybackState.PLAYING
    protocol.set_current_media(uri="track-a", title="Shout")
    player.update_state()
    return protocol


@pytest.fixture
def player(protocol: MockProtocol) -> Player:
    """Return the player the protocol fixture is attached to."""
    assert protocol.player is not None
    return protocol.player


def _published(player: Player) -> PlaybackState:
    """Return the playback state the player publishes."""
    return player.playback_state


def _device_state(player: Player) -> PlaybackState:
    """Return the playback state the device of the player reports."""
    return player.device_playback_state


def _hold(player: Player) -> None:
    """Hold the queue of the player for an announcement, as the announcement does."""
    player.extra_data[ATTR_ANNOUNCEMENT_HELD_QUEUE] = player.player_id
    player.update_state(force_update=True)


def _release(player: Player) -> None:
    """Release the queue of the player after an announcement, as the announcement does."""
    player.extra_data.pop(ATTR_ANNOUNCEMENT_HELD_QUEUE, None)
    player.update_state(force_update=True)


def _device(protocol: MockProtocol, state: PlaybackState) -> None:
    """Let the device report a playback state."""
    protocol._attr_playback_state = state
    assert protocol.player is not None
    protocol.player.update_state()


def test_a_held_player_stays_playing_when_the_device_stops(
    player: Player, protocol: MockProtocol
) -> None:
    """Test the stop an announcement needs does not show as a stop of the player."""
    _hold(player)

    _device(protocol, PlaybackState.IDLE)

    assert _published(player) is PlaybackState.PLAYING
    assert player.state.playback_state is PlaybackState.PLAYING
    assert _device_state(player) is PlaybackState.IDLE


def test_a_held_player_that_goes_away_is_not_published_as_playing(
    player: Player, protocol: MockProtocol
) -> None:
    """Test a device that is gone during the announcement does not keep the player playing."""
    _hold(player)
    _device(protocol, PlaybackState.IDLE)
    assert _published(player) is PlaybackState.PLAYING

    protocol._attr_available = False
    player.update_state()

    assert _published(player) is not PlaybackState.PLAYING


def test_a_player_whose_device_stopped_before_the_hold_is_published_as_playing(
    player: Player, protocol: MockProtocol
) -> None:
    """Test the hold also applies when the device reported the stop first."""
    _device(protocol, PlaybackState.IDLE)
    assert _published(player) is PlaybackState.IDLE

    _hold(player)

    assert _published(player) is PlaybackState.PLAYING
    assert _device_state(player) is PlaybackState.IDLE


def test_the_player_follows_the_device_again_after_the_release(
    player: Player, protocol: MockProtocol
) -> None:
    """Test a device that stays stopped is published as stopped once the queue is released."""
    _hold(player)
    _device(protocol, PlaybackState.IDLE)

    _release(player)

    assert _published(player) is PlaybackState.IDLE
    assert _device_state(player) is PlaybackState.IDLE


def test_a_device_that_plays_again_keeps_the_player_playing_through_the_release(
    player: Player, protocol: MockProtocol
) -> None:
    """Test the restored playback is not published as a stop and a start."""
    _hold(player)
    _device(protocol, PlaybackState.IDLE)
    _device(protocol, PlaybackState.PLAYING)

    _release(player)

    assert _published(player) is PlaybackState.PLAYING
    assert _device_state(player) is PlaybackState.PLAYING


def test_a_player_that_is_not_held_follows_its_device(
    player: Player, protocol: MockProtocol
) -> None:
    """Test nothing changes for a player without an announcement."""
    _device(protocol, PlaybackState.IDLE)
    assert _published(player) is PlaybackState.IDLE
    assert _device_state(player) is PlaybackState.IDLE

    _device(protocol, PlaybackState.PLAYING)
    assert _published(player) is PlaybackState.PLAYING
    assert _device_state(player) is PlaybackState.PLAYING


def test_a_paused_device_of_a_held_player_is_published_as_playing(
    player: Player, protocol: MockProtocol
) -> None:
    """Test every state but playing is covered by the hold, not only a stop."""
    _hold(player)

    _device(protocol, PlaybackState.PAUSED)

    assert _published(player) is PlaybackState.PLAYING
    assert _device_state(player) is PlaybackState.PAUSED


def test_the_stop_of_a_held_device_still_counts_as_the_player_going_idle(
    player: Player, protocol: MockProtocol
) -> None:
    """Test the bookkeeping of going idle follows the device, not the published state."""
    _hold(player)
    _device(protocol, PlaybackState.IDLE)
    assert player._stop_called is True

    _device(protocol, PlaybackState.PLAYING)

    assert player._stop_called is False


def test_a_change_of_the_device_state_is_a_change_of_the_player(
    player: Player, protocol: MockProtocol
) -> None:
    """Test whoever waits for the device is told, although the published state did not change."""
    _hold(player)
    before = player._state_fingerprint

    _device(protocol, PlaybackState.IDLE)

    assert before is not None
    assert before[ATTR_DEVICE_PLAYBACK_STATE] is PlaybackState.PLAYING
    assert player._state_fingerprint is not None
    assert player._state_fingerprint[ATTR_DEVICE_PLAYBACK_STATE] is PlaybackState.IDLE
    assert player._state_fingerprint["playback_state"] is PlaybackState.PLAYING


def _controller_for(player: Player) -> Any:
    """Return what the wait of the players controller needs of it, around the player."""
    controller = SimpleNamespace(logger=MagicMock())
    controller.get_player = MagicMock(return_value=player)
    controller.subscribe_player_state_update = MagicMock(return_value=MagicMock())
    controller.wait_for_player_update = lambda *args, **kwargs: (
        PlayerController.wait_for_player_update(controller, *args, **kwargs)  # type: ignore[arg-type]
    )
    return controller


async def test_the_wait_for_a_stop_follows_the_device_of_a_held_player(
    player: Player, protocol: MockProtocol
) -> None:
    """Test the announcement does not wait for a stop that the hold keeps from being published."""
    _hold(player)
    _device(protocol, PlaybackState.IDLE)
    controller = _controller_for(player)

    started = time.monotonic()
    await PlayerController._wait_for_playback_state(
        controller, player, PlaybackState.IDLE, timeout=5
    )

    assert time.monotonic() - started < 1


async def test_the_wait_for_a_start_follows_the_device_of_a_held_player(
    player: Player, protocol: MockProtocol
) -> None:
    """Test a published playing state does not stand in for a device that has not started."""
    _hold(player)
    _device(protocol, PlaybackState.IDLE)
    controller = _controller_for(player)

    with pytest.raises(asyncio.TimeoutError):
        async with asyncio.timeout(0.5):
            await PlayerController._wait_for_playback_state(
                controller, player, PlaybackState.PLAYING, timeout=5
            )
