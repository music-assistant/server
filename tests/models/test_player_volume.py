"""
Tests for Player model final volume/mute state resolution.

Covers the published volume level and mute state, which are read
exclusively from whichever control volume_control/mute_control
resolves to (native, one of the player's protocols, or an external player control). The
final state is None (unknown) whenever that resolved control itself reports
no value.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from music_assistant_models.enums import PlayerFeature, ProtocolRole

from music_assistant.constants import CONF_MUTE_CONTROL, CONF_VOLUME_CONTROL
from music_assistant.models.player import Player
from tests.common import MockProvider, attach_mock, mock_player_mass


@pytest.fixture
def mock_mass() -> MagicMock:
    """Create a mock MusicAssistant instance."""
    mass = mock_player_mass()
    mass.loop = None
    mass.config.get_raw_core_config_value = MagicMock(return_value="GLOBAL")
    return mass


def _attach_control(mock_mass: MagicMock, player: Player, **reported: int | bool | None) -> None:
    """
    Attach the audio protocol that the stored volume or mute control of the tests names.

    :param mock_mass: The mocked MusicAssistant instance.
    :param player: The player to attach it to.
    :param reported: What the protocol reports: ``volume_level`` or ``volume_muted``.
    """
    provider = MockProvider("control", mass=mock_mass)
    _, control = attach_mock(provider, "control_player", role=ProtocolRole.AUDIO, player=player)
    control._attr_supported_features = {PlayerFeature.VOLUME_SET, PlayerFeature.VOLUME_MUTE}
    for key, value in reported.items():
        setattr(control, f"_attr_{key}", value)


class TestFinalVolumeLevel:
    """Test volume level resolution against the control volume_control resolves to."""

    def test_uses_control_protocol_when_available(self, mock_mass: MagicMock) -> None:
        """Final volume uses the control protocol's volume when it is present."""
        mock_mass.config.get_player_value = MagicMock(
            side_effect=lambda _pid, key, *_a: (
                "control_player" if key == CONF_VOLUME_CONTROL else None
            )
        )
        provider = MockProvider("test", mass=mock_mass)
        player, protocol = attach_mock(provider, "main_player", "Main")
        protocol._attr_volume_level = 30

        _attach_control(mock_mass, player, volume_level=80)

        player.update_state(signal_event=False)
        assert player.volume_level == 80

    def test_falls_back_to_native_when_control_protocol_missing(self, mock_mass: MagicMock) -> None:
        """Final volume falls back to native when the control protocol doesn't exist."""
        mock_mass.config.get_player_value = MagicMock(
            side_effect=lambda _pid, key, *_a: (
                "missing_player" if key == CONF_VOLUME_CONTROL else None
            )
        )
        provider = MockProvider("test", mass=mock_mass)
        player, protocol = attach_mock(provider, "main_player", "Main")
        protocol._attr_volume_level = 55

        player.update_state(signal_event=False)
        assert player.volume_level == 55

    def test_reports_unknown_when_control_has_no_volume(self, mock_mass: MagicMock) -> None:
        """Final volume is unknown when the resolved control's volume is None."""
        mock_mass.config.get_player_value = MagicMock(
            side_effect=lambda _pid, key, *_a: (
                "control_player" if key == CONF_VOLUME_CONTROL else None
            )
        )
        provider = MockProvider("test", mass=mock_mass)
        player, protocol = attach_mock(provider, "main_player", "Main")
        protocol._attr_volume_level = 42

        _attach_control(mock_mass, player, volume_level=None)

        player.update_state(signal_event=False)
        assert player.volume_level is None

    def test_uses_zero_from_the_control_protocol(self, mock_mass: MagicMock) -> None:
        """Volume 0 from the protocol that is the control is trusted as genuine."""
        mock_mass.config.get_player_value = MagicMock(
            side_effect=lambda _pid, key, *_a: (
                "control_player" if key == CONF_VOLUME_CONTROL else None
            )
        )
        provider = MockProvider("test", mass=mock_mass)
        player, protocol = attach_mock(provider, "main_player", "Main")
        protocol._attr_volume_level = 55

        _attach_control(mock_mass, player, volume_level=0)

        player.update_state(signal_event=False)
        assert player.volume_level == 0


class TestFinalVolumeMutedState:
    """Test mute state resolution against the control mute_control resolves to."""

    def test_uses_control_protocol_when_available(self, mock_mass: MagicMock) -> None:
        """Final mute state uses the control protocol's mute state when present."""
        mock_mass.config.get_player_value = MagicMock(
            side_effect=lambda _pid, key, default=None: (
                "control_player"
                if key == CONF_MUTE_CONTROL
                else 0
                if key == "min_volume"
                else 100
                if key == "max_volume"
                else default
            ),
        )
        provider = MockProvider("test", mass=mock_mass)
        player, protocol = attach_mock(provider, "main_player", "Main")
        protocol._attr_supported_features.add(PlayerFeature.VOLUME_MUTE)
        protocol._attr_volume_muted = False

        _attach_control(mock_mass, player, volume_muted=True)

        player.update_state(signal_event=False)
        assert player.volume_muted is True

    def test_falls_back_to_native_when_control_protocol_missing(self, mock_mass: MagicMock) -> None:
        """Final mute state falls back to native when the control protocol doesn't exist."""
        mock_mass.config.get_player_value = MagicMock(
            side_effect=lambda _pid, key, default=None: (
                "missing_player"
                if key == CONF_MUTE_CONTROL
                else 0
                if key == "min_volume"
                else 100
                if key == "max_volume"
                else default
            ),
        )
        provider = MockProvider("test", mass=mock_mass)
        player, protocol = attach_mock(provider, "main_player", "Main")
        protocol._attr_supported_features.add(PlayerFeature.VOLUME_MUTE)
        protocol._attr_volume_muted = True

        player.update_state(signal_event=False)
        assert player.volume_muted is True

    def test_reports_unknown_when_control_has_no_mute_state(self, mock_mass: MagicMock) -> None:
        """Final mute state is unknown when the resolved control's mute state is None."""
        mock_mass.config.get_player_value = MagicMock(
            side_effect=lambda _pid, key, default=None: (
                "control_player"
                if key == CONF_MUTE_CONTROL
                else 0
                if key == "min_volume"
                else 100
                if key == "max_volume"
                else default
            ),
        )
        provider = MockProvider("test", mass=mock_mass)
        player, protocol = attach_mock(provider, "main_player", "Main")
        protocol._attr_supported_features.add(PlayerFeature.VOLUME_MUTE)
        protocol._attr_volume_muted = False

        _attach_control(mock_mass, player, volume_muted=None)

        player.update_state(signal_event=False)
        assert player.volume_muted is None
