"""
Tests for the published playback state when an output protocol is active.

Covers the propagation of the state an audio protocol reports (including IDLE)
to the player that has it set as ``active_output_protocol``.
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest
from music_assistant_models.enums import PlaybackState, PlayerType, ProtocolRole

from music_assistant.controllers.players import PlayerController
from tests.common import MockProvider, attach_mock


@pytest.fixture
def mock_mass() -> MagicMock:
    """Create a mock MusicAssistant instance."""
    mass = MagicMock()
    mass.closing = False
    mass.loop = None
    mass.config = MagicMock()
    mass.config.get = MagicMock(return_value=[])
    mass.config.get_player_value = MagicMock(
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
        """A PLAYING audio protocol makes the player report PLAYING."""
        player, native = attach_mock(provider, "native_1", "Test Player")
        native._attr_playback_state = PlaybackState.IDLE  # native is idle
        _, airplay = attach_mock(
            provider, "ap_1", "AirPlay", role=ProtocolRole.AUDIO, player=player
        )
        airplay._attr_playback_state = PlaybackState.PLAYING
        airplay._attr_elapsed_time = 20.0
        airplay._attr_elapsed_time_last_updated = time.time()
        player.set_active_output_protocol("ap_1")

        player.refresh_state(signal_event=False)

        assert controller.get_player(player.player_id) is player
        assert player.playback_state == PlaybackState.PLAYING

    def test_idle_protocol_propagates_idle(
        self,
        provider: MockProvider,
        controller: PlayerController,
    ) -> None:
        """
        An IDLE audio protocol makes the player report IDLE (regression guard).

        Previously the player would fall through to the parent/group state when
        the protocol was IDLE, which could create a circular state inheritance
        in sync groups and leave the group stuck in PLAYING forever. The
        active protocol is now the source of truth regardless of its state.
        """
        player, native = attach_mock(provider, "native_1", "Test Player")
        # Native protocol still reports PLAYING (e.g. a Sonos that's outputting
        # someone else's AirPlay audio) — but the active output protocol is
        # the live audio source and it has gone IDLE, which is what matters.
        native._attr_playback_state = PlaybackState.PLAYING
        _, airplay = attach_mock(
            provider, "ap_1", "AirPlay", role=ProtocolRole.AUDIO, player=player
        )
        airplay._attr_playback_state = PlaybackState.IDLE
        airplay._attr_elapsed_time = 0.0
        airplay._attr_elapsed_time_last_updated = time.time()
        player.set_active_output_protocol("ap_1")

        player.refresh_state(signal_event=False)

        assert controller.get_player(player.player_id) is player
        assert player.playback_state == PlaybackState.IDLE

    def test_no_active_protocol_uses_native_state(
        self,
        provider: MockProvider,
        controller: PlayerController,
    ) -> None:
        """Without an active output protocol, fall through to the native state."""
        player, native = attach_mock(provider, "native_1", "Test Player")
        native._attr_playback_state = PlaybackState.PLAYING
        native._attr_elapsed_time = 5.0
        native._attr_elapsed_time_last_updated = time.time()

        player.update_state(signal_event=False)

        assert controller.get_player(player.player_id) is player
        assert player.playback_state == PlaybackState.PLAYING

    def test_negative_elapsed_time_is_clamped(
        self,
        provider: MockProvider,
        controller: PlayerController,
    ) -> None:
        """A negative provider elapsed time is clamped at the elapsed_time accessor."""
        player, native = attach_mock(provider, "native_1", "Test Player")
        native._attr_playback_state = PlaybackState.PAUSED
        native._attr_elapsed_time = -1.0
        native._attr_elapsed_time_last_updated = time.time()

        player.update_state(signal_event=False)

        assert native.elapsed_time == 0
        assert native.corrected_elapsed_time == 0
        assert player.elapsed_time == 0


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
        player, native = attach_mock(provider, "native_1", "Player")
        native._attr_playback_state = PlaybackState.PLAYING
        native._attr_active_source = player.player_id
        native._attr_elapsed_time = 0.0
        native._attr_elapsed_time_last_updated = time.time()

        session = _source_session(42.0, time.time(), player.player_id)
        mock_mass.players.get_audio_source_session = MagicMock(
            side_effect=lambda pid: {player.player_id: session}.get(pid)
        )

        player.update_state(signal_event=False)

        assert player.elapsed_time == 42.0

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
        group, group_protocol = attach_mock(
            provider,
            "group_1",
            "Group",
            role=ProtocolRole.VIRTUAL,
            player_type=PlayerType.GROUP,
        )
        group_protocol._attr_playback_state = PlaybackState.PLAYING
        group_protocol._attr_active_source = "leader_1"
        # stale leader byte clock that gets copied into the group
        group_protocol._attr_elapsed_time = 999.0
        group_protocol._attr_elapsed_time_last_updated = time.time()

        session = _source_session(42.0, time.time(), group.player_id)
        mock_mass.players.get_audio_source_session = MagicMock(
            side_effect=lambda pid: {group.player_id: session}.get(pid)
        )

        group.update_state(signal_event=False)

        assert group.elapsed_time == 42.0

    def test_a_player_without_a_source_keeps_its_own_clock(
        self,
        provider: MockProvider,
        controller: PlayerController,
        mock_mass: MagicMock,
    ) -> None:
        """A player with no live source of its own is not given another player's position."""
        player, native = attach_mock(provider, "native_1", "Player")
        native._attr_playback_state = PlaybackState.PLAYING
        native._attr_active_source = "other_src"
        native._attr_elapsed_time = 5.0
        native._attr_elapsed_time_last_updated = time.time()

        elsewhere = _source_session(42.0, time.time(), "other_player")
        mock_mass.players.get_audio_source_session = MagicMock(
            side_effect=lambda pid: {"other_player": elsewhere}.get(pid)
        )

        player.update_state(signal_event=False)

        assert player.elapsed_time == 5.0
