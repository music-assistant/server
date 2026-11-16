"""
Tests for SnapCastPlayer volume/mute exposure vs. the active output protocol.

Regression test for support#6468: native Snapcast volume/mute must not be
offered while a foreign protocol (e.g. Sendspin) is actually driving output.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from music_assistant_models.config_entries import PlayerConfig
from music_assistant_models.constants import PLAYER_CONTROL_NATIVE
from music_assistant_models.enums import PlayerFeature, PlayerType, ProtocolRole

from music_assistant.constants import CONF_MUTE_CONTROL, CONF_VOLUME_CONTROL
from music_assistant.models.player import Player
from music_assistant.providers.snapcast.player import SnapCastPlayer
from tests.common import MockProtocol, MockProvider, attach_mock, mock_player_mass


@pytest.fixture
def mock_mass() -> MagicMock:
    """Create a mocked MusicAssistant whose players controller looks players up for real."""
    return mock_player_mass()


@pytest.fixture
def snapcast_player(mock_mass: MagicMock) -> SnapCastPlayer:
    """Create a SnapCastPlayer with native volume/mute support, attached to a player."""
    provider = MockProvider("snapcast", instance_id="test_snapcast", mass=mock_mass)
    provider.stream_audio_format = MagicMock(sample_rate=48000, bit_depth=16)  # type: ignore[attr-defined]
    mock_mass.config.get_base_protocol_config.return_value = PlayerConfig(
        values={}, provider=provider.instance_id, player_id="ma_kitchen"
    )
    # group=None keeps _get_active_snapstream() from reaching for snap_provider._snapserver
    snap_client = MagicMock(group=None)
    protocol = SnapCastPlayer(provider, "ma_kitchen", snap_client)  # type: ignore[arg-type]
    protocol._attr_supported_features = {
        PlayerFeature.PLAY_MEDIA,
        PlayerFeature.VOLUME_SET,
        PlayerFeature.VOLUME_MUTE,
    }
    player = Player(
        mock_mass,
        "pl_kitchen",
        PlayerConfig(
            values={},
            provider=provider.instance_id,
            player_id="pl_kitchen",
            default_name="Kitchen",
            player_type=PlayerType.PLAYER,
        ),
    )
    mock_mass.players._players[player.player_id] = player
    mock_mass.players._protocols[protocol.protocol_id] = protocol
    player.add_protocol(protocol, ProtocolRole.NATIVE)
    protocol.set_player(player)
    player.set_initialized()
    return protocol


@pytest.fixture
def sendspin_player(mock_mass: MagicMock, snapcast_player: SnapCastPlayer) -> MockProtocol:
    """Attach a Sendspin audio protocol that supports volume to the player of the snapclient."""
    provider = MockProvider("sendspin", instance_id="sendspin", mass=mock_mass)
    _, protocol = attach_mock(
        provider,
        "sendspin_1",
        "Sendspin Bridge",
        role=ProtocolRole.AUDIO,
        player=snapcast_player.player,
    )
    protocol._attr_supported_features = {PlayerFeature.VOLUME_SET, PlayerFeature.VOLUME_MUTE}
    return protocol


def _player(protocol: SnapCastPlayer) -> Player:
    """Return the player a protocol is attached to."""
    assert protocol.player is not None
    return protocol.player


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
        _player(snapcast_player).set_active_output_protocol("native")
        assert PlayerFeature.VOLUME_SET in snapcast_player.supported_features
        assert PlayerFeature.VOLUME_MUTE in snapcast_player.supported_features

    def test_native_volume_hidden_when_foreign_protocol_active(
        self, snapcast_player: SnapCastPlayer, sendspin_player: MockProtocol
    ) -> None:
        """A foreign active protocol (e.g. Sendspin) hides native volume/mute."""
        _player(snapcast_player).set_active_output_protocol(sendspin_player.protocol_id)

        assert PlayerFeature.VOLUME_SET not in snapcast_player.supported_features
        assert PlayerFeature.VOLUME_MUTE not in snapcast_player.supported_features

    def test_volume_control_resolves_to_active_protocol_not_idle_native(
        self, snapcast_player: SnapCastPlayer, sendspin_player: MockProtocol
    ) -> None:
        """volume_control/mute_control redirect to Sendspin once it is active."""
        player = _player(snapcast_player)
        player.set_active_output_protocol(sendspin_player.protocol_id)

        assert player._volume_control == sendspin_player.protocol_id
        assert player._mute_control == sendspin_player.protocol_id

    def test_volume_control_reverts_to_native_when_protocol_cleared(
        self, snapcast_player: SnapCastPlayer, sendspin_player: MockProtocol
    ) -> None:
        """Once Sendspin stops being active, native volume/mute control returns."""
        player = _player(snapcast_player)
        player.set_active_output_protocol(sendspin_player.protocol_id)
        assert player._volume_control == sendspin_player.protocol_id

        player.set_active_output_protocol(None)

        assert player._volume_control == PLAYER_CONTROL_NATIVE
        assert player._mute_control == PLAYER_CONTROL_NATIVE
        assert PlayerFeature.VOLUME_SET in snapcast_player.supported_features
        assert PlayerFeature.VOLUME_MUTE in snapcast_player.supported_features

    def test_explicit_native_config_is_overridden_while_foreign_protocol_active(
        self, mock_mass: MagicMock, snapcast_player: SnapCastPlayer, sendspin_player: MockProtocol
    ) -> None:
        """An explicit Native setting is set aside while Sendspin plays - deliberate."""
        player = _player(snapcast_player)
        mock_mass.config.get_player_value = MagicMock(
            side_effect=lambda player_id, key, default=None: (
                PLAYER_CONTROL_NATIVE
                if player_id == player.player_id and key in (CONF_VOLUME_CONTROL, CONF_MUTE_CONTROL)
                else default
            )
        )
        player.set_active_output_protocol(sendspin_player.protocol_id)

        assert player._volume_control == sendspin_player.protocol_id
        assert player._mute_control == sendspin_player.protocol_id

    def test_volume_control_for_output_is_that_output_before_it_is_marked_active(
        self, snapcast_player: SnapCastPlayer, sendspin_player: MockProtocol
    ) -> None:
        """The volume of an output that is about to play is owned by it, not by the snapclient."""
        player = _player(snapcast_player)

        assert player.active_output_protocol in (None, "native")
        assert (
            player.volume_control_for_output(sendspin_player.protocol_id)
            == sendspin_player.protocol_id
        )
