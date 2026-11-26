"""Tests for the Chromecast Sendspin bridge manager: its policy, opt-in and fatal errors."""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncGenerator
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiosendspin.models.types import GoodbyeReason
from aiosendspin.server import ClientRemovedEvent
from music_assistant_models.errors import PlayerCommandFailed

from music_assistant.constants import (
    CONF_PROTOCOL_EXPERIMENTAL_NOTE,
    CONF_PROTOCOL_OFF_BY_DEFAULT,
    CONF_UNDERLYING_PLAYER_ID,
)
from music_assistant.controllers.config.migrations import _migrate_sendspin_one_output
from music_assistant.providers.chromecast import sendspin_bridge as bridge_module
from music_assistant.providers.chromecast.constants import (
    CONF_SENDSPIN_UNSUPPORTED,
    SENDSPIN_CAST_APP_ID,
    SENDSPIN_CAST_EXPERIMENTAL_NOTE,
)
from music_assistant.providers.chromecast.sendspin_bridge import (
    SendspinBridgeManager as CastSendspinBridgeManager,
)
from music_assistant.providers.sendspin import constants as sendspin_constants
from music_assistant.providers.sendspin.constants import CONF_SENDSPIN_CAST_OPT_IN
from music_assistant.providers.squeezelite.sendspin_bridge import (
    SendspinBridgeManager as SqueezeliteSendspinBridgeManager,
)
from tests.common import scheduled_call, use_real_bridge_holders
from tests.providers.sendspin.bridge_harness import CAST_CLIENT, BridgedDevice
from tests.providers.sendspin.bridge_harness import CAST_ID as DEVICE_CAST_ID

CAST_ID = "cc_player"
# the player of the Cast protocol has an id of its own
PLAYER_ID = "pl0123456789ab"
# a squeezelite player on the same device, with the MAC address of the Cast device as id
SQUEEZELITE_ID = "aa:bb:cc:dd:ee:ff"


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
    # nothing is stored: no protocol, and no Sendspin output of the player
    mass.config.get_protocol_record = MagicMock(return_value=None)
    mass.config.get_protocol_ids_of_player = MagicMock(return_value=[])
    use_real_bridge_holders(mass.get_provider.return_value)
    external_clients: set[str] = set()
    sendspin_server = mass.get_provider.return_value.server_api
    sendspin_server.is_external_player = MagicMock(side_effect=external_clients.__contains__)
    mass.external_clients = external_clients
    provider = MagicMock(domain="chromecast")
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
    cast_protocol.player = MagicMock(
        player_id=PLAYER_ID, attached_protocols={CAST_ID: cast_protocol}
    )
    cast_protocol.provider = provider
    cast_protocol._on_detach_callbacks = []
    provider.protocols = [cast_protocol]
    provider.get_protocol = lambda protocol_id: cast_protocol if protocol_id == CAST_ID else None
    mass.players.get_protocol = MagicMock(side_effect=provider.get_protocol)

    manager = provider.bridge_manager = CastSendspinBridgeManager(provider)
    return manager, mass, cast_protocol


