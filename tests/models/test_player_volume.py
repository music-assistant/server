"""
Tests for Player model final volume/mute state resolution.

Covers __final_volume_level and __final_volume_muted_state, which read the
final volume/mute exclusively from whichever control volume_control/mute_control
resolves to (native, a protocol player, or an external player control). The
final state is None (unknown) whenever that resolved control itself reports
no value.
"""

from __future__ import annotations

from typing import cast
from unittest.mock import AsyncMock, MagicMock, call

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


class TestReapplyVolume:
    """
    Tests for the generic Player.reapply_volume detour.

    A device can accept a volume set while it is idle, report it back as applied, keep playing
    at the old level, and then ignore a repeat of the value it is already reporting. Only a
    different value gets through, so the re-apply detours by one step and comes back.
    """

    @staticmethod
    def _player(mock_mass: MagicMock, volume_level: int | None) -> MockPlayer:
        provider = MockProvider("test", mass=mock_mass)
        player = MockPlayer(provider, "player_1", "Player 1")
        player._attr_volume_level = volume_level
        player._cache.clear()
        player.volume_set = AsyncMock()  # type: ignore[method-assign]
        return player

    async def test_detours_one_step_then_restores(self, mock_mass: MagicMock) -> None:
        """The detour value must differ from the target, or the device drops it as a no-op."""
        player = self._player(mock_mass, 30)
        await player.reapply_volume(1)
        assert cast("AsyncMock", player.volume_set).await_args_list == [call(29), call(30)]

    async def test_zero_volume_detours_upwards(self, mock_mass: MagicMock) -> None:
        """At zero the detour has to go up: -1 is not a volume any player would accept."""
        player = self._player(mock_mass, 0)
        await player.reapply_volume(1)
        assert cast("AsyncMock", player.volume_set).await_args_list == [call(1), call(0)]

    async def test_unknown_volume_sends_nothing(self, mock_mass: MagicMock) -> None:
        """With no volume known there is nothing to re-apply, and no value to invent."""
        player = self._player(mock_mass, None)
        await player.reapply_volume(1)
        cast("AsyncMock", player.volume_set).assert_not_awaited()

    async def test_step_below_one_percent_rounds_up(self, mock_mass: MagicMock) -> None:
        """
        volume_set carries whole percent, so a finer step becomes one whole step here.

        A player that can honour the configured step exactly overrides this - the Cast player
        does, because a rounded-up step is audible where the configured one is not.
        """
        player = self._player(mock_mass, 30)
        await player.reapply_volume(0.4)
        assert cast("AsyncMock", player.volume_set).await_args_list == [call(29), call(30)]

    async def test_larger_step_is_honoured(self, mock_mass: MagicMock) -> None:
        """A step big enough to express is used as given, for a device needing a coarser one."""
        player = self._player(mock_mass, 30)
        await player.reapply_volume(3)
        assert cast("AsyncMock", player.volume_set).await_args_list == [call(27), call(30)]

    async def test_oversized_step_stays_within_range(self, mock_mass: MagicMock) -> None:
        """
        The detour goes straight to the device, so it has to stay a valid volume itself.

        It skips the controller's 0-100 clamp, and a step larger than the headroom would
        otherwise send something out of range to the hardware.
        """
        player = self._player(mock_mass, 50)
        await player.reapply_volume(60)
        sent = [c.args[0] for c in cast("AsyncMock", player.volume_set).await_args_list]
        assert all(0 <= level <= 100 for level in sent)
        assert sent[-1] == 50

    async def test_step_that_fits_nowhere_sends_nothing(self, mock_mass: MagicMock) -> None:
        """A step with no room on either side has no detour value, so nothing is sent."""
        player = self._player(mock_mass, 100)
        await player.reapply_volume(150)
        cast("AsyncMock", player.volume_set).assert_not_awaited()

    async def test_detour_stays_above_the_min_volume_limit(self, mock_mass: MagicMock) -> None:
        """
        A configured min-volume floor bounds the detour, not just the hardware 0..100.

        The detour goes straight to the device; a value below the floor would be reported back
        as out of range and dragged to the floor by the controller's limit enforcement. At the
        floor the detour has to go up instead of below it.
        """
        player = self._player(mock_mass, 20)
        await player.reapply_volume(5, 20, 100)
        assert cast("AsyncMock", player.volume_set).await_args_list == [call(25), call(20)]

    async def test_detour_stays_below_the_max_volume_limit(self, mock_mass: MagicMock) -> None:
        """A configured max-volume ceiling bounds the up-detour, not just the hardware 100."""
        player = self._player(mock_mass, 0)
        await player.reapply_volume(10, 0, 5)
        assert cast("AsyncMock", player.volume_set).await_args_list == [call(5), call(0)]

    async def test_anchor_above_the_max_limit_sends_nothing(self, mock_mass: MagicMock) -> None:
        """
        A current volume already above the ceiling has no in-range detour, so nothing is sent.

        The device level can be moved out of the configured range externally (e.g. the Google
        Home app), and the Cast path anchors on that device-reported level. A detour from there
        lands out of range too; limit enforcement is about to drag the level back regardless.
        """
        player = self._player(mock_mass, 100)
        await player.reapply_volume(1, 0, 80)
        cast("AsyncMock", player.volume_set).assert_not_awaited()

    async def test_anchor_below_the_min_limit_sends_nothing(self, mock_mass: MagicMock) -> None:
        """A current volume already below the floor has no in-range detour either."""
        player = self._player(mock_mass, 10)
        await player.reapply_volume(1, 20, 100)
        cast("AsyncMock", player.volume_set).assert_not_awaited()
