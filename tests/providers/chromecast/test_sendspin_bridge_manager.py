"""Tests for the Chromecast Sendspin bridge manager: its policy, opt-in and fatal errors."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from music_assistant_models.errors import PlayerCommandFailed

from music_assistant.constants import CONF_PROTOCOL_EXPERIMENTAL_NOTE, CONF_UNDERLYING_PLAYER_ID
from music_assistant.providers.chromecast import sendspin_bridge as bridge_module
from music_assistant.providers.chromecast.constants import (
    CONF_SENDSPIN_OPT_OUT_PENDING,
    CONF_SENDSPIN_UNSUPPORTED,
    SENDSPIN_CAST_EXPERIMENTAL_NOTE,
)
from music_assistant.providers.chromecast.sendspin_bridge import (
    SendspinBridgeManager as CastSendspinBridgeManager,
)
from music_assistant.providers.sendspin import constants as sendspin_constants
from music_assistant.providers.sendspin.bridge_manager import SendspinBridgeManagerBase
from tests.common import scheduled_call

CAST_ID = "cc_player"
# the player of the Cast protocol has an id of its own
PLAYER_ID = "pl0123456789ab"


def _player(*, airplay: Any = None) -> Any:
    """
    Return a stand-in of the player a Cast protocol is attached to.

    :param airplay: What the player lists as its AirPlay output, if anything.
    """
    player = MagicMock(player_id=PLAYER_ID)
    player.get_output_protocol_by_domain = MagicMock(return_value=airplay)
    return player


def _make_cast_environment() -> tuple[CastSendspinBridgeManager, MagicMock, MagicMock]:
    """
    Build a cast bridge manager with a mocked environment.

    :return: Tuple of (manager, mass, cast_protocol).
    """
    mass = MagicMock()
    mass.subscribe = MagicMock(return_value=MagicMock())
    mass.players.subscribe_player_state_update = MagicMock(return_value=MagicMock())
    mass.create_task = MagicMock()
    mass.config.get_protocol_value = MagicMock(return_value=None)
    external_clients: set[str] = set()
    sendspin_server = mass.get_provider.return_value.server_api
    sendspin_server.is_external_player = MagicMock(side_effect=external_clients.__contains__)
    mass.external_clients = external_clients
    provider = MagicMock()
    provider.mass = mass
    provider.logger = logging.getLogger("test.cast_bridge_manager")

    cast_protocol = MagicMock()
    cast_protocol.protocol_id = CAST_ID
    cast_protocol.display_name = "Cast Speaker"
    cast_protocol.cast_info.is_audio_group = False
    cast_protocol.cast_info.is_multichannel_group = False
    cast_protocol.cast_info.mac_address = "AA:BB:CC:DD:EE:FF"
    cast_protocol.device_info.manufacturer = "TestCo"
    cast_protocol.device_info.model = "TestSpeaker"
    cast_protocol.player = _player()
    cast_protocol.provider = provider
    cast_protocol._on_detach_callbacks = []
    provider.protocols = [cast_protocol]
    provider.get_protocol = lambda protocol_id: cast_protocol if protocol_id == CAST_ID else None
    mass.players.get_protocol = MagicMock(side_effect=provider.get_protocol)

    manager = CastSendspinBridgeManager(provider)
    return manager, mass, cast_protocol


class TestCastBridgePolicy:
    """Tests for the Chromecast-specific bridge policy."""

    def test_hard_deny_when_device_has_airplay(self) -> None:
        """Test the cast bridge is denied when the device has AirPlay at all."""
        manager, _, cast_protocol = _make_cast_environment()
        airplay_output = MagicMock()
        # AirPlay output listed but NOT available (e.g. disabled by user)
        airplay_output.available = False
        cast_protocol.player = _player(airplay=airplay_output)

        assert manager._should_have_bridge(cast_protocol) is False

    def test_hard_deny_when_airplay_provider_not_loaded(self) -> None:
        """Test the deny also applies while the airplay provider itself is not loaded."""
        manager, mass, cast_protocol = _make_cast_environment()
        # the AirPlay output of the player, from its stored protocol record
        cast_protocol.player = _player(airplay=MagicMock())
        mass.get_provider = MagicMock(return_value=None)

        assert manager._should_have_bridge(cast_protocol) is False

    def test_allowed_when_device_has_no_airplay(self) -> None:
        """Test the cast bridge is allowed when the device has no AirPlay protocol."""
        manager, _, cast_protocol = _make_cast_environment()

        assert manager._should_have_bridge(cast_protocol) is True

    def test_denied_while_airplay_bridge_holds_the_other_mac_form(self) -> None:
        """Test the deny also holds before the AirPlay protocol joined the player."""
        manager, mass, cast_protocol = _make_cast_environment()
        # AirPlay's bridge registers under the locally-administered form (0xAA ^ 0x02)
        mass.external_clients.add("spb_a8bbccddeeff")

        assert manager._should_have_bridge(cast_protocol) is False

    async def test_bridge_client_under_the_same_mac_is_left_alone(self) -> None:
        """Test a client another bridge registered under the same id is neither claimed nor replaced."""
        manager, mass, cast_protocol = _make_cast_environment()
        client_id = manager._bridge_client_id(cast_protocol)
        assert client_id is not None
        mass.external_clients.add(client_id)

        with patch.object(manager, "_create_bridge", return_value=AsyncMock()) as create_bridge:
            await manager.setup_bridge(cast_protocol)

        create_bridge.assert_not_called()
        assert not manager._claimed_clients

    def test_own_bridge_is_not_denied(self) -> None:
        """Test the Cast bridge's own registered client does not deny the bridge."""
        manager, mass, cast_protocol = _make_cast_environment()
        client_id = manager._bridge_client_id(cast_protocol)
        assert client_id is not None
        mass.external_clients.add(client_id)
        manager._bridges[CAST_ID] = MagicMock()

        assert manager._should_have_bridge(cast_protocol) is True

    async def test_self_connected_receiver_is_claimed(self) -> None:
        """Test a Cast receiver that reconnected on its own is adopted, riding on the protocol."""
        manager, mass, cast_protocol = _make_cast_environment()
        client_id = manager._bridge_client_id(cast_protocol)
        assert client_id is not None
        sendspin = mass.get_provider.return_value
        sendspin.apply_bridge_claim = AsyncMock(return_value=True)

        assert await manager._try_claim_existing(cast_protocol) is True
        assert manager._claimed_clients == {CAST_ID: client_id}
        assert sendspin.apply_bridge_claim.call_args.kwargs["underlying_protocol_id"] == CAST_ID

    async def test_a_claimed_client_goes_with_its_protocol_for_good(self) -> None:
        """Test a permanent removal takes the claimed client along when it rides on the protocol."""
        manager, mass, cast_protocol = _make_cast_environment()
        client_id = manager._bridge_client_id(cast_protocol)
        assert client_id is not None
        manager._claimed_clients[CAST_ID] = client_id
        mass.players.remove_protocol = AsyncMock()
        mass.config.get_protocol_record = MagicMock(
            return_value={"values": {CONF_UNDERLYING_PLAYER_ID: CAST_ID}}
        )

        await manager.remove_bridge(CAST_ID, permanent=True)

        assert not manager._claimed_clients
        mass.players.remove_protocol.assert_awaited_once_with(client_id)

    def test_denied_after_the_device_reported_it_cannot_run_sendspin(self) -> None:
        """Test a device that failed once is not offered the bridge again at all."""
        manager, mass, cast_protocol = _make_cast_environment()
        assert manager._should_have_bridge(cast_protocol) is True

        mass.config.get_protocol_value = MagicMock(
            side_effect=lambda protocol_id, key, default=None: (
                True if protocol_id == CAST_ID and key == CONF_SENDSPIN_UNSUPPORTED else default
            )
        )

        assert manager._should_have_bridge(cast_protocol) is False

    def test_blocklist_message_only_repeats_after_policy_change(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Test repeated blocklist checks only log when the policy result changes."""
        manager, _, cast_protocol = _make_cast_environment()
        cast_protocol.device_info.manufacturer = "Harman Luxury Audio"

        with caplog.at_level(logging.DEBUG, logger=manager.logger.name):
            assert manager._should_have_bridge(cast_protocol) is False
            assert manager._should_have_bridge(cast_protocol) is False
            cast_protocol.device_info.manufacturer = "TestCo"
            assert manager._should_have_bridge(cast_protocol) is True
            cast_protocol.device_info.manufacturer = "Harman Luxury Audio"
            assert manager._should_have_bridge(cast_protocol) is False

        blocklist_records = [
            record for record in caplog.records if "device is blocklisted" in record.message
        ]
        assert len(blocklist_records) == 2

    def test_irrelevant_state_update_does_not_schedule_evaluation(self) -> None:
        """Test playback state updates do not re-evaluate bridge policy."""
        manager, mass, cast_protocol = _make_cast_environment()
        player = SimpleNamespace(attached_protocols={CAST_ID: cast_protocol})

        manager._on_player_state_updated(player, {"volume_level": (20, 30)})  # type: ignore[arg-type]

        mass.create_task.assert_not_called()

    @pytest.mark.parametrize(
        "changed",
        [
            {"device_info.model": ("Old Model", "New Model")},
            # the AirPlay protocol joined the player
            {"output_protocols": ((), ("airplay",))},
            # the player of the Cast protocol now always exists: it goes and comes back
            {"available": (True, False)},
        ],
    )
    def test_relevant_player_update_schedules_evaluation_of_its_cast_protocol(
        self, changed: dict[str, tuple[Any, Any]]
    ) -> None:
        """Test a policy-relevant change of a player re-evaluates the bridge of its Cast protocol."""
        manager, mass, cast_protocol = _make_cast_environment()
        other_protocol = MagicMock(protocol_id="ap_other", provider=MagicMock())
        player = SimpleNamespace(
            attached_protocols={CAST_ID: cast_protocol, "ap_other": other_protocol}
        )

        manager._on_player_state_updated(player, changed)  # type: ignore[arg-type]

        mass.create_task.assert_called_once()
        name, arguments = scheduled_call(mass.create_task.call_args.args[0])
        assert name.endswith("_process_pending_bridge_evaluations")
        assert arguments["protocol_id"] == CAST_ID
        assert mass.create_task.call_args.kwargs == {
            "task_id": "evaluate_chromecast_sendspin_bridge_cc_player",
        }

    async def test_update_during_evaluation_triggers_trailing_reconciliation(self) -> None:
        """Test a policy update during reconciliation is processed afterward."""
        manager, _, cast_protocol = _make_cast_environment()
        evaluation_count = 0

        async def evaluate_bridge(protocol: Any) -> None:
            nonlocal evaluation_count
            evaluation_count += 1
            if evaluation_count == 1:
                manager._pending_bridge_evaluations.add(protocol.protocol_id)

        evaluate_mock = AsyncMock(side_effect=evaluate_bridge)
        manager.evaluate_bridge = evaluate_mock  # type: ignore[method-assign]
        manager._pending_bridge_evaluations.add(CAST_ID)

        await manager._process_pending_bridge_evaluations(CAST_ID)

        assert evaluate_mock.await_count == 2
        assert not manager._pending_bridge_evaluations
        evaluate_mock.assert_awaited_with(cast_protocol)


def _make_opt_in_environment() -> tuple[
    CastSendspinBridgeManager, MagicMock, MagicMock, dict[str, Any], list[Any], list[str]
]:
    """
    Build a cast bridge manager with dict-backed protocol and player records.

    :return: Tuple of (manager, mass, cast_protocol, configs, scheduled, write_order).
    """
    manager, mass, cast_protocol = _make_cast_environment()
    # the stored config: protocol records by protocol id, player records by storage key
    configs: dict[str, Any] = {}
    scheduled: list[Any] = []
    # both config writes land in one log, so a test can assert their relative order
    write_order: list[str] = []

    def get_value(protocol_id: str, key: str, default: Any = None) -> Any:
        values = (configs.get(protocol_id) or {}).get("values", {})
        return values.get(key, default)

    def set_value(protocol_id: str, key: str, value: Any) -> None:
        record = configs.setdefault(protocol_id, {})
        record.setdefault("values", {})[key] = value
        if key == CONF_PROTOCOL_EXPERIMENTAL_NOTE:
            write_order.append("note")

    async def save(protocol_id: str, values: dict[str, Any]) -> None:
        configs.setdefault(protocol_id, {}).update(values)
        write_order.append("save")

    mass.config.get = MagicMock(side_effect=lambda key, default=None: configs.get(key, default))
    mass.config.get_protocol_record = MagicMock(side_effect=configs.get)
    mass.config.get_protocol_value = MagicMock(side_effect=get_value)
    mass.config.set_protocol_value = MagicMock(side_effect=set_value)
    mass.config.save_protocol_config = AsyncMock(side_effect=save)
    mass.create_task = MagicMock(side_effect=lambda target, *_a, **_kw: scheduled.append(target))
    return manager, mass, cast_protocol, configs, scheduled, write_order


def _store_attached_client(configs: dict[str, Any], client_id: str, **record: Any) -> None:
    """
    Store the protocol record of the bridge client as its attach does, and its player's.

    :param configs: The dict-backed config.
    :param client_id: The client id of the bridge.
    :param record: More keys of the client's record.
    """
    configs[client_id] = {"enabled": True, "player_id": PLAYER_ID, "values": {}, **record}
    configs[f"players/{PLAYER_ID}"] = {"enabled": True}


class TestCastBridgeOptIn:
    """Tests for the experimental opt-in of the Sendspin Cast bridge."""

    async def test_new_device_is_flagged_and_left_switched_off(self) -> None:
        """Test a device that never had the bridge keeps it off for the user to opt in."""
        manager, mass, cast_protocol, configs, scheduled, order = _make_opt_in_environment()
        client_id = manager._bridge_client_id(cast_protocol)
        assert client_id is not None
        configs[CAST_ID] = {"enabled": True}

        async def fake_super(protocol: Any) -> None:
            """Register the bridge the way a real setup would, its client attached."""
            _store_attached_client(configs, client_id)
            manager._bridges[protocol.protocol_id] = MagicMock()

        with patch.object(SendspinBridgeManagerBase, "evaluate_bridge", side_effect=fake_super):
            await manager.evaluate_bridge(cast_protocol)
        assert len(scheduled) == 1
        await scheduled[0]

        mass.config.save_protocol_config.assert_awaited_once_with(client_id, {"enabled": False})
        assert configs[client_id]["enabled"] is False
        assert configs[client_id]["values"][CONF_PROTOCOL_EXPERIMENTAL_NOTE] == (
            SENDSPIN_CAST_EXPERIMENTAL_NOTE
        )
        # the note is what stops the next evaluation from retrying, so it must come last
        assert order == ["save", "note"]

    async def test_device_that_already_had_the_bridge_keeps_it_on(self) -> None:
        """Test an existing setup is flagged as experimental but not switched off."""
        manager, mass, cast_protocol, configs, scheduled, _ = _make_opt_in_environment()
        client_id = manager._bridge_client_id(cast_protocol)
        assert client_id is not None
        configs[CAST_ID] = {"enabled": True}
        _store_attached_client(configs, client_id)

        async def fake_super(protocol: Any) -> None:
            manager._bridges[protocol.protocol_id] = MagicMock()

        with patch.object(SendspinBridgeManagerBase, "evaluate_bridge", side_effect=fake_super):
            await manager.evaluate_bridge(cast_protocol)
        assert len(scheduled) == 1
        await scheduled[0]

        assert configs[client_id]["enabled"] is True
        mass.config.save_protocol_config.assert_not_awaited()

    async def test_already_flagged_device_is_left_alone(self) -> None:
        """Test a device that carries the warning already does no further work."""
        manager, mass, cast_protocol, configs, scheduled, _ = _make_opt_in_environment()
        client_id = manager._bridge_client_id(cast_protocol)
        assert client_id is not None
        _store_attached_client(
            configs,
            client_id,
            enabled=False,
            values={CONF_PROTOCOL_EXPERIMENTAL_NOTE: SENDSPIN_CAST_EXPERIMENTAL_NOTE},
        )
        manager._bridges[CAST_ID] = MagicMock()

        with patch.object(SendspinBridgeManagerBase, "evaluate_bridge", new=AsyncMock()):
            await manager.evaluate_bridge(cast_protocol)

        assert not scheduled
        mass.config.set_protocol_value.assert_not_called()

    async def test_the_warning_text_is_authored(self) -> None:
        """Test the note the output renders resolves to a string that exists."""
        manager, _, cast_protocol, configs, _, _ = _make_opt_in_environment()
        client_id = manager._bridge_client_id(cast_protocol)
        assert client_id is not None
        _store_attached_client(configs, client_id)
        manager._bridges[CAST_ID] = MagicMock()

        await manager._flag_bridge_experimental(CAST_ID, client_id, disable=False)

        values = configs[client_id]["values"]
        assert values[CONF_PROTOCOL_EXPERIMENTAL_NOTE] == SENDSPIN_CAST_EXPERIMENTAL_NOTE
        # the note is rendered as an alert, so guard the authored string against drift
        strings = json.loads(
            (Path(sendspin_constants.__file__).resolve().parent / "strings.json").read_text(
                encoding="utf-8"
            )
        )
        assert SENDSPIN_CAST_EXPERIMENTAL_NOTE in strings["config_entries"]

    async def test_bridge_whose_client_is_no_output_of_a_player_yet_is_not_switched_off(
        self,
    ) -> None:
        """Test the bridge stays as-is while there is no toggle to opt back in with."""
        manager, mass, cast_protocol, configs, _, _ = _make_opt_in_environment()
        client_id = manager._bridge_client_id(cast_protocol)
        assert client_id is not None
        # stored, but not attached to a player yet
        configs[client_id] = {"enabled": True, "player_id": None, "values": {}}
        manager._bridges[CAST_ID] = MagicMock()

        with (
            patch.object(bridge_module, "SENDSPIN_LINK_WAIT_TRIES", 2),
            patch.object(bridge_module, "SENDSPIN_LINK_WAIT_INTERVAL", 0),
        ):
            await manager._flag_bridge_experimental(CAST_ID, client_id, disable=True)

        assert configs[client_id]["enabled"] is True
        assert CONF_PROTOCOL_EXPERIMENTAL_NOTE not in configs[client_id]["values"]
        mass.config.save_protocol_config.assert_not_awaited()

    async def test_a_missed_opt_out_is_retried_on_the_next_evaluation(self) -> None:
        """Test a device whose client never got attached in time still gets switched off later."""
        manager, _, cast_protocol, configs, scheduled, _ = _make_opt_in_environment()
        client_id = manager._bridge_client_id(cast_protocol)
        assert client_id is not None
        # what a previous run left behind: bridged and marked, but never switched off
        # because its client was not an output of a player yet
        configs[CAST_ID] = {"enabled": True, "values": {CONF_SENDSPIN_OPT_OUT_PENDING: True}}
        _store_attached_client(configs, client_id)

        async def fake_super(protocol: Any) -> None:
            manager._bridges[protocol.protocol_id] = MagicMock()

        with patch.object(SendspinBridgeManagerBase, "evaluate_bridge", side_effect=fake_super):
            await manager.evaluate_bridge(cast_protocol)
        assert len(scheduled) == 1
        await scheduled[0]

        assert configs[client_id]["enabled"] is False
        # settled now, so the marker is cleared and nothing retries again
        assert not configs[CAST_ID]["values"][CONF_SENDSPIN_OPT_OUT_PENDING]

    async def test_the_wait_gives_up_as_soon_as_the_bridge_is_taken_away(self) -> None:
        """Test a bridge denied mid-wait stops the poll instead of running it out."""
        manager, mass, cast_protocol, _, _, _ = _make_opt_in_environment()
        client_id = manager._bridge_client_id(cast_protocol)
        assert client_id is not None
        polls = 0

        def get_record(_protocol_id: str) -> dict[str, Any]:
            nonlocal polls
            polls += 1
            # the device turns out to speak AirPlay too, so policy pulls the bridge
            manager._bridges.pop(CAST_ID, None)
            return {"enabled": True, "player_id": None}

        manager._bridges[CAST_ID] = MagicMock()
        mass.config.get_protocol_record = MagicMock(side_effect=get_record)

        with patch.object(bridge_module, "SENDSPIN_LINK_WAIT_INTERVAL", 0):
            assert await manager._wait_for_protocol_link(CAST_ID, client_id) is False

        # one look for the link, then the next round sees the bridge is gone and stops
        assert polls == 1

    async def test_bridge_gone_after_the_wait_is_not_written_to(self) -> None:
        """Test a bridge that went away is not switched off, since its config may be gone."""
        manager, mass, cast_protocol, configs, _, _ = _make_opt_in_environment()
        client_id = manager._bridge_client_id(cast_protocol)
        assert client_id is not None
        _store_attached_client(configs, client_id)

        await manager._flag_bridge_experimental(CAST_ID, client_id, disable=True)

        mass.config.set_protocol_value.assert_not_called()
        mass.config.save_protocol_config.assert_not_awaited()


class TestCastFatalAudioError:
    """Tests for a Cast device that reports it cannot run the Sendspin receiver."""

    async def test_unsupported_device_is_not_offered_the_bridge_again(self) -> None:
        """Test the device is recorded as unsupported and its bridge re-evaluated away."""
        raw_values: dict[tuple[str, str], Any] = {}
        # the resolve and the re-evaluation share one log: the re-evaluation tears the
        # bridge down, which cancels the future the play command waits on
        order: list[str] = []

        mass = MagicMock()
        mass.config.set_protocol_value = MagicMock(
            side_effect=lambda protocol_id, key, value: raw_values.__setitem__(
                (protocol_id, key), value
            )
        )

        async def evaluate(_protocol: Any) -> None:
            order.append("evaluate")

        bridge = MagicMock(spec=bridge_module.SendspinChromecastBridge)
        bridge.mass = mass
        bridge.logger = logging.getLogger("test.cast_bridge")
        bridge._bridge_client_id = "spb_aabbccddeeff"
        bridge.cast_player = MagicMock()
        bridge.cast_player.protocol_id = CAST_ID
        bridge.cast_player.display_name = "Cast Speaker"
        bridge.provider = MagicMock()
        bridge.provider.bridge_manager.evaluate_bridge = AsyncMock(side_effect=evaluate)
        resolved: list[BaseException | None] = []

        def resolve(error: BaseException | None) -> None:
            resolved.append(error)
            order.append("resolve")

        bridge._resolve_cast_app_ready = MagicMock(side_effect=resolve)

        await bridge_module.SendspinChromecastBridge._handle_fatal_audio_error(bridge)

        # recorded on the Cast protocol: the bridge config goes away with the bridge
        assert raw_values[(CAST_ID, CONF_SENDSPIN_UNSUPPORTED)] is True
        assert isinstance(resolved[0], PlayerCommandFailed)
        assert order == ["resolve", "evaluate"]