class TestCastBridgePolicy:
    """Tests for the Chromecast-specific bridge policy."""

    def test_a_cast_device_qualifies(self) -> None:
        """Test a Cast device that is not blocked qualifies for a bridge."""
        manager, _, cast_protocol = _make_cast_environment()

        assert manager.qualifies(cast_protocol) is True

    def test_another_bridge_of_the_device_does_not_change_the_policy(self) -> None:
        """Test the policy is about the Cast device only: the order decides between bridges."""
        manager, mass, cast_protocol = _make_cast_environment()
        # the AirPlay bridge of the device registered under the other form of its MAC address
        mass.external_clients.add("spb_a8bbccddeeff")
        _attach_squeezelite(mass, cast_protocol)

        assert manager.qualifies(cast_protocol) is True

    async def test_the_client_of_another_bridge_is_not_claimed_as_a_receiver(self) -> None:
        """Test a client a bridge registered under the id is registered again, not claimed."""
        manager, mass, cast_protocol = _make_cast_environment()
        client_id = manager.bridge_client_id(cast_protocol)
        assert client_id is not None
        mass.external_clients.add(client_id)
        mass.get_provider.return_value.apply_bridge_claim = AsyncMock(return_value=True)
        bridge = MagicMock(start=AsyncMock(), is_registered=True)

        with patch.object(manager, "_create_bridge", return_value=bridge):
            await manager.setup_bridge(cast_protocol)

        assert not manager._claimed_clients, "the client of another bridge was claimed"
        assert manager.get_bridge(CAST_ID) is bridge

    async def test_self_connected_receiver_is_claimed(self) -> None:
        """Test a Cast receiver that reconnected on its own is adopted, riding on the protocol."""
        manager, mass, cast_protocol = _make_cast_environment()
        client_id = manager.bridge_client_id(cast_protocol)
        assert client_id is not None
        sendspin = mass.get_provider.return_value
        sendspin.apply_bridge_claim = AsyncMock(return_value=True)

        assert await manager._try_claim_existing(cast_protocol) is True
        assert manager._claimed_clients == {CAST_ID: client_id}
        assert sendspin.apply_bridge_claim.call_args.kwargs["underlying_protocol_id"] == CAST_ID

    async def test_a_claimed_receiver_that_leaves_is_bridged_by_an_evaluation(self) -> None:
        """Test the bridge after a claimed receiver goes is decided with the rest of the player."""
        manager, mass, cast_protocol = _make_cast_environment()
        sendspin = mass.get_provider.return_value
        sendspin.apply_bridge_claim = AsyncMock(return_value=True)
        assert await manager._try_claim_existing(cast_protocol) is True
        client_id = manager._claimed_clients[CAST_ID]
        (listener,) = (
            call.args[0] for call in sendspin.server_api.add_event_listener.call_args_list
        )

        listener(sendspin.server_api, ClientRemovedEvent(client_id))

        mass.create_task.assert_called_once()
        name, arguments = scheduled_call(mass.create_task.call_args.args[0])
        assert name.endswith(".evaluate_bridge"), "the bridge is set up outside the player lock"
        assert arguments["protocol"] is cast_protocol

    async def test_a_claimed_receiver_whose_app_closes_is_detached(self) -> None:
        """Test a receiver claimed before its Cast status came in is let go once its app closes."""
        manager, mass, cast_protocol = _make_cast_environment()
        mass.loop.call_soon_threadsafe = MagicMock(side_effect=lambda call, *args: call(*args))
        mass.create_task = MagicMock(side_effect=lambda coro: coro.close())
        sendspin = mass.get_provider.return_value
        sendspin.apply_bridge_claim = AsyncMock(return_value=True)
        client = sendspin.server_api.get_client.return_value
        cast_protocol.cc.app_id = None
        assert await manager._try_claim_existing(cast_protocol) is True
        # claimed while connecting to the device, so still without a status
        cast_protocol.on_app_status_changed(None)
        client.detach_connection.assert_not_called()

        cast_protocol.on_app_status_changed(SENDSPIN_CAST_APP_ID)
        client.detach_connection.assert_not_called()
        cast_protocol.on_app_status_changed(None)

        client.detach_connection.assert_called_once_with(GoodbyeReason.SHUTDOWN)

    async def test_a_claimed_client_goes_with_its_protocol_for_good(self) -> None:
        """Test a permanent removal takes the claimed client along when it rides on the protocol."""
        manager, mass, cast_protocol = _make_cast_environment()
        client_id = manager.bridge_client_id(cast_protocol)
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
        assert manager.qualifies(cast_protocol) is True

        mass.config.get_protocol_value = MagicMock(
            side_effect=lambda protocol_id, key, default=None: (
                True if protocol_id == CAST_ID and key == CONF_SENDSPIN_UNSUPPORTED else default
            )
        )

        assert manager.qualifies(cast_protocol) is False

    def test_unsupported_skip_is_logged_once_at_info(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Test a device flagged unsupported logs why its bridge is skipped, once."""
        manager, mass, cast_protocol = _make_cast_environment()
        mass.config.get_protocol_value = MagicMock(
            side_effect=lambda protocol_id, key, default=None: (
                True if protocol_id == CAST_ID and key == CONF_SENDSPIN_UNSUPPORTED else default
            )
        )

        with caplog.at_level(logging.INFO, logger=manager.logger.name):
            assert manager.qualifies(cast_protocol) is False
            assert manager.qualifies(cast_protocol) is False

        records = [r for r in caplog.records if "cannot run Sendspin" in r.message]
        assert len(records) == 1
        assert records[0].levelno == logging.INFO

    def test_blocklist_message_only_repeats_after_policy_change(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Test repeated blocklist checks only log when the policy result changes."""
        manager, _, cast_protocol = _make_cast_environment()
        cast_protocol.device_info.manufacturer = "Harman Luxury Audio"

        with caplog.at_level(logging.DEBUG, logger=manager.logger.name):
            assert manager.qualifies(cast_protocol) is False
            assert manager.qualifies(cast_protocol) is False
            cast_protocol.device_info.manufacturer = "TestCo"
            assert manager.qualifies(cast_protocol) is True
            cast_protocol.device_info.manufacturer = "Harman Luxury Audio"
            assert manager.qualifies(cast_protocol) is False

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


def _attach_squeezelite(
    mass: MagicMock,
    cast_protocol: MagicMock,
    *,
    protocol_id: str = SQUEEZELITE_ID,
    device_type: str = "squeezeplay",
    device_model: str = "SqueezeLite",
) -> MagicMock:
    """
    Attach a squeezelite protocol, with the bridge manager of its provider, to the Cast player.

    :param mass: The mocked Music Assistant of the Cast environment.
    :param cast_protocol: The Cast protocol whose player gets the squeezelite protocol.
    :param protocol_id: The id of the squeezelite player.
    :param device_type: The device type the squeezelite player reports.
    :param device_model: The device model the squeezelite player reports.
    :return: The squeezelite protocol.
    """
    # the squeezelite manager looks up what it learned about a player before it bridges it
    mass.cache.get = AsyncMock(return_value=None)
    provider = MagicMock(mass=mass, domain="squeezelite")
    provider.logger = logging.getLogger("test.squeezelite_bridge_manager")
    manager = provider.bridge_manager = SqueezeliteSendspinBridgeManager(provider)
    protocol = MagicMock(protocol_id=protocol_id, provider=provider, player=cast_protocol.player)
    protocol.client.device_type = device_type
    protocol.client.device_model = device_model
    protocol._on_detach_callbacks = []
    provider.get_protocol = lambda _protocol_id: protocol
    manager._create_bridge = MagicMock(  # type: ignore[method-assign]
        return_value=MagicMock(start=AsyncMock(), stop=AsyncMock(), is_registered=True)
    )
    cast_protocol.player.attached_protocols = {CAST_ID: cast_protocol, protocol_id: protocol}
    return protocol


class TestCastBridgeYieldsToSqueezelite:
    """Tests for the Cast bridge leaving the output to the squeezelite bridge of the device."""

    async def test_no_cast_bridge_for_a_device_with_a_squeezelite_player_that_qualifies(
        self,
    ) -> None:
        """Test a Cast protocol that shows up after the squeezelite player gets no bridge."""
        manager, mass, cast_protocol = _make_cast_environment()
        # no receiver connected on its own, which would be claimed instead
        mass.get_provider.return_value.server_api.get_client.return_value = None
        squeezelite = _attach_squeezelite(mass, cast_protocol)

        with patch.object(manager, "_create_bridge", return_value=AsyncMock()) as create_bridge:
            await manager.evaluate_bridge(cast_protocol)

        create_bridge.assert_not_called()
        assert not manager._has_bridge(CAST_ID)
        assert squeezelite.provider.bridge_manager.get_bridge(SQUEEZELITE_ID) is not None

    async def test_the_cast_bridge_goes_when_a_squeezelite_player_shows_up(self) -> None:
        """Test a Cast bridge that came first hands its output over, with its config."""
        manager, mass, cast_protocol = _make_cast_environment()
        client_id = manager.bridge_client_id(cast_protocol)
        assert client_id is not None
        sendspin_server = mass.get_provider.return_value.server_api
        cast_bridge = MagicMock(stop=AsyncMock(), sendspin_server=sendspin_server)
        manager._bridges[CAST_ID] = cast_bridge
        manager._client_ids[CAST_ID] = client_id
        mass.players.remove_protocol = AsyncMock()

        squeezelite = _attach_squeezelite(mass, cast_protocol)
        await manager.evaluate_bridge(cast_protocol)

        assert not manager._has_bridge(CAST_ID)
        cast_bridge.stop.assert_awaited_once()
        mass.players.remove_protocol.assert_not_awaited()
        assert squeezelite.provider.bridge_manager.bridge_client_id(squeezelite) == client_id

    async def test_a_claimed_client_goes_with_its_bridge(self) -> None:
        """Test the client of a claimed receiver is removed with the bridge that claimed it."""
        manager, mass, cast_protocol = _make_cast_environment()
        client_id = manager.bridge_client_id(cast_protocol)
        assert client_id is not None
        manager._client_ids[CAST_ID] = client_id
        # the Sendspin Cast receiver connected on its own and was claimed
        manager._claimed_clients[CAST_ID] = client_id
        mass.get_provider.return_value.hold_bridge_client(client_id, CAST_ID)
        sendspin_server = mass.get_provider.return_value.server_api
        sendspin_server.remove_client = AsyncMock()

        await manager.remove_bridge(CAST_ID)

        sendspin_server.remove_client.assert_awaited_once_with(client_id)
        assert not manager._has_bridge(CAST_ID)

    async def test_a_claimed_client_another_bridge_registered_since_is_left_alone(self) -> None:
        """Test the Cast manager leaves a client the AirPlay bridge registered after its claim."""
        manager, mass, cast_protocol = _make_cast_environment()
        client_id = manager.bridge_client_id(cast_protocol)
        assert client_id is not None
        manager._client_ids[CAST_ID] = client_id
        manager._claimed_clients[CAST_ID] = client_id
        sendspin = mass.get_provider.return_value
        sendspin.hold_bridge_client(client_id, CAST_ID)
        # the receiver left, and the AirPlay bridge of the device registered the client id
        sendspin.hold_bridge_client(client_id, "apaabbccddeeff")
        sendspin.server_api.remove_client = AsyncMock()

        await manager.remove_bridge(CAST_ID)

        sendspin.server_api.remove_client.assert_not_awaited()
        assert sendspin.get_bridge_holder(client_id) == "apaabbccddeeff"

    @pytest.mark.parametrize(
        "squeezelite",
        [
            pytest.param({"device_model": "CastBridge"}, id="bridge tool"),
            pytest.param({"device_type": "squeezebox"}, id="no timed start"),
            pytest.param({"protocol_id": "kitchen"}, id="no mac address"),
        ],
    )
    async def test_a_squeezelite_player_without_a_bridge_of_its_own_leaves_the_cast_bridge(
        self, squeezelite: dict[str, str]
    ) -> None:
        """Test only a squeezelite player that qualifies for a bridge takes the output."""
        manager, mass, cast_protocol = _make_cast_environment()
        mass.get_provider.return_value.server_api.get_client.return_value = None
        _attach_squeezelite(mass, cast_protocol, **squeezelite)
        bridge = MagicMock(start=AsyncMock(), is_registered=True)

        with patch.object(manager, "_create_bridge", return_value=bridge):
            await manager.evaluate_bridge(cast_protocol)

        assert manager.get_bridge(CAST_ID) is bridge


@pytest.fixture
async def device() -> AsyncGenerator[BridgedDevice]:
    """Yield a device that only has a Cast protocol, which is not attached yet."""
    cast_device = BridgedDevice(airplay=False, squeezelite=False, cast=True)
    yield cast_device
    await cast_device.close()


class TestCastBridgeOptIn:
    """Tests for the experimental opt-in of the Sendspin Cast bridge."""

    async def test_new_device_is_flagged_and_left_switched_off(self, device: BridgedDevice) -> None:
        """Test a device that never had the bridge keeps it off for the user to opt in."""
        await device.attach(device.cast)

        assert device.state() == (None, CAST_CLIENT, "chromecast", "off-default")
        values = device.output()["values"]
        assert values[CONF_PROTOCOL_EXPERIMENTAL_NOTE] == SENDSPIN_CAST_EXPERIMENTAL_NOTE
        assert CONF_SENDSPIN_CAST_OPT_IN not in values
        # registered once, for the output to be stored with its player
        assert device.timeline == [("registered", "chromecast"), ("stopped", "chromecast")]

    async def test_switching_the_output_on_is_remembered_as_the_opt_in(
        self, device: BridgedDevice
    ) -> None:
        """Test the user switching the output on gets the bridge, from then on."""
        await device.attach(device.cast)

        await device.switch_output(True)

        assert device.state() == ("chromecast", CAST_CLIENT, "chromecast", "on")
        values = device.output()["values"]
        assert values[CONF_SENDSPIN_CAST_OPT_IN] is True
        assert CONF_PROTOCOL_OFF_BY_DEFAULT not in values

        await device.detach(device.cast)
        await device.attach(device.cast)

        assert device.state() == ("chromecast", CAST_CLIENT, "chromecast", "on")

    async def test_an_output_the_user_switched_off_again_stays_off(
        self, device: BridgedDevice
    ) -> None:
        """Test the user switching the output off again is no default of the bridge."""
        await device.attach(device.cast)
        await device.switch_output(True)

        await device.switch_output(False)
        await device.detach(device.cast)
        await device.attach(device.cast)

        assert device.state() == (None, CAST_CLIENT, "chromecast", "off-user")

    async def test_device_that_already_had_the_bridge_keeps_it_on(
        self, device: BridgedDevice
    ) -> None:
        """Test an output switched on before the warning existed is flagged, not switched off."""
        device.store_output(CAST_CLIENT, DEVICE_CAST_ID, values={CONF_SENDSPIN_CAST_OPT_IN: True})

        await device.attach(device.cast)

        assert device.state() == ("chromecast", CAST_CLIENT, "chromecast", "on")
        assert (
            device.output()["values"][CONF_PROTOCOL_EXPERIMENTAL_NOTE]
            == SENDSPIN_CAST_EXPERIMENTAL_NOTE
        )

    async def test_already_flagged_device_is_left_alone(self, device: BridgedDevice) -> None:
        """Test a device that carries the warning already is not written to again."""
        device.store_output(
            CAST_CLIENT,
            DEVICE_CAST_ID,
            enabled=False,
            values={CONF_PROTOCOL_EXPERIMENTAL_NOTE: SENDSPIN_CAST_EXPERIMENTAL_NOTE},
        )
        output = device.output()
        stored = json.dumps(output, sort_keys=True)

        await device.attach(device.cast)

        assert device.output() is output
        assert json.dumps(output, sort_keys=True) == stored
        assert device.state() == (None, CAST_CLIENT, "chromecast", "off-user")

    def test_the_warning_text_is_authored(self) -> None:
        """Test the note the output renders resolves to a string that exists."""
        assert CastSendspinBridgeManager.experimental_note == SENDSPIN_CAST_EXPERIMENTAL_NOTE
        # the note is rendered as an alert, so guard the authored string against drift
        strings = json.loads(
            (Path(sendspin_constants.__file__).resolve().parent / "strings.json").read_text(
                encoding="utf-8"
            )
        )
        assert SENDSPIN_CAST_EXPERIMENTAL_NOTE in strings["config_entries"]

    async def test_an_output_that_is_stored_for_no_player_yet_is_not_switched_off(
        self, device: BridgedDevice
    ) -> None:
        """Test the bridge stays as-is while there is no toggle to opt back in with."""
        # registered by the bridge, but not stored as an output of its player yet
        with patch.object(device, "_client_added", AsyncMock()):
            await device.attach(device.cast)

        assert device.manager_of(device.cast).get_bridge(DEVICE_CAST_ID) is not None
        assert not device.outputs()

    async def test_a_missed_opt_out_is_retried_on_the_next_evaluation(
        self, device: BridgedDevice
    ) -> None:
        """Test an output that was stored but never switched off is switched off later."""
        # what a previous run left behind: stored switched on, without the warning
        device.store_output(CAST_CLIENT, DEVICE_CAST_ID)

        await device.attach(device.cast)

        assert device.state() == (None, CAST_CLIENT, "chromecast", "off-default")
        assert device.timeline == []

    async def test_an_opt_out_an_older_version_did_not_finish_is_finished_after_the_upgrade(
        self, device: BridgedDevice
    ) -> None:
        """Test an output switched on, that the bridge of an older version still had to switch off."""
        # what an older version left behind: the Cast protocol still marked to switch it off
        device.records()[DEVICE_CAST_ID]["values"]["sendspin_opt_out_pending"] = True
        device.store_output(CAST_CLIENT, DEVICE_CAST_ID)

        _migrate_sendspin_one_output(device.config.data)
        await device.attach(device.cast)

        assert device.state() == (None, CAST_CLIENT, "chromecast", "off-default")
        assert CONF_SENDSPIN_CAST_OPT_IN not in device.output()["values"]
        assert device.timeline == []


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
