"""
Tests for SnapCastPlayer volume/mute exposure vs. the active output protocol.

Regression test for support#6468: native Snapcast volume/mute must not be
offered while a foreign protocol (e.g. Sendspin) is actually driving output.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from music_assistant_models.constants import PLAYER_CONTROL_NATIVE
from music_assistant_models.enums import PlaybackState, PlayerFeature, PlayerType

from music_assistant.constants import CONF_MUTE_CONTROL, CONF_VOLUME_CONTROL
from music_assistant.controllers.players import PlayerController
from music_assistant.providers.snapcast.player import SnapCastPlayer
from tests.common import MockPlayer, MockProvider, create_mock_config


@pytest.fixture
def mock_mass() -> MagicMock:
    """Create a mock MusicAssistant instance, wired like the sibling protocol-state tests."""
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
    """Create a real PlayerController so cross-player lookups resolve normally."""
    ctrl = PlayerController(mock_mass)
    mock_mass.players = ctrl
    return ctrl


@pytest.fixture
def provider(mock_mass: MagicMock) -> MockProvider:
    """Create a mock Snapcast provider, including the attributes SnapCastPlayer needs."""
    prov = MockProvider("snapcast", instance_id="test_snapcast", mass=mock_mass)
    prov.stream_audio_format = MagicMock(sample_rate=48000, bit_depth=16)  # type: ignore[attr-defined]
    return prov


@pytest.fixture
def snapcast_player(provider: MockProvider) -> SnapCastPlayer:
    """Create a SnapCastPlayer with native volume/mute support, as setup() grants it."""
    provider.mass.config.get_base_player_config.return_value = create_mock_config(
        "Test Snapcast Player"
    )
    # group=None keeps _get_active_snapstream() from reaching for snap_provider._snapserver,
    # which MockProvider does not have - playback_state is read eagerly during Player.__init__.
    snap_client = MagicMock(group=None)
    player = SnapCastPlayer(provider, "player_1", snap_client)  # type: ignore[arg-type]
    player._attr_supported_features = {
        PlayerFeature.PLAY_MEDIA,
        PlayerFeature.VOLUME_SET,
        PlayerFeature.VOLUME_MUTE,
    }
    player._cache.clear()
    return player


@pytest.fixture
def sendspin_player(provider: MockProvider) -> MockPlayer:
    """Create a Sendspin protocol player that supports volume, standing in for the real one."""
    player = MockPlayer(provider, "sendspin_1", "Sendspin Bridge", player_type=PlayerType.PROTOCOL)
    player._attr_supported_features = {PlayerFeature.VOLUME_SET, PlayerFeature.VOLUME_MUTE}
    player._attr_playback_state = PlaybackState.PLAYING
    player._cache.clear()
    return player


class TestSnapCastVolumeFeatureExposure:
    """supported_features must hide native volume/mute while a foreign protocol plays."""

    def test_native_volume_exposed_when_idle(self, snapcast_player: SnapCastPlayer) -> None:
        """With no active output protocol, native volume/mute stay available."""
        assert PlayerFeature.VOLUME_SET in snapcast_player.supported_features
        assert PlayerFeature.VOLUME_MUTE in snapcast_player.supported_features

    def test_native_volume_exposed_when_native_is_active(
        self, snapcast_player: SnapCastPlayer
    ) -> None:
        """An explicitly active native output also keeps native volume/mute."""
        snapcast_player.set_active_output_protocol("native")
        assert PlayerFeature.VOLUME_SET in snapcast_player.supported_features
        assert PlayerFeature.VOLUME_MUTE in snapcast_player.supported_features

    def test_native_volume_hidden_when_foreign_protocol_active(
        self,
        snapcast_player: SnapCastPlayer,
        sendspin_player: MockPlayer,
        controller: PlayerController,
    ) -> None:
        """A foreign active protocol (e.g. Sendspin) hides native volume/mute."""
        controller._players = {
            snapcast_player.player_id: snapcast_player,
            sendspin_player.player_id: sendspin_player,
        }
        snapcast_player.set_active_output_protocol(sendspin_player.player_id)

        assert PlayerFeature.VOLUME_SET not in snapcast_player.supported_features
        assert PlayerFeature.VOLUME_MUTE not in snapcast_player.supported_features

    def test_volume_control_resolves_to_active_protocol_not_idle_native(
        self,
        snapcast_player: SnapCastPlayer,
        sendspin_player: MockPlayer,
        controller: PlayerController,
    ) -> None:
        """volume_control/mute_control redirect to Sendspin once it is active."""
        controller._players = {
            snapcast_player.player_id: snapcast_player,
            sendspin_player.player_id: sendspin_player,
        }
        snapcast_player.set_active_output_protocol(sendspin_player.player_id)

        assert snapcast_player.volume_control == sendspin_player.player_id
        assert snapcast_player.mute_control == sendspin_player.player_id
        assert snapcast_player.volume_control != PLAYER_CONTROL_NATIVE

    def test_volume_control_reverts_to_native_when_protocol_cleared(
        self,
        snapcast_player: SnapCastPlayer,
        sendspin_player: MockPlayer,
        controller: PlayerController,
    ) -> None:
        """Once Sendspin stops being active, native volume/mute control returns."""
        controller._players = {
            snapcast_player.player_id: snapcast_player,
            sendspin_player.player_id: sendspin_player,
        }
        snapcast_player.set_active_output_protocol(sendspin_player.player_id)
        assert snapcast_player.volume_control == sendspin_player.player_id

        snapcast_player.set_active_output_protocol(None)

        assert snapcast_player.volume_control == PLAYER_CONTROL_NATIVE
        assert snapcast_player.mute_control == PLAYER_CONTROL_NATIVE
        assert PlayerFeature.VOLUME_SET in snapcast_player.supported_features
        assert PlayerFeature.VOLUME_MUTE in snapcast_player.supported_features

    def test_explicit_native_config_is_overridden_while_foreign_protocol_active(
        self,
        provider: MockProvider,
        snapcast_player: SnapCastPlayer,
        sendspin_player: MockPlayer,
        controller: PlayerController,
    ) -> None:
        """An explicit Native setting is set aside while Sendspin plays - deliberate."""
        provider.mass.config.get_raw_player_config_value = MagicMock(
            side_effect=lambda player_id, key, default=None: (
                PLAYER_CONTROL_NATIVE
                if player_id == snapcast_player.player_id
                and key in (CONF_VOLUME_CONTROL, CONF_MUTE_CONTROL)
                else default
            )
        )
        controller._players = {
            snapcast_player.player_id: snapcast_player,
            sendspin_player.player_id: sendspin_player,
        }
        snapcast_player.set_active_output_protocol(sendspin_player.player_id)

        assert snapcast_player.volume_control == sendspin_player.player_id
        assert snapcast_player.mute_control == sendspin_player.player_id

    def test_volume_control_for_output_still_native_before_protocol_marked_active(
        self,
        snapcast_player: SnapCastPlayer,
        sendspin_player: MockPlayer,
        controller: PlayerController,
    ) -> None:
        """Known gap: volume_control_for_output still returns native before Sendspin is active."""
        controller._players = {
            snapcast_player.player_id: snapcast_player,
            sendspin_player.player_id: sendspin_player,
        }

        assert snapcast_player.active_output_protocol in (None, "native")
        assert (
            snapcast_player.volume_control_for_output(sendspin_player.player_id)
            == PLAYER_CONTROL_NATIVE
        )
