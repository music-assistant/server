"""Tests for the shared Sendspin bridge manager lifecycle reconciliation."""

import logging
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from music_assistant.providers.chromecast.sendspin_bridge import (
    SendspinBridgeManager as CastSendspinBridgeManager,
)
from music_assistant.providers.sendspin.bridge_manager import SendspinBridgeManagerBase


class FakeBridge:
    """Minimal Sendspin bridge implementation for testing."""

    def __init__(self, sendspin_server: Any) -> None:
        """Initialize the fake bridge."""
        self.sendspin_server = sendspin_server
        self.started = False
        self.stopped = False

    @property
    def is_registered(self) -> bool:
        """Return whether the bridge is registered with Sendspin."""
        return self.started and not self.stopped

    async def start(self) -> None:
        """Register the bridge as an external Sendspin client."""
        self.started = True

    async def stop(self) -> None:
        """Stop and unregister the bridge."""
        self.stopped = True


class FakeBridgeManager(SendspinBridgeManagerBase[FakeBridge]):
    """Concrete bridge manager with controllable policy for testing."""

    policy_result = True

    def _bridge_client_id(self, player: Any) -> str | None:
        """Return the Sendspin client_id used to bridge the given player."""
        return f"spb_{player.player_id}"

    def _create_bridge(self, player: Any) -> FakeBridge:
        """Create a (not yet started) bridge instance for the given player."""
        return FakeBridge(self.sendspin_server)

    def _should_have_bridge(self, player: Any) -> bool:
        """Return whether provider policy wants a bridge for this player."""
        return self.policy_result


def _make_environment() -> tuple[
    FakeBridgeManager, MagicMock, MagicMock, dict[str, Any], dict[str, Any]
]:
    """
    Build a bridge manager with a mocked MusicAssistant environment.

    :return: Tuple of (manager, mass, player, registered_players, player_configs).
    """
    registered_players: dict[str, Any] = {}
    player_configs: dict[str, Any] = {}

    mass = MagicMock()
    mass.subscribe = MagicMock(return_value=MagicMock())
    sendspin_provider = MagicMock()
    sendspin_provider.server_api = MagicMock()
    mass.get_provider = MagicMock(
        side_effect=lambda domain: sendspin_provider if domain == "sendspin" else None
    )
    mass.players.get_player = MagicMock(side_effect=registered_players.get)
    mass.config.get = MagicMock(
        side_effect=lambda key, default=None: player_configs.get(key, default)
    )

    async def fake_save_player_config(player_id: str, values: dict[str, Any]) -> None:
        player_configs.setdefault(f"players/{player_id}", {}).update(values)

    mass.config.save_player_config = AsyncMock(side_effect=fake_save_player_config)

    provider = MagicMock()
    provider.mass = mass
    provider.logger = logging.getLogger("test.bridge_manager")

    player = MagicMock()
    player.player_id = "player_1"
    player.display_name = "Test Player"
    player.provider = provider
    registered_players["player_1"] = player
    provider.players = [player]

    manager = FakeBridgeManager(provider)
    return manager, mass, player, registered_players, player_configs


