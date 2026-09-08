"""
Tests for __final_playback_state when an output protocol is active.

Covers the propagation of the protocol player's state (including IDLE) up to
the parent player that has it set as ``active_output_protocol``.
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest
from music_assistant_models.audio_processing import ActiveSourceAudioDetails
from music_assistant_models.enums import ContentType, PlaybackState, PlayerType
from music_assistant_models.media_items import AudioFormat

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
    mass.config.get_raw_player_config_value = MagicMock(
        side_effect=lambda _player_id, _key, default=None: default
    )
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

    def test_protocol_player_inherits_parent_source_audio(
        self,
        provider: MockProvider,
        controller: PlayerController,
    ) -> None:
        """A protocol player publishes the live source audio details of its parent."""
        details = ActiveSourceAudioDetails(
            input_format=AudioFormat(
                content_type=ContentType.FLAC,
                sample_rate=44100,
                bit_depth=16,
                channels=2,
            )
        )
        parent = MockPlayer(provider, "player_1", "Player")
        parent._state.active_source_audio = details
        protocol_player = MockPlayer(provider, "ap_1", "AirPlay", player_type=PlayerType.PROTOCOL)
        controller._players = {"player_1": parent, "ap_1": protocol_player}
        protocol_player.set_protocol_parent_id("player_1")

        protocol_player.refresh_state(signal_event=False)

        assert protocol_player.state.active_source_audio == details

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


def _source_session(elapsed: float | None, updated: float | None, player_id: str) -> MagicMock:
    """Build a mock session whose source reports the given clock."""
    stream_metadata = MagicMock()
    stream_metadata.elapsed_time = elapsed
    stream_metadata.elapsed_time_last_updated = updated
    session = MagicMock()
    session.player_id = player_id
    session.stream_metadata = stream_metadata
    session.source_uri = f"test_instance://audio_source/{player_id}"
    session.source.image = None
    return session


class TestFinalPlaybackStateAudioSourceElapsed:
    """The source-clock override for a live external source playing on a player."""

    def test_standalone_uses_its_sources_clock(
        self,
        provider: MockProvider,
        controller: PlayerController,
        mock_mass: MagicMock,
    ) -> None:
        """A standalone player reports the position its live source reports."""
        player = MockPlayer(provider, "player_1", "Player")
        player._attr_playback_state = PlaybackState.PLAYING
        player._attr_active_source = "player_1"
        player._attr_elapsed_time = 0.0
        player._attr_elapsed_time_last_updated = time.time()

        session = _source_session(42.0, time.time(), "player_1")
        mock_mass.players.get_audio_source_session = MagicMock(
            side_effect=lambda pid: {"player_1": session}.get(pid)
        )
        controller._players = {"player_1": player}

        player.update_state(signal_event=False)

        assert player.state.elapsed_time == 42.0

    def test_group_uses_its_sources_clock_not_the_leaders(
        self,
        provider: MockProvider,
        controller: PlayerController,
        mock_mass: MagicMock,
    ) -> None:
        """
        A group reports its own source's position, not the clock copied from its leader.

        The session belongs to the group that owns the source, so it is found directly
        on the group and the leader never has to be consulted.
        """
        group = MockPlayer(provider, "group_1", "Group", player_type=PlayerType.GROUP)
        group._attr_playback_state = PlaybackState.PLAYING
        group._attr_active_source = "leader_1"
        # stale leader byte clock that gets copied into the group
        group._attr_elapsed_time = 999.0
        group._attr_elapsed_time_last_updated = time.time()

        session = _source_session(42.0, time.time(), "group_1")
        mock_mass.players.get_audio_source_session = MagicMock(
            side_effect=lambda pid: {"group_1": session}.get(pid)
        )
        controller._players = {"group_1": group}

        group.update_state(signal_event=False)

        assert group.state.elapsed_time == 42.0

    def test_a_player_without_a_source_keeps_its_own_clock(
        self,
        provider: MockProvider,
        controller: PlayerController,
        mock_mass: MagicMock,
    ) -> None:
        """A player with no live source of its own is not given another player's position."""
        player = MockPlayer(provider, "player_1", "Player")
        player._attr_playback_state = PlaybackState.PLAYING
        player._attr_active_source = "other_src"
        player._attr_elapsed_time = 5.0
        player._attr_elapsed_time_last_updated = time.time()

        elsewhere = _source_session(42.0, time.time(), "other_player")
        mock_mass.players.get_audio_source_session = MagicMock(
            side_effect=lambda pid: {"other_player": elsewhere}.get(pid)
        )
        controller._players = {"player_1": player}

        player.update_state(signal_event=False)

        assert player.state.elapsed_time == 5.0
