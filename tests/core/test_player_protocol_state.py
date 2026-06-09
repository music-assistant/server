"""Tests for __final_playback_state when an output protocol is active.

Covers the propagation of the protocol player's state (including IDLE) up to
the parent player that has it set as ``active_output_protocol``.
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest
from music_assistant_models.enums import PlaybackState, PlayerType

from music_assistant.controllers.players import PlayerController
from tests.common import MockPlayer, MockProvider


@pytest.fixture
def mock_mass() -> MagicMock:
    """Create a mock MusicAssistant instance."""
    mass = MagicMock()
    mass.closing = False
    mass.loop = None
    mass.config = MagicMock()
    mass.config.get = MagicMock(return_value=[])
    mass.config.get_raw_player_config_value = MagicMock(return_value="auto")
    mass.config.get_raw_core_config_value = MagicMock(return_value="GLOBAL")
    mass.config.set = MagicMock()
    mass.signal_event = MagicMock()
    mass.get_providers = MagicMock(return_value=[])
    mass.player_queues = MagicMock()
    mass.player_queues.get = MagicMock(return_value=None)
    return mass


@pytest.fixture
def controller(mock_mass: MagicMock) -> PlayerController:
    """Create a PlayerController instance."""
    ctrl = PlayerController(mock_mass)
    mock_mass.players = ctrl
    return ctrl


@pytest.fixture
def provider(mock_mass: MagicMock) -> MockProvider:
    """Create a mock provider."""
    return MockProvider("test_provider", instance_id="test_prov", mass=mock_mass)


class TestFinalPlaybackStateWithActiveProtocol:
    """When an active output protocol is set the protocol is the source of truth."""

    def test_playing_protocol_propagates_playing(
        self,
        provider: MockProvider,
        controller: PlayerController,
    ) -> None:
        """A PLAYING protocol player makes the parent report PLAYING."""
        protocol_player = MockPlayer(provider, "ap_1", "AirPlay", player_type=PlayerType.PROTOCOL)
        protocol_player._attr_playback_state = PlaybackState.PLAYING
        protocol_player._attr_elapsed_time = 20.0
        protocol_player._attr_elapsed_time_last_updated = time.time()

        player = MockPlayer(provider, "player_1", "Test Player")
        player._attr_playback_state = PlaybackState.IDLE  # native is idle
        player.set_active_output_protocol("ap_1")

        controller._players = {"player_1": player, "ap_1": protocol_player}

        protocol_player.update_state(signal_event=False)
        player.update_state(signal_event=False)

        assert player.state.playback_state == PlaybackState.PLAYING

    def test_idle_protocol_propagates_idle(
        self,
        provider: MockProvider,
        controller: PlayerController,
    ) -> None:
        """An IDLE protocol player makes the parent report IDLE (regression guard).

        Previously the parent would fall through to the parent/group state when
        the protocol was IDLE, which could create a circular state inheritance
        in sync groups and leave the group stuck in PLAYING forever. The
        protocol player is now the source of truth regardless of its state.
        """
        protocol_player = MockPlayer(provider, "ap_1", "AirPlay", player_type=PlayerType.PROTOCOL)
        protocol_player._attr_playback_state = PlaybackState.IDLE
        protocol_player._attr_elapsed_time = 0.0
        protocol_player._attr_elapsed_time_last_updated = time.time()

        player = MockPlayer(provider, "player_1", "Test Player")
        # Native player still reports PLAYING (e.g. a Sonos that's outputting
        # someone else's AirPlay audio) — but the active output protocol is
        # the live audio source and it has gone IDLE, which is what matters.
        player._attr_playback_state = PlaybackState.PLAYING
        player.set_active_output_protocol("ap_1")

        controller._players = {"player_1": player, "ap_1": protocol_player}

        protocol_player.update_state(signal_event=False)
        player.update_state(signal_event=False)

        assert player.state.playback_state == PlaybackState.IDLE

    def test_no_active_protocol_uses_native_state(
        self,
        provider: MockProvider,
        controller: PlayerController,
    ) -> None:
        """Without an active output protocol, fall through to the native state."""
        player = MockPlayer(provider, "player_1", "Test Player")
        player._attr_playback_state = PlaybackState.PLAYING
        player._attr_elapsed_time = 5.0
        player._attr_elapsed_time_last_updated = time.time()

        controller._players = {"player_1": player}

        player.update_state(signal_event=False)

        assert player.state.playback_state == PlaybackState.PLAYING