class TestBridgeLifecycleReconciliation:
    """Tests for the desired-state reconciliation of Sendspin bridges."""

    @pytest.mark.asyncio
    async def test_bridge_created_when_all_conditions_met(self) -> None:
        """Test a bridge is created for a registered, enabled player."""
        manager, _, player, _, _ = _make_environment()

        await manager.evaluate_bridge(player)

        bridge = manager.get_bridge("player_1")
        assert bridge is not None
        assert bridge.is_registered

    @pytest.mark.asyncio
    async def test_bridge_removed_when_base_player_disabled(self) -> None:
        """Test the bridge is torn down when the base player gets disabled."""
        manager, _, player, _, player_configs = _make_environment()
        await manager.evaluate_bridge(player)
        bridge = manager.get_bridge("player_1")
        assert bridge is not None

        player_configs["players/player_1"] = {"enabled": False}
        await manager.evaluate_bridge(player)

        assert manager.get_bridge("player_1") is None
        assert bridge.stopped

    @pytest.mark.asyncio
    async def test_bridge_removed_when_bridge_client_disabled(self) -> None:
        """Test the bridge is torn down when its own Sendspin client gets disabled."""
        manager, mass, player, _, player_configs = _make_environment()
        await manager.evaluate_bridge(player)
        assert manager.get_bridge("player_1") is not None

        # a user-made disable carries the parent link the toggle was rendered under
        player_configs["players/player_1"] = {"enabled": True}
        player_configs["players/spb_player_1"] = {
            "enabled": False,
            "values": {"protocol_parent_id": "player_1"},
        }
        await manager.evaluate_bridge(player)

        assert manager.get_bridge("player_1") is None
        mass.config.save_player_config.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_bridge_not_created_for_unregistered_player(self) -> None:
        """Test no bridge is created when the player is not the registered instance."""
        manager, _, player, registered_players, _ = _make_environment()
        del registered_players["player_1"]

        await manager.evaluate_bridge(player)

        assert manager.get_bridge("player_1") is None

    @pytest.mark.asyncio
    async def test_policy_denial_removes_bridge_permanently(self) -> None:
        """Test a policy denial removes the bridge and cleans up the client config."""
        manager, mass, player, _, player_configs = _make_environment()
        await manager.evaluate_bridge(player)
        assert manager.get_bridge("player_1") is not None

        manager.policy_result = False
        player_configs["players/spb_player_1"] = {"enabled": True}
        await manager.evaluate_bridge(player)

        assert manager.get_bridge("player_1") is None
        mass.players.delete_player_config.assert_called_once_with("spb_player_1")

    @pytest.mark.asyncio
    async def test_config_event_on_bridge_client_recreates_bridge(self) -> None:
        """Test a config event on the bridge client id re-evaluates the base player."""
        manager, _, player, _, player_configs = _make_environment()
        player_configs["players/player_1"] = {"enabled": True}
        player_configs["players/spb_player_1"] = {
            "enabled": False,
            "values": {"protocol_parent_id": "player_1"},
        }
        await manager.evaluate_bridge(player)
        assert manager.get_bridge("player_1") is None

        # Re-enable the bridge client and fire the (mapped) config event
        player_configs["players/spb_player_1"] = {"enabled": True}
        event = MagicMock()
        event.object_id = "spb_player_1"
        await manager._on_player_config_updated(event)

        assert manager.get_bridge("player_1") is not None

    @pytest.mark.asyncio
    async def test_stale_disabled_client_reenabled_when_parent_gone(self) -> None:
        """Test a client disabled under a no-longer-existing parent is re-enabled."""
        manager, mass, player, _, player_configs = _make_environment()
        # e.g. left behind by a cascade-disable from a parent player whose
        # config was removed (device re-setup changed its player id)
        player_configs["players/spb_player_1"] = {
            "enabled": False,
            "values": {"protocol_parent_id": "cc_old_uuid"},
        }

        await manager.evaluate_bridge(player)

        mass.config.save_player_config.assert_awaited_once_with("spb_player_1", {"enabled": True})
        assert manager.get_bridge("player_1") is not None

    @pytest.mark.asyncio
    async def test_stale_disabled_client_without_parent_link_reenabled(self) -> None:
        """Test a disabled client without any parent link is re-enabled."""
        manager, mass, player, _, player_configs = _make_environment()
        player_configs["players/spb_player_1"] = {"enabled": False}

        await manager.evaluate_bridge(player)

        mass.config.save_player_config.assert_awaited_once_with("spb_player_1", {"enabled": True})
        assert manager.get_bridge("player_1") is not None

    @pytest.mark.asyncio
    async def test_no_heal_when_base_player_disabled(self) -> None:
        """Test a stale client disable is left alone while the base player is disabled."""
        manager, mass, player, _, player_configs = _make_environment()
        player_configs["players/player_1"] = {"enabled": False}
        player_configs["players/spb_player_1"] = {"enabled": False}

        await manager.evaluate_bridge(player)

        mass.config.save_player_config.assert_not_awaited()
        assert manager.get_bridge("player_1") is None

    @pytest.mark.asyncio
    async def test_stale_bridge_rebuilt_after_sendspin_reload(self) -> None:
        """Test a bridge bound to a replaced Sendspin server is rebuilt."""
        manager, mass, player, _, _ = _make_environment()
        await manager.evaluate_bridge(player)
        old_bridge = manager.get_bridge("player_1")
        assert old_bridge is not None

        # Simulate a sendspin provider reload (new server instance)
        new_provider = MagicMock()
        new_provider.server_api = MagicMock()
        mass.get_provider = MagicMock(
            side_effect=lambda domain: new_provider if domain == "sendspin" else None
        )
        await manager.evaluate_bridge(player)

        new_bridge = manager.get_bridge("player_1")
        assert old_bridge.stopped
        assert new_bridge is not None
        assert new_bridge is not old_bridge
        assert new_bridge.sendspin_server is new_provider.server_api


