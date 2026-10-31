"""Tests for the native softvol pin when UI volume is mapped externally."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from music_assistant_models.config_entries import ConfigEntry, PlayerConfig
from music_assistant_models.constants import PLAYER_CONTROL_NATIVE, PLAYER_CONTROL_NONE
from music_assistant_models.enums import ConfigEntryType, PlayerFeature
from music_assistant_models.errors import InvalidDataError, PlayerCommandFailed

from music_assistant.constants import (
    CONF_ENTRY_NATIVE_VOLUME,
    CONF_NATIVE_VOLUME,
    CONF_VOLUME_CONTROL,
    PLAYER_CONTROL_PROTOCOL,
)
from music_assistant.controllers.players import PlayerController
from music_assistant.controllers.players.constants import PlayerLockPurpose
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
    player.update_state(signal_event=False)
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

    async def test_rejects_when_pin_value_invalid(self, mock_mass: MagicMock) -> None:
        """Invalid stored pin values raise so config-save can roll back."""
        mock_mass.config.get_raw_player_config_value = MagicMock(
            side_effect=_config_side_effect(
                {CONF_VOLUME_CONTROL: "media_player.denon", CONF_NATIVE_VOLUME: "loud"}
            )
        )
        controller = PlayerController(mock_mass)
        player, volume_set = _make_player(mock_mass)

        with pytest.raises(InvalidDataError, match="Invalid native volume pin"):
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

    async def test_rejects_out_of_range_levels(self, mock_mass: MagicMock) -> None:
        """Out-of-range pin values raise instead of being coerced."""
        mock_mass.config.get_raw_player_config_value = MagicMock(
            side_effect=_config_side_effect(
                {CONF_VOLUME_CONTROL: "media_player.denon", CONF_NATIVE_VOLUME: 150}
            )
        )
        controller = PlayerController(mock_mass)
        player, volume_set = _make_player(mock_mass)

        with pytest.raises(InvalidDataError, match="out of range"):
            await controller._apply_native_volume_pin(player)

        volume_set.assert_not_awaited()

    async def test_volume_set_errors_normalize_to_player_command_failed(
        self, mock_mass: MagicMock
    ) -> None:
        """Provider device failures become PlayerCommandFailed for config rollback."""
        mock_mass.config.get_raw_player_config_value = MagicMock(
            side_effect=_config_side_effect(
                {CONF_VOLUME_CONTROL: "media_player.denon", CONF_NATIVE_VOLUME: 75}
            )
        )
        controller = PlayerController(mock_mass)
        player, volume_set = _make_player(mock_mass)
        volume_set.side_effect = RuntimeError("FailedCommand: device busy")

        with pytest.raises(PlayerCommandFailed, match="FailedCommand: device busy"):
            await controller._apply_native_volume_pin(player)

    async def test_skips_write_when_pin_cleared_while_waiting_for_lock(
        self, mock_mass: MagicMock
    ) -> None:
        """A config save that clears the pin while we wait must not send the old level."""
        overrides: dict[str, Any] = {
            CONF_VOLUME_CONTROL: "media_player.denon",
            CONF_NATIVE_VOLUME: 80,
        }
        mock_mass.config.get_raw_player_config_value = MagicMock(
            side_effect=_config_side_effect(overrides)
        )
        controller = PlayerController(mock_mass)
        player, volume_set = _make_player(mock_mass)

        lock_held = asyncio.Event()
        release_lock = asyncio.Event()

        async def _hold_volume_lock() -> None:
            async with controller.get_player_lock(player.player_id, PlayerLockPurpose.VOLUME):
                lock_held.set()
                await release_lock.wait()

        holder = asyncio.create_task(_hold_volume_lock())
        await lock_held.wait()

        apply_task = asyncio.create_task(controller._apply_native_volume_pin(player))
        # Let the apply task block on the volume lock before clearing the pin.
        await asyncio.sleep(0)
        overrides[CONF_NATIVE_VOLUME] = None
        release_lock.set()
        await apply_task
        await holder

        volume_set.assert_not_awaited()


class TestNativeVolumePinOnConfigChange:
    """Config-change hook re-applies the pin when the pin or volume_control changes."""

    async def test_config_change_of_pin_triggers_apply(self, mock_mass: MagicMock) -> None:
        """Changing values/native_volume runs the apply helper before set_config."""
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
        call_order: list[str] = []

        async def _apply(_player: Any) -> None:
            call_order.append("apply")

        def _set_config(_config: PlayerConfig) -> None:
            call_order.append("set_config")

        with (
            patch.object(
                controller, "_apply_native_volume_pin", new_callable=AsyncMock, side_effect=_apply
            ) as apply,
            patch.object(player, "set_config", side_effect=_set_config) as set_config,
            patch.object(player, "on_config_updated", new_callable=AsyncMock),
            patch.object(player, "update_state"),
        ):
            await controller.on_player_config_change(config, {f"values/{CONF_NATIVE_VOLUME}"})

        apply.assert_awaited_once_with(player)
        set_config.assert_called_once_with(config)
        assert call_order == ["apply", "set_config"]

    async def test_config_change_propagates_apply_errors(self, mock_mass: MagicMock) -> None:
        """Device rejection on config change must raise before mutating live config."""
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
            patch.object(player, "set_config") as set_config,
            patch.object(player, "on_config_updated", new_callable=AsyncMock) as on_updated,
            patch.object(player, "update_state") as update_state,
            pytest.raises(RuntimeError, match="device rejected"),
        ):
            await controller.on_player_config_change(config, {f"values/{CONF_NATIVE_VOLUME}"})

        set_config.assert_not_called()
        on_updated.assert_not_awaited()
        update_state.assert_not_called()

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
    """Register/reconnect callers catch expected pin failures so connect is not aborted."""

    async def test_register_or_update_swallows_normalized_device_errors(
        self, mock_mass: MagicMock
    ) -> None:
        """Provider device rejection during pin apply must not abort register_or_update."""
        mock_mass.config.get = MagicMock(side_effect=lambda _key, default=None: default)
        mock_mass.config.get_raw_player_config_value = MagicMock(
            side_effect=_config_side_effect(
                {CONF_VOLUME_CONTROL: "media_player.denon", CONF_NATIVE_VOLUME: 80}
            )
        )
        mock_mass.cache = MagicMock()
        mock_mass.cache.get = AsyncMock(return_value=None)
        queue = MagicMock()
        queue.items = []
        mock_mass.player_queues = MagicMock()
        mock_mass.player_queues.get = MagicMock(return_value=queue)
        controller = PlayerController(mock_mass)
        player, volume_set = _make_player(mock_mass)
        player.set_initialized()
        controller._players[player.player_id] = player
        volume_set.side_effect = RuntimeError("FailedCommand: device rejected")

        with patch.object(player, "update_state"):
            await controller.register_or_update(player)

        assert controller._players[player.player_id] is player
        volume_set.assert_awaited_once_with(80)

    async def test_register_or_update_propagates_unexpected_errors(
        self, mock_mass: MagicMock
    ) -> None:
        """Programming errors on reconnect still surface out of register_or_update."""
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
                side_effect=AttributeError("bug in pin helper"),
            ),
            pytest.raises(AttributeError, match="bug in pin helper"),
        ):
            await controller.register_or_update(player)


class _Timers:
    """Run the native volume pin timers on the real loop, record every other timer."""

    def __init__(self) -> None:
        """Initialize the timer bookkeeping."""
        self.handles: dict[str, asyncio.TimerHandle] = {}
        self.tasks: list[asyncio.Task[Any]] = []

    def call_later(
        self, delay: float, target: Any, *args: Any, task_id: str | None = None, **_kwargs: Any
    ) -> Any:
        """Mimic MusicAssistant.call_later for the pin task only."""
        if not task_id or not task_id.startswith("native_volume_pin_"):
            return MagicMock()
        if existing := self.handles.pop(task_id, None):
            existing.cancel()

        def _run() -> None:
            self.handles.pop(task_id, None)
            self.tasks.append(asyncio.ensure_future(target(*args)))

        handle = asyncio.get_running_loop().call_later(delay, _run)
        self.handles[task_id] = handle
        return handle

    def cancel_timer(self, task_id: str) -> None:
        """Mimic MusicAssistant.cancel_timer."""
        if existing := self.handles.pop(task_id, None):
            existing.cancel()

    async def settle(self) -> None:
        """Wait for every pending pin timer and the writes it started."""
        await asyncio.sleep(NATIVE_VOLUME_PIN_TEST_DELAY * 3)
        await asyncio.gather(*self.tasks)


NATIVE_VOLUME_PIN_TEST_DELAY = 0.01


class TestNativeVolumePinOnAvailability:
    """A player coming back online gets the pin without registering again."""

    @pytest.fixture(autouse=True)
    def _short_delay(self) -> Any:
        """Shorten the online-transition delay for the tests."""
        with patch(
            "music_assistant.controllers.players.controller.NATIVE_VOLUME_PIN_DELAY",
            NATIVE_VOLUME_PIN_TEST_DELAY,
        ):
            yield

    def _setup(
        self, mock_mass: MagicMock, overrides: dict[str, Any], *, available: bool
    ) -> tuple[PlayerController, MockPlayer, AsyncMock, _Timers]:
        """
        Build a controller with one registered player and working pin timers.

        :param mock_mass: The mock MusicAssistant instance to build it on.
        :param overrides: Stored raw config values of the player.
        :param available: Whether the player starts out available.
        """
        mock_mass.config.get_raw_player_config_value = MagicMock(
            side_effect=_config_side_effect(overrides)
        )
        timers = _Timers()
        mock_mass.call_later = MagicMock(side_effect=timers.call_later)
        mock_mass.cancel_timer = MagicMock(side_effect=timers.cancel_timer)
        controller = PlayerController(mock_mass)
        mock_mass.players = controller
        player, volume_set = _make_player(mock_mass, available=available)
        controller._players[player.player_id] = player
        controller._forward_state_update = MagicMock()  # type: ignore[method-assign]
        player.set_initialized()
        player.update_state(signal_event=False)
        return controller, player, volume_set, timers

    @staticmethod
    def _set_available(player: MockPlayer, available: bool) -> None:
        """Flip availability the way a provider's connection handler does."""
        player._attr_available = available
        player.update_state()

    async def test_player_registered_unavailable_gets_pin_when_online(
        self, mock_mass: MagicMock
    ) -> None:
        """A player that registered while unavailable is pinned once it connects."""
        controller, player, volume_set, timers = self._setup(
            mock_mass,
            {CONF_VOLUME_CONTROL: "media_player.denon", CONF_NATIVE_VOLUME: 90},
            available=False,
        )
        await controller._apply_native_volume_pin_logged(player)
        volume_set.assert_not_awaited()

        self._set_available(player, True)
        await timers.settle()

        volume_set.assert_awaited_once_with(90)

    async def test_reconnect_reapplies_pin(self, mock_mass: MagicMock) -> None:
        """Dropping offline and coming back writes the pin again."""
        _controller, player, volume_set, timers = self._setup(
            mock_mass,
            {CONF_VOLUME_CONTROL: "media_player.denon", CONF_NATIVE_VOLUME: 70},
            available=True,
        )

        self._set_available(player, False)
        await timers.settle()
        volume_set.assert_not_awaited()

        self._set_available(player, True)
        await timers.settle()

        volume_set.assert_awaited_once_with(70)

    async def test_pin_saved_while_offline_applies_when_online(self, mock_mass: MagicMock) -> None:
        """Saving the pin while the player is offline applies it once the player returns."""
        overrides: dict[str, Any] = {CONF_VOLUME_CONTROL: "media_player.denon"}
        controller, player, volume_set, timers = self._setup(mock_mass, overrides, available=False)
        overrides[CONF_NATIVE_VOLUME] = 60
        config = PlayerConfig(provider="test", player_id=player.player_id, values={})
        with (
            patch.object(player, "set_config"),
            patch.object(player, "on_config_updated", new_callable=AsyncMock),
        ):
            await controller.on_player_config_change(config, {f"values/{CONF_NATIVE_VOLUME}"})
        volume_set.assert_not_awaited()

        self._set_available(player, True)
        await timers.settle()

        volume_set.assert_awaited_once_with(60)

    async def test_flapping_connection_writes_once(self, mock_mass: MagicMock) -> None:
        """A quick drop-and-return within the delay results in a single write."""
        _controller, player, volume_set, timers = self._setup(
            mock_mass,
            {CONF_VOLUME_CONTROL: "media_player.denon", CONF_NATIVE_VOLUME: 80},
            available=False,
        )

        self._set_available(player, True)
        self._set_available(player, False)
        self._set_available(player, True)
        await timers.settle()

        volume_set.assert_awaited_once_with(80)

    async def test_register_or_update_supersedes_pending_online_pin(
        self, mock_mass: MagicMock
    ) -> None:
        """A provider that re-registers on reconnect gets one write, not two."""
        controller, player, volume_set, timers = self._setup(
            mock_mass,
            {CONF_VOLUME_CONTROL: "media_player.denon", CONF_NATIVE_VOLUME: 80},
            available=False,
        )
        mock_mass.config.get = MagicMock(side_effect=lambda _key, default=None: default)
        queue = MagicMock()
        queue.items = []
        mock_mass.player_queues.get = MagicMock(return_value=queue)

        player._attr_available = True
        await controller.register_or_update(player)
        await timers.settle()

        volume_set.assert_awaited_once_with(80)

    async def test_player_disabled_during_delay_is_not_pinned(self, mock_mass: MagicMock) -> None:
        """A player disabled before the pending pin runs is left alone."""
        _controller, player, volume_set, timers = self._setup(
            mock_mass,
            {CONF_VOLUME_CONTROL: "media_player.denon", CONF_NATIVE_VOLUME: 80},
            available=False,
        )

        self._set_available(player, True)
        player._config.enabled = False
        player.update_state(force_update=True, signal_event=False)
        await timers.settle()

        volume_set.assert_not_awaited()

    async def test_shutdown_during_delay_does_not_pin(self, mock_mass: MagicMock) -> None:
        """No pin is written once the server is shutting down."""
        _controller, player, volume_set, timers = self._setup(
            mock_mass,
            {CONF_VOLUME_CONTROL: "media_player.denon", CONF_NATIVE_VOLUME: 80},
            available=False,
        )

        self._set_available(player, True)
        mock_mass.closing = True
        await timers.settle()

        volume_set.assert_not_awaited()

    async def test_online_without_pin_does_not_write(self, mock_mass: MagicMock) -> None:
        """Coming online without a configured pin leaves the player volume alone."""
        _controller, player, volume_set, timers = self._setup(
            mock_mass, {CONF_VOLUME_CONTROL: "media_player.denon"}, available=False
        )

        self._set_available(player, True)
        await timers.settle()

        volume_set.assert_not_awaited()


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
