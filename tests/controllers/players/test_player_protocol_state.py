"""
Tests for __final_playback_state when an output protocol is active.

Covers the propagation of the protocol player's state (including IDLE) up to
the parent player that has it set as ``active_output_protocol``.
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest
from music_assistant_models.enums import MediaType, PlaybackState, PlayerType

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
        # refresh: the protocol player is cross-player state and the production
        # fan-out (which marks the parent dirty) is suppressed in this test
        player.refresh_state(signal_event=False)

        assert player.state.playback_state == PlaybackState.PLAYING

    def test_idle_protocol_propagates_idle(
        self,
        provider: MockProvider,
        controller: PlayerController,
    ) -> None:
        """
        An IDLE protocol player makes the parent report IDLE (regression guard).

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
        player.refresh_state(signal_event=False)

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

    def test_negative_elapsed_time_is_clamped(
        self,
        provider: MockProvider,
        controller: PlayerController,
    ) -> None:
        """A negative provider elapsed time is clamped at the elapsed_time accessor."""
        player = MockPlayer(provider, "player_1", "Test Player")
        player._attr_playback_state = PlaybackState.PAUSED
        player._attr_elapsed_time = -1.0
        player._attr_elapsed_time_last_updated = time.time()

        controller._players = {"player_1": player}

        player.update_state(signal_event=False)

        assert player.elapsed_time == 0
        assert player.corrected_elapsed_time == 0
        assert player.state.elapsed_time == 0


def _audio_source_queue(elapsed: float, updated: float) -> MagicMock:
    """Build a mock queue whose current item is an AudioSource carrying a source clock."""
    stream_metadata = MagicMock()
    stream_metadata.elapsed_time = elapsed
    stream_metadata.elapsed_time_last_updated = updated
    streamdetails = MagicMock()
    streamdetails.media_type = MediaType.AUDIO_SOURCE
    streamdetails.stream_metadata = stream_metadata
    current_item = MagicMock()
    current_item.streamdetails = streamdetails
    queue = MagicMock()
    queue.current_item = current_item
    return queue


def _empty_queue() -> MagicMock:
    """Build a mock queue with no current item (no AudioSource source clock)."""
    queue = MagicMock()
    queue.current_item = None
    return queue


class TestFinalPlaybackStateAudioSourceElapsed:
    """The AudioSource source-clock override, including the group own-queue fallback."""

    def test_standalone_uses_active_source_audio_source_elapsed(
        self,
        provider: MockProvider,
        controller: PlayerController,
        mock_mass: MagicMock,
    ) -> None:
        """A standalone player reports the AudioSource source clock from its active queue."""
        player = MockPlayer(provider, "player_1", "Player")
        player._attr_playback_state = PlaybackState.PLAYING
        player._attr_active_source = "player_1"
        player._attr_elapsed_time = 0.0
        player._attr_elapsed_time_last_updated = time.time()

        own_queue = _audio_source_queue(42.0, time.time())
        mock_mass.player_queues.get = MagicMock(
            side_effect=lambda qid: {"player_1": own_queue}.get(qid)
        )
        controller._players = {"player_1": player}

        player.update_state(signal_event=False)

        assert player.state.elapsed_time == 42.0

    def test_group_falls_back_to_own_queue_audio_source_elapsed(
        self,
        provider: MockProvider,
        controller: PlayerController,
        mock_mass: MagicMock,
    ) -> None:
        """
        A group reports the AudioSource source clock from its own queue.

        The group's active source resolves to the sync leader, whose queue does not
        carry the AudioSource, so without the own-queue fallback the group would report
        the leader's byte clock instead of the source's logical position.
        """
        group = MockPlayer(provider, "group_1", "Group", player_type=PlayerType.GROUP)
        group._attr_playback_state = PlaybackState.PLAYING
        group._attr_active_source = "leader_1"
        # stale leader byte clock that gets copied into the group
        group._attr_elapsed_time = 999.0
        group._attr_elapsed_time_last_updated = time.time()

        leader_queue = _empty_queue()
        own_queue = _audio_source_queue(42.0, time.time())
        mock_mass.player_queues.get = MagicMock(
            side_effect=lambda qid: {"leader_1": leader_queue, "group_1": own_queue}.get(qid)
        )
        controller._players = {"group_1": group}

        group.update_state(signal_event=False)

        assert group.state.elapsed_time == 42.0

    def test_non_group_does_not_use_own_queue_fallback(
        self,
        provider: MockProvider,
        controller: PlayerController,
        mock_mass: MagicMock,
    ) -> None:
        """A non-group player ignores an AudioSource sitting on its own (inactive) queue."""
        player = MockPlayer(provider, "player_1", "Player")
        player._attr_playback_state = PlaybackState.PLAYING
        player._attr_active_source = "other_src"
        player._attr_elapsed_time = 5.0
        player._attr_elapsed_time_last_updated = time.time()

        other_queue = _empty_queue()
        own_queue = _audio_source_queue(42.0, time.time())
        mock_mass.player_queues.get = MagicMock(
            side_effect=lambda qid: {"other_src": other_queue, "player_1": own_queue}.get(qid)
        )
        controller._players = {"player_1": player}

        player.update_state(signal_event=False)

        assert player.state.elapsed_time == 5.0
