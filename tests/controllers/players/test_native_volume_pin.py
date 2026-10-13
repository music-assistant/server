"""Tests for the native softvol pin when UI volume is mapped externally."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from music_assistant_models.config_entries import ConfigEntry, PlayerConfig
from music_assistant_models.constants import PLAYER_CONTROL_NATIVE, PLAYER_CONTROL_NONE
from music_assistant_models.enums import ConfigEntryType, PlayerFeature

from music_assistant.constants import (
    CONF_ENTRY_NATIVE_VOLUME,
    CONF_NATIVE_VOLUME,
    CONF_VOLUME_CONTROL,
    PLAYER_CONTROL_PROTOCOL,
)
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
    mass.config.get_raw_player_config_value = MagicMock(return_value=None)
    mass.config.get_raw_core_config_value = MagicMock(return_value="GLOBAL")
    mass.config.set = MagicMock()
    mass.signal_event = MagicMock()
    mass.get_providers = MagicMock(return_value=[])
    mass.create_task = MagicMock()
    return mass


def _make_player(
    mock_mass: MagicMock,
    *,
    features: set[PlayerFeature] | None = None,
    available: bool = True,
) -> tuple[MockPlayer, AsyncMock]:
    """Create a MockPlayer with VOLUME_SET and a mocked volume_set."""
    provider = MockProvider("test", mass=mock_mass)
    player = MockPlayer(provider, "pin_player", "Pin Player")
    player._attr_supported_features = (
        features if features is not None else {PlayerFeature.VOLUME_SET}
    )
    player._attr_available = available
    volume_set = AsyncMock()
    player.volume_set = volume_set  # type: ignore[method-assign]
    player._cache.clear()
    return player, volume_set


def _config_side_effect(overrides: dict[str, Any]) -> Any:
    """Build a get_raw_player_config_value side_effect from overrides."""

    def _side_effect(_pid: str, key: str, default: Any = None) -> Any:
        if key in overrides:
            return overrides[key]
        return default

    return _side_effect


class TestApplyNativeVolumePin:
    """Unit tests for PlayersController._apply_native_volume_pin."""

    async def test_applies_via_player_volume_set(self, mock_mass: MagicMock) -> None:
        """Pin calls player.volume_set with the configured level, not the mapped path."""
        mock_mass.config.get_raw_player_config_value = MagicMock(
            side_effect=_config_side_effect(
                {
                    CONF_VOLUME_CONTROL: "media_player.denon",
                    CONF_NATIVE_VOLUME: 100,
                }
            )
        )
        controller = PlayerController(mock_mass)
        player, volume_set = _make_player(mock_mass)

        await controller._apply_native_volume_pin(player)

        volume_set.assert_awaited_once_with(100)

    async def test_skips_when_pin_unset(self, mock_mass: MagicMock) -> None:
        """Missing pin value is opt-in: do not apply Softvol 100."""
        mock_mass.config.get_raw_player_config_value = MagicMock(
            side_effect=_config_side_effect({CONF_VOLUME_CONTROL: PLAYER_CONTROL_NONE})
        )
        controller = PlayerController(mock_mass)
        player, volume_set = _make_player(mock_mass)

        await controller._apply_native_volume_pin(player)

        volume_set.assert_not_awaited()

    async def test_skips_when_pin_value_invalid(self, mock_mass: MagicMock) -> None:
        """Invalid stored pin values are skipped rather than forced to 100."""
        mock_mass.config.get_raw_player_config_value = MagicMock(
            side_effect=_config_side_effect(
                {CONF_VOLUME_CONTROL: "media_player.denon", CONF_NATIVE_VOLUME: "loud"}
            )
        )
        controller = PlayerController(mock_mass)
        player, volume_set = _make_player(mock_mass)

        await controller._apply_native_volume_pin(player)

        volume_set.assert_not_awaited()

    async def test_skips_when_volume_control_is_native(self, mock_mass: MagicMock) -> None:
        """No pin when UI volume already drives the native player."""
        mock_mass.config.get_raw_player_config_value = MagicMock(
            side_effect=_config_side_effect(
                {CONF_VOLUME_CONTROL: PLAYER_CONTROL_NATIVE, CONF_NATIVE_VOLUME: 80}
            )
        )
        controller = PlayerController(mock_mass)
        player, volume_set = _make_player(mock_mass)

        await controller._apply_native_volume_pin(player)

        volume_set.assert_not_awaited()

    async def test_skips_when_volume_control_is_follow_protocol(self, mock_mass: MagicMock) -> None:
        """No pin when volume follows a protocol player."""
        mock_mass.config.get_raw_player_config_value = MagicMock(
            side_effect=_config_side_effect(
                {CONF_VOLUME_CONTROL: PLAYER_CONTROL_PROTOCOL, CONF_NATIVE_VOLUME: 80}
            )
        )
        controller = PlayerController(mock_mass)
        player, volume_set = _make_player(mock_mass)

        await controller._apply_native_volume_pin(player)

        volume_set.assert_not_awaited()

    async def test_skips_without_volume_set_feature(self, mock_mass: MagicMock) -> None:
        """No pin when the player cannot set native volume."""
        mock_mass.config.get_raw_player_config_value = MagicMock(
            side_effect=_config_side_effect(
                {CONF_VOLUME_CONTROL: "media_player.denon", CONF_NATIVE_VOLUME: 90}
            )
        )
        controller = PlayerController(mock_mass)
        player, volume_set = _make_player(mock_mass, features=set())

        await controller._apply_native_volume_pin(player)

        volume_set.assert_not_awaited()

    async def test_clamps_out_of_range_levels(self, mock_mass: MagicMock) -> None:
        """Out-of-range pin values are clamped to 0-100 before apply."""
        mock_mass.config.get_raw_player_config_value = MagicMock(
            side_effect=_config_side_effect(
                {CONF_VOLUME_CONTROL: "media_player.denon", CONF_NATIVE_VOLUME: 150}
            )
        )
        controller = PlayerController(mock_mass)
        player, volume_set = _make_player(mock_mass)

        await controller._apply_native_volume_pin(player)

        volume_set.assert_awaited_once_with(100)

    async def test_volume_set_errors_propagate(self, mock_mass: MagicMock) -> None:
        """A failing volume_set raises so config-save can roll back."""
        mock_mass.config.get_raw_player_config_value = MagicMock(
            side_effect=_config_side_effect(
                {CONF_VOLUME_CONTROL: "media_player.denon", CONF_NATIVE_VOLUME: 75}
            )
        )
        controller = PlayerController(mock_mass)
        player, volume_set = _make_player(mock_mass)
        volume_set.side_effect = RuntimeError("device busy")

        with pytest.raises(RuntimeError, match="device busy"):
            await controller._apply_native_volume_pin(player)


class TestNativeVolumePinOnConfigChange:
    """Config-change hook re-applies the pin when the pin or volume_control changes."""

    async def test_config_change_of_pin_triggers_apply(self, mock_mass: MagicMock) -> None:
        """Changing values/native_volume runs the apply helper."""
        controller = PlayerController(mock_mass)
        player, _volume_set = _make_player(mock_mass)
        controller._players[player.player_id] = player
        config = PlayerConfig(
            provider="test",
            player_id=player.player_id,
            values={
                CONF_NATIVE_VOLUME: ConfigEntry(
                    key=CONF_NATIVE_VOLUME,
                    type=ConfigEntryType.INTEGER,
                    default_value=None,
                    required=False,
                    value=85,
                )
            },
        )

        with (
            patch.object(controller, "_apply_native_volume_pin", new_callable=AsyncMock) as apply,
            patch.object(player, "set_config"),
            patch.object(player, "on_config_updated", new_callable=AsyncMock),
            patch.object(player, "update_state"),
        ):
            await controller.on_player_config_change(config, {f"values/{CONF_NATIVE_VOLUME}"})

        apply.assert_awaited_once_with(player)

    async def test_config_change_propagates_apply_errors(self, mock_mass: MagicMock) -> None:
        """Device rejection on config change must raise (save rolls back)."""
        controller = PlayerController(mock_mass)
        player, _volume_set = _make_player(mock_mass)
        controller._players[player.player_id] = player
        config = PlayerConfig(
            provider="test",
            player_id=player.player_id,
            values={
                CONF_NATIVE_VOLUME: ConfigEntry(
                    key=CONF_NATIVE_VOLUME,
                    type=ConfigEntryType.INTEGER,
                    default_value=None,
                    required=False,
                    value=85,
                )
            },
        )

        with (
            patch.object(
                controller,
                "_apply_native_volume_pin",
                new_callable=AsyncMock,
                side_effect=RuntimeError("device rejected"),
            ),
            patch.object(player, "set_config"),
            patch.object(player, "on_config_updated", new_callable=AsyncMock),
            patch.object(player, "update_state"),
            pytest.raises(RuntimeError, match="device rejected"),
        ):
            await controller.on_player_config_change(config, {f"values/{CONF_NATIVE_VOLUME}"})

    async def test_unrelated_config_change_does_not_apply(self, mock_mass: MagicMock) -> None:
        """Unrelated config keys do not re-apply the pin."""
        controller = PlayerController(mock_mass)
        player, _volume_set = _make_player(mock_mass)
        controller._players[player.player_id] = player
        config = PlayerConfig(
            provider="test",
            player_id=player.player_id,
            values={},
        )

        with (
            patch.object(controller, "_apply_native_volume_pin", new_callable=AsyncMock) as apply,
            patch.object(player, "set_config"),
            patch.object(player, "on_config_updated", new_callable=AsyncMock),
            patch.object(player, "update_state"),
        ):
            await controller.on_player_config_change(config, {"values/some_other_key"})

        apply.assert_not_awaited()


class TestNativeVolumePinOnRegister:
    """Register/reconnect callers catch pin failures so connect is not aborted."""

    async def test_register_or_update_swallows_apply_errors(self, mock_mass: MagicMock) -> None:
        """A failing pin on reconnect must not raise out of register_or_update."""
        mock_mass.config.get = MagicMock(side_effect=lambda _key, default=None: default)
        mock_mass.cache = MagicMock()
        mock_mass.cache.get = AsyncMock(return_value=None)
        queue = MagicMock()
        queue.items = []
        mock_mass.player_queues = MagicMock()
        mock_mass.player_queues.get = MagicMock(return_value=queue)
        controller = PlayerController(mock_mass)
        player, _volume_set = _make_player(mock_mass)
        player.set_initialized()
        controller._players[player.player_id] = player

        with (
            patch.object(player, "update_state"),
            patch.object(
                controller,
                "_apply_native_volume_pin",
                new_callable=AsyncMock,
                side_effect=RuntimeError("device busy"),
            ),
        ):
            await controller.register_or_update(player)

        assert controller._players[player.player_id] is player


class TestNativeVolumeConfigEntry:
    """Static checks on the shared ConfigEntry definition."""

    def test_entry_defaults(self) -> None:
        """Entry is optional integer 0-100, unset by default, under player_controls."""
        entry = CONF_ENTRY_NATIVE_VOLUME
        assert entry.key == CONF_NATIVE_VOLUME
        assert entry.type == ConfigEntryType.INTEGER
        assert entry.range == (0, 100)
        assert entry.default_value is None
        assert entry.required is False
        assert entry.category == "player_controls"