class TestCastBridgePolicy:
    """Tests for the Chromecast-specific bridge policy."""

    @staticmethod
    def _make_cast_environment() -> tuple[CastSendspinBridgeManager, MagicMock, MagicMock]:
        """
        Build a cast bridge manager with a mocked environment.

        :return: Tuple of (manager, mass, cast_player).
        """
        mass = MagicMock()
        mass.subscribe = MagicMock(return_value=MagicMock())
        mass.players.subscribe_player_state_update = MagicMock(return_value=MagicMock())
        mass.create_task = MagicMock()
        provider = MagicMock()
        provider.mass = mass
        provider.logger = logging.getLogger("test.cast_bridge_manager")

        cast_player = MagicMock()
        cast_player.player_id = "cc_player"
        cast_player.display_name = "Cast Speaker"
        cast_player.cast_info.is_audio_group = False
        cast_player.cast_info.is_multichannel_group = False
        cast_player.cast_info.mac_address = "AA:BB:CC:DD:EE:FF"
        cast_player.device_info.manufacturer = "TestCo"
        cast_player.device_info.model = "TestSpeaker"
        cast_player.protocol_parent_id = "parent_1"
        cast_player.provider = provider
        provider.players = [cast_player]

        manager = CastSendspinBridgeManager(provider)
        return manager, mass, cast_player

    def test_hard_deny_when_device_has_airplay(self) -> None:
        """Test the cast bridge is denied when the device has AirPlay at all."""
        manager, mass, cast_player = self._make_cast_environment()

        parent = MagicMock()
        airplay_protocol = MagicMock()
        # AirPlay protocol link present but NOT available (e.g. disabled by user)
        airplay_protocol.available = False
        parent.get_output_protocol_by_domain = MagicMock(return_value=airplay_protocol)
        mass.players.get_player = MagicMock(return_value=parent)

        assert manager._should_have_bridge(cast_player) is False

    def test_hard_deny_when_airplay_provider_not_loaded(self) -> None:
        """Test the deny also applies while the airplay provider itself is not loaded."""
        manager, mass, cast_player = self._make_cast_environment()

        parent = MagicMock()
        # Cached airplay protocol entry from an earlier session
        parent.get_output_protocol_by_domain = MagicMock(return_value=MagicMock())
        mass.get_provider = MagicMock(return_value=None)
        mass.players.get_player = MagicMock(return_value=parent)

        assert manager._should_have_bridge(cast_player) is False

    def test_allowed_when_device_has_no_airplay(self) -> None:
        """Test the cast bridge is allowed when the device has no AirPlay protocol."""
        manager, mass, cast_player = self._make_cast_environment()

        parent = MagicMock()
        parent.get_output_protocol_by_domain = MagicMock(return_value=None)
        mass.players.get_player = MagicMock(return_value=parent)

        assert manager._should_have_bridge(cast_player) is True

    def test_blocklist_message_only_repeats_after_policy_change(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Test repeated blocklist checks only log when the policy result changes."""
        manager, _, cast_player = self._make_cast_environment()
        cast_player.device_info.manufacturer = "Harman Luxury Audio"
        cast_player.protocol_parent_id = None

        with caplog.at_level(logging.DEBUG, logger=manager.logger.name):
            assert manager._should_have_bridge(cast_player) is False
            assert manager._should_have_bridge(cast_player) is False
            cast_player.device_info.manufacturer = "TestCo"
            assert manager._should_have_bridge(cast_player) is True
            cast_player.device_info.manufacturer = "Harman Luxury Audio"
            assert manager._should_have_bridge(cast_player) is False

        blocklist_records = [
            record for record in caplog.records if "device is blocklisted" in record.message
        ]
        assert len(blocklist_records) == 2

    def test_irrelevant_state_update_does_not_schedule_evaluation(self) -> None:
        """Test playback state updates do not re-evaluate bridge policy."""
        manager, mass, cast_player = self._make_cast_environment()

        manager._on_player_state_updated(cast_player, {"volume_level": (20, 30)})

        mass.create_task.assert_not_called()

    def test_relevant_cast_state_update_schedules_evaluation(self) -> None:
        """Test device policy changes re-evaluate the affected Cast bridge."""
        manager, mass, cast_player = self._make_cast_environment()

        manager._on_player_state_updated(
            cast_player, {"device_info.model": ("Old Model", "New Model")}
        )

        mass.create_task.assert_called_once_with(
            manager._process_pending_bridge_evaluations,
            cast_player.player_id,
            task_id="evaluate_chromecast_sendspin_bridge_cc_player",
        )

    def test_parent_protocol_update_schedules_evaluation(self) -> None:
        """Test protocol changes on a parent re-evaluate its Cast bridge."""
        manager, mass, _ = self._make_cast_environment()
        parent = MagicMock()
        parent.player_id = "parent_1"
        parent.protocol_parent_id = None

        manager._on_player_state_updated(parent, {"output_protocols": ((), ("airplay",))})

        mass.create_task.assert_called_once()

    def test_unregistered_parent_schedules_evaluation(self) -> None:
        """Test removal of a protocol parent re-evaluates its Cast bridge."""
        manager, mass, _ = self._make_cast_environment()
        mass.players.get_player.return_value = None
        event = MagicMock()
        event.object_id = "parent_1"

        manager._on_player_unregistered(event)

        mass.create_task.assert_called_once()

    def test_registered_player_event_does_not_duplicate_evaluation(self) -> None:
        """Test regular player events remain on the filtered state-update path."""
        manager, mass, _ = self._make_cast_environment()
        mass.players.get_player.return_value = MagicMock()
        event = MagicMock()
        event.object_id = "parent_1"

        manager._on_player_unregistered(event)

        mass.create_task.assert_not_called()

    @pytest.mark.asyncio
    async def test_update_during_evaluation_triggers_trailing_reconciliation(self) -> None:
        """Test a policy update during reconciliation is processed afterward."""
        manager, mass, cast_player = self._make_cast_environment()
        mass.players.get_player.return_value = cast_player
        evaluation_count = 0

        async def evaluate_bridge(player: Any) -> None:
            nonlocal evaluation_count
            evaluation_count += 1
            if evaluation_count == 1:
                manager._pending_bridge_evaluations.add(player.player_id)

        evaluate_mock = AsyncMock(side_effect=evaluate_bridge)
        manager.evaluate_bridge = evaluate_mock  # type: ignore[method-assign]
        manager._pending_bridge_evaluations.add(cast_player.player_id)

        await manager._process_pending_bridge_evaluations(cast_player.player_id)

        assert evaluate_mock.await_count == 2
        assert not manager._pending_bridge_evaluations
