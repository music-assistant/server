"""
Tests for Player model final volume/mute state resolution.

Covers __final_volume_level and __final_volume_muted_state, which read the
final volume/mute exclusively from whichever control volume_control/mute_control
resolves to (native, a protocol player, or an external player control). The
final state is None (unknown) whenever that resolved control itself reports
no value.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from music_assistant_models.enums import PlayerFeature

from music_assistant.constants import CONF_MUTE_CONTROL, CONF_VOLUME_CONTROL
from tests.common import MockPlayer, MockProvider


@pytest.fixture
def mock_mass() -> MagicMock:
    """Create a mock MusicAssistant instance."""
    mass = MagicMock()
    mass.closing = False
    mass.loop = None
    mass.config.get = MagicMock(return_value=[])
    mass.config.get_raw_player_config_value = MagicMock(return_value=None)
    mass.config.get_raw_core_config_value = MagicMock(return_value="GLOBAL")
    mass.config.set = MagicMock()
    mass.signal_event = MagicMock()
    mass.get_providers = MagicMock(return_value=[])
    return mass


class TestFinalVolumeLevel:
    """Test __final_volume_level resolution against the control volume_control resolves to."""

    def test_uses_control_player_when_available(self, mock_mass: MagicMock) -> None:
        """Final volume uses the control player's volume when it is present."""
        mock_mass.config.get_raw_player_config_value = MagicMock(
            side_effect=lambda _pid, key, *_a: (
                "control_player" if key == CONF_VOLUME_CONTROL else None
            )
        )
        provider = MockProvider("test", mass=mock_mass)
        player = MockPlayer(provider, "main_player", "Main")
        player._attr_volume_level = 30

        control = MagicMock()
        control.volume_level = 80
        mock_mass.players.get_player = MagicMock(return_value=control)
        mock_mass.players.get_player_control = MagicMock(return_value=None)
        # With default min=0, max=100, scaling is identity
        mock_mass.players.scale_volume_from_device = MagicMock(side_effect=lambda _pid, vol: vol)

        player.update_state(signal_event=False)
        assert player.state.volume_level == 80

    def test_falls_back_to_native_when_control_player_missing(self, mock_mass: MagicMock) -> None:
        """Final volume falls back to native when the control player doesn't exist."""
        mock_mass.config.get_raw_player_config_value = MagicMock(
            side_effect=lambda _pid, key, *_a: (
                "missing_player" if key == CONF_VOLUME_CONTROL else None
            )
        )
        provider = MockProvider("test", mass=mock_mass)
        player = MockPlayer(provider, "main_player", "Main")
        player._attr_volume_level = 55

        mock_mass.players.get_player = MagicMock(return_value=None)
        mock_mass.players.get_player_control = MagicMock(return_value=None)
        # With default min=0, max=100, scaling is identity
        mock_mass.players.scale_volume_from_device = MagicMock(side_effect=lambda _pid, vol: vol)

        player.update_state(signal_event=False)
        assert player.state.volume_level == 55

    def test_reports_unknown_when_control_has_no_volume(self, mock_mass: MagicMock) -> None:
        """Final volume is unknown when the resolved control's volume is None."""
        mock_mass.config.get_raw_player_config_value = MagicMock(
            side_effect=lambda _pid, key, *_a: (
                "control_player" if key == CONF_VOLUME_CONTROL else None
            )
        )
        provider = MockProvider("test", mass=mock_mass)
        player = MockPlayer(provider, "main_player", "Main")
        player._attr_volume_level = 42

        control = MagicMock()
        control.volume_level = None
        mock_mass.players.get_player = MagicMock(return_value=control)
        mock_mass.players.get_player_control = MagicMock(return_value=None)
        # With default min=0, max=100, scaling is identity
        mock_mass.players.scale_volume_from_device = MagicMock(side_effect=lambda _pid, vol: vol)

        player.update_state(signal_event=False)
        assert player.state.volume_level is None

    def test_uses_zero_from_external_control_player(self, mock_mass: MagicMock) -> None:
        """Volume 0 from an external (non-linked) control player is trusted as genuine."""
        mock_mass.config.get_raw_player_config_value = MagicMock(
            side_effect=lambda _pid, key, *_a: "amplifier" if key == CONF_VOLUME_CONTROL else None
        )
        provider = MockProvider("test", mass=mock_mass)
        player = MockPlayer(provider, "main_player", "Main")
        player._attr_volume_level = 55

        control = MagicMock()
        control.player_id = "amplifier"
        control.volume_level = 0
        mock_mass.players.get_player = MagicMock(return_value=control)
        mock_mass.players.get_player_control = MagicMock(return_value=None)
        # With default min=0, max=100, scaling is identity
        mock_mass.players.scale_volume_from_device = MagicMock(side_effect=lambda _pid, vol: vol)

        player.update_state(signal_event=False)
        assert player.state.volume_level == 0


class TestFinalVolumeMutedState:
    """Test __final_volume_muted_state resolution against the control mute_control resolves to."""

    def test_uses_control_player_when_available(self, mock_mass: MagicMock) -> None:
        """Final mute state uses the control player's mute state when present."""
        mock_mass.config.get_raw_player_config_value = MagicMock(
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
        player = MockPlayer(provider, "main_player", "Main")
        player._attr_supported_features.add(PlayerFeature.VOLUME_MUTE)
        player._attr_volume_muted = False

        control = MagicMock()
        control.volume_muted = True
        mock_mass.players.get_player = MagicMock(return_value=control)
        mock_mass.players.get_player_control = MagicMock(return_value=None)

        player.update_state(signal_event=False)
        assert player.state.volume_muted is True

    def test_falls_back_to_native_when_control_player_missing(self, mock_mass: MagicMock) -> None:
        """Final mute state falls back to native when the control player doesn't exist."""
        mock_mass.config.get_raw_player_config_value = MagicMock(
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
        player = MockPlayer(provider, "main_player", "Main")
        player._attr_supported_features.add(PlayerFeature.VOLUME_MUTE)
        player._attr_volume_muted = True

        mock_mass.players.get_player = MagicMock(return_value=None)
        mock_mass.players.get_player_control = MagicMock(return_value=None)

        player.update_state(signal_event=False)
        assert player.state.volume_muted is True

    def test_reports_unknown_when_control_has_no_mute_state(self, mock_mass: MagicMock) -> None:
        """Final mute state is unknown when the resolved control's mute state is None."""
        mock_mass.config.get_raw_player_config_value = MagicMock(
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
        player = MockPlayer(provider, "main_player", "Main")
        player._attr_supported_features.add(PlayerFeature.VOLUME_MUTE)
        player._attr_volume_muted = False

        control = MagicMock()
        control.volume_muted = None
        mock_mass.players.get_player = MagicMock(return_value=control)
        mock_mass.players.get_player_control = MagicMock(return_value=None)

        player.update_state(signal_event=False)
        assert player.state.volume_muted is None
