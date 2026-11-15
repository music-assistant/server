"""Tests for the Sendspin output of a device whose AirPlay output the user switched off."""

from __future__ import annotations

import asyncio
import logging
from collections import Counter
from collections.abc import AsyncGenerator
from types import MethodType
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiosendspin.models.core import ClientHelloPayload
from aiosendspin.models.core import DeviceInfo as SendspinDeviceInfo
from aiosendspin.models.player import ClientHelloPlayerSupport, SupportedAudioFormat
from aiosendspin.models.types import AudioCodec, PlayerCommand
from aiosendspin.noise.keys import Identity
from aiosendspin.noise.trust_store import InMemoryServerPairingStore
from aiosendspin.server import (
    ClientAddedEvent,
    ClientRemovedEvent,
    SendspinEvent,
    SendspinServer,
)
from music_assistant_models.player import OutputProtocol

from music_assistant.constants import CONF_PROTOCOL_EXPERIMENTAL_NOTE, CONF_UNDERLYING_PLAYER_ID
from music_assistant.controllers.config.players import PlayerConfigMixin
from music_assistant.providers.airplay.sendspin_bridge import (
    SendspinBridgeManager as AirPlaySendspinBridgeManager,
)
from music_assistant.providers.chromecast import sendspin_bridge as cast_bridge_module
from music_assistant.providers.chromecast.sendspin_bridge import (
    SendspinBridgeManager as CastSendspinBridgeManager,
)
from music_assistant.providers.chromecast.sendspin_bridge import SendspinChromecastBridge
from music_assistant.providers.sendspin.bridge_manager import evaluate_bridge_of
from music_assistant.providers.sendspin.bridge_role import BRIDGE_ROLE_ID
from music_assistant.providers.sendspin.helpers import holds_bridge_client
from music_assistant.providers.sendspin.player import SendspinPlayer
from music_assistant.providers.squeezelite.bridge_role import SLIMPROTO_BRIDGE_ROLE_ID
from music_assistant.providers.squeezelite.sendspin_bridge import (
    SendspinBridgeManager as SqueezeliteSendspinBridgeManager,
)
from music_assistant.providers.squeezelite.sendspin_bridge import SendspinSqueezeliteBridge
from tests.common import use_real_create_task

PLAYER_ID = "pl0123456789ab"
SQUEEZELITE_ID = "aa:bb:cc:dd:ee:ff"
SQUEEZELITE_CLIENT = "spb_aabbccddeeff"
# AirPlay reports the MAC address of the device with its locally administered bit set
AIRPLAY_ID = "apa8bbccddeeff"
AIRPLAY_CLIENT = "spb_a8bbccddeeff"
# an AirPlay protocol that reports the MAC address as it is, as squeezelite and Cast do
SAME_ID_AIRPLAY_ID = "apaabbccddeeff"
CAST_ID = "cc_player"
CAST_MAC = "AA:BB:CC:DD:EE:FF"
CAST_CLIENT = SQUEEZELITE_CLIENT

# the devices of a test, whose Sendspin servers are closed after it
_DEVICES: list[_Device] = []


@pytest.fixture(autouse=True)
async def _close_servers(monkeypatch: pytest.MonkeyPatch) -> AsyncGenerator[None]:
    """Close the Sendspin servers of the devices of a test, whose Cast bridge waits no time."""
    monkeypatch.setattr(cast_bridge_module, "SENDSPIN_LINK_WAIT_INTERVAL", 0)
    yield
    while _DEVICES:
        await _DEVICES.pop().server.close()


def _hello(client_id: str, role_id: str) -> ClientHelloPayload:
    """
    Return the hello a bridge registers its client with.

    :param client_id: The client id of the bridge.
    :param role_id: The player role of the bridge.
    """
    return ClientHelloPayload(
        client_id=client_id,
        name="Den",
        version=1,
        supported_roles=[role_id, "player@v1"],
        device_info=SendspinDeviceInfo(product_name="Den", manufacturer="Acme"),
        player_support=ClientHelloPlayerSupport(
            supported_formats=[
                SupportedAudioFormat(
                    codec=AudioCodec.PCM, channels=2, sample_rate=48000, bit_depth=16
                )
            ],
            buffer_capacity=1_000,
            supported_commands=[PlayerCommand.VOLUME],
        ),
    )


class _Player:
    """The player of the device: its outputs follow the stored and the attached protocols."""

    def __init__(self, device: _Device) -> None:
        """
        Initialize the player.

        :param device: The device the player belongs to.
        """
        self.player_id = PLAYER_ID
        self.attached_protocols: dict[str, Any] = {}
        self._device = device

    def get_output_protocol_by_domain(self, protocol_domain: str) -> OutputProtocol | None:
        """
        Return the output of a protocol domain, attached or not.

        :param protocol_domain: The domain of the protocol.
        """
        for protocol_id, record in self._device.records().items():
            if record.get("player_id") != PLAYER_ID:
                continue
            if record["provider"] == protocol_domain:
                return OutputProtocol(
                    output_protocol_id=protocol_id,
                    name=protocol_domain.title(),
                    protocol_domain=protocol_domain,
                    available=protocol_id in self.attached_protocols,
                )
        return None


class _Device:
    """
    A device with an AirPlay, a squeezelite and optionally a Cast protocol, with real managers.

    The bridges register with a real Sendspin server. The Sendspin provider and the players
    controller around it are stood in for, as they behave:
    - a client the server reports as added gets a Sendspin protocol that rides on the
      protocol its bridge registered for it, and takes a stored output of its id over;
    - a client the server reports as removed, or a Sendspin output the user switches off,
      is detached with the real on_detach of its Sendspin protocol;
    - a protocol that attaches, detaches or is removed, and a client that is added, let the
      managers hear that the outputs of the player changed; a client that leaves only does so
      while the protocol it rides on is attached;
    - a Sendspin protocol is only attached to the player while no other Sendspin output of
      the player is switched on.
    """

    def __init__(
        self,
        *,
        airplay_id: str = AIRPLAY_ID,
        cast: bool = False,
        squeezelite: bool = True,
        slow_teardown: bool = False,
        cast_connected: bool = False,
    ) -> None:
        """
        Set up the device with none of its protocols attached.

        :param airplay_id: The id of the AirPlay protocol.
        :param cast: Whether the device has a Cast protocol.
        :param squeezelite: Whether the device has a squeezelite protocol.
        :param slow_teardown: Let a bridge that stops take a moment before its client leaves,
            as one that streams, or waits for its lock or its Cast app launch, does.
        :param cast_connected: Let the Sendspin Cast receiver connect to the client of the
            Cast bridge, as it does while the Cast app runs.
        """
        _DEVICES.append(self)
        self._slow_teardown = slow_teardown
        self._cast_connected = cast_connected
        self.mass = mass = MagicMock()
        use_real_create_task(mass)
        mass.loop = asyncio.get_running_loop()
        mass.cache.get = AsyncMock(return_value=None)
        self.server = SendspinServer(
            mass.loop, Identity.generate(), "test", pairing_store=InMemoryServerPairingStore()
        )
        self.server.add_event_listener(self._on_sendspin_event)
        # client id -> the protocol its bridge registered it for, until its protocol is made
        self._underlying_ids: dict[str, str] = {}
        # client id -> the protocol the Sendspin protocol of the client rides on
        self.sendspin_protocols: dict[str, str] = {}
        sendspin = mass.get_provider.return_value
        sendspin.server_api = self.server
        sendspin.register_bridge_underlying_protocol = self._underlying_ids.__setitem__
        sendspin.discover_players = AsyncMock(side_effect=self._discover_players)
        self.configs: dict[str, Any] = {
            f"players/{PLAYER_ID}": {"enabled": True},
            **{
                f"providers/{domain}": {"enabled": True}
                for domain in ("airplay", "squeezelite", "chromecast", "sendspin")
            },
        }
        mass.config.get = MagicMock(
            side_effect=lambda key, default=None: self.configs.get(key, default)
        )
        mass.config.get_protocol_record = MagicMock(
            side_effect=lambda protocol_id: self.records().get(protocol_id)
        )
        mass.config.get_protocol_value = MagicMock(side_effect=self._get_value)
        mass.config.set_protocol_value = MagicMock(side_effect=self._set_value)
        mass.config.save_protocol_config = AsyncMock(side_effect=self._save_protocol_config)
        mass.config.is_protocol_switched_on = MethodType(
            PlayerConfigMixin.is_protocol_switched_on, mass.config
        )
        mass.players.wants_protocol = MagicMock(
            side_effect=lambda protocol_id, *_: (self.configs.get(protocol_id) or {}).get(
                "enabled", True
            )
        )
        mass.players.remove_protocol = AsyncMock(side_effect=self._remove_protocol)
        mass.players.unregister = AsyncMock(side_effect=self._unregister_sendspin)
        self.player = _Player(self)
        mass.players.get_protocol = MagicMock(side_effect=self._get_protocol)
        mass.players.get_player_id_of_protocol = MagicMock(return_value=PLAYER_ID)
        mass.players.get_player = MagicMock(return_value=self.player)
        self.managers: list[Any] = []
        self.bridges: dict[str, MagicMock] = {}
        # what the bridges did with the Sendspin server, in order
        self.timeline: list[tuple[str, str]] = []
        self.airplay = self._protocol("airplay", AirPlaySendspinBridgeManager, airplay_id)
        if squeezelite:
            self.squeezelite = self._protocol(
                "squeezelite", SqueezeliteSendspinBridgeManager, SQUEEZELITE_ID
            )
            self.squeezelite.client.device_type = "squeezeplay"
            self.squeezelite.client.device_model = "SqueezeLite"
        if cast:
            self.cast = self._protocol("chromecast", CastSendspinBridgeManager, CAST_ID)
            self.cast.cast_info.is_audio_group = False
            self.cast.cast_info.mac_address = CAST_MAC
            self.cast.device_info.manufacturer = "TestCo"
            self.cast.device_info.model = "TestSpeaker"

    def records(self) -> dict[str, dict[str, Any]]:
        """Return the stored protocols, by their id."""
        return {key: value for key, value in self.configs.items() if "/" not in key}

    def manager_of(self, protocol: Any) -> Any:
        """Return the bridge manager of a protocol."""
        return protocol.provider.bridge_manager

    def store_output(self, client_id: str, rides_on: str, **values: Any) -> None:
        """
        Store the Sendspin output of a bridge client, riding on a protocol.

        :param client_id: The client id of the bridge.
        :param rides_on: The id of the protocol the output rides on.
        :param values: Other values of the output, such as its settings.
        """
        self.configs[client_id] = {
            "enabled": True,
            "provider": "sendspin",
            "player_id": PLAYER_ID,
            "values": {CONF_UNDERLYING_PLAYER_ID: rides_on, **values},
        }

    async def attach(self, protocol: Any) -> None:
        """
        Attach a protocol to the player, then let its manager evaluate its bridge.

        In the order of the players controller: the hooks of the protocol evaluate its
        bridge (AirPlay awaits it, squeezelite starts it eagerly, Cast as a task that waits),
        then the changed outputs of the player are published, which the managers act on at
        once as Music Assistant starts its tasks eagerly, and then the AirPlay provider
        evaluates its bridge again once the registration returned.

        :param protocol: The protocol that attaches, as its provider registers it.
        """
        self.player.attached_protocols[protocol.protocol_id] = protocol
        protocol.player = self.player
        manager = self.manager_of(protocol)
        domain = protocol.provider.domain
        if domain == "airplay":
            await manager.evaluate_bridge(protocol)
        elif domain == "squeezelite":
            self.mass.create_task(evaluate_bridge_of(self.mass, protocol.protocol_id))
        else:
            self.mass.create_task(manager.evaluate_bridge(protocol), eager_start=False)
        self._outputs_changed()
        if domain == "airplay":
            await manager.evaluate_bridge(protocol)
        await self.settle()

    async def switch(self, protocol: Any, enabled: bool) -> None:
        """
        Let the user switch an output of the player on or off.

        An output switched off is detached; one switched on attaches once its provider
        finds the device again, which the test does with attach().

        :param protocol: The protocol of the output.
        :param enabled: Whether the output is switched on.
        """
        self.configs[protocol.protocol_id]["enabled"] = enabled
        if not enabled and protocol.protocol_id in self.player.attached_protocols:
            await self.detach(protocol)

    async def detach(self, protocol: Any) -> None:
        """
        Detach a protocol from the player, as a disconnect or switching it off does.

        :param protocol: The protocol that is detached.
        """
        del self.player.attached_protocols[protocol.protocol_id]
        protocol.player = None
        callbacks, protocol._on_detach_callbacks = protocol._on_detach_callbacks, []
        for callback in callbacks:
            callback()
        self._outputs_changed()
        await self.settle()

    def sendspin_output(self) -> str | None:
        """Return the client id of the Sendspin output attached to the player, if any."""
        for client_id in self.sendspin_protocols:
            if self.server.get_client(client_id) is None:
                continue
            if not self.mass.players.wants_protocol(client_id):
                continue
            others = [
                other_id
                for other_id, record in self.records().items()
                if record["provider"] == "sendspin"
                and other_id != client_id
                and record.get("enabled", True)
            ]
            if not others:
                return client_id
        return None

    def owner_of(self, client_id: str) -> str | None:
        """
        Return the protocol whose bridge holds the client of the server: its role and callback.

        :param client_id: The client id.
        """
        if (client := self.server.get_client(client_id)) is None:
            return None
        roles = client.roles_by_family("player")
        for protocol_id, bridge in self.bridges.items():
            if (
                bridge.client is client
                and bridge.role in roles
                and self.server._external_stream_start_cbs.get(client_id) is bridge.on_stream_start
            ):
                return protocol_id
        return None

    def rides_on(self, client_id: str) -> str | None:
        """Return the id of the protocol a stored Sendspin output rides on, if it is stored."""
        rides_on = self._get_value(client_id, CONF_UNDERLYING_PLAYER_ID)
        return rides_on if isinstance(rides_on, str) else None

    async def settle(self) -> None:
        """Let the evaluations the managers scheduled run."""
        for _ in range(50):
            await asyncio.sleep(0)

    def _protocol(self, domain: str, manager_class: type, protocol_id: str) -> MagicMock:
        """Create a protocol of the device, with the provider and real bridge manager of it."""
        provider = MagicMock(mass=self.mass, domain=domain, instance_id=domain)
        provider.logger = logging.getLogger(f"test.{domain}")
        protocol = MagicMock(protocol_id=protocol_id, provider=provider, player=None)
        protocol.display_name = f"Den ({domain})"
        protocol._on_detach_callbacks = []
        provider.protocols = [protocol]
        provider.get_protocol = lambda pid: (
            self.player.attached_protocols.get(pid) if pid == protocol_id else None
        )
        self.configs[protocol_id] = {"enabled": True, "provider": domain, "player_id": PLAYER_ID}
        manager = provider.bridge_manager = manager_class(provider)
        manager._create_bridge = MagicMock(side_effect=lambda _: self._bridge(protocol, domain))
        self.managers.append(manager)
        return protocol

    def _bridge(self, protocol: Any, domain: str) -> MagicMock:
        """
        Return a bridge that registers its client with the Sendspin server.

        It stops as the bridge of its provider does: AirPlay removes the client of its id,
        squeezelite and Cast only the client they still hold.
        """
        protocol_id = protocol.protocol_id
        bridge = MagicMock(is_registered=True, sendspin_server=self.server, shared_group=None)
        bridge.client = None

        async def _start() -> None:
            client_id = self.manager_of(protocol)._client_ids[protocol_id]
            self._underlying_ids[client_id] = protocol_id
            bridge.client = self.server.register_external_player(
                _hello(
                    client_id,
                    SLIMPROTO_BRIDGE_ROLE_ID if domain == "squeezelite" else BRIDGE_ROLE_ID,
                ),
                on_stream_start=bridge.on_stream_start,
            )
            (bridge.role,) = bridge.client.roles_by_family("player")
            if domain == "chromecast" and self._cast_connected:
                # the Sendspin Cast receiver holds a connection to the client
                bridge.client._connection = MagicMock()
                bridge.client._connected = True
            self.timeline.append(("registered", domain))

        async def _stop() -> None:
            if bridge.client is None:
                # its registration failed
                return
            if self._slow_teardown:
                # long enough for the evaluations that wait for this client to run first
                for _ in range(10):
                    await asyncio.sleep(0)
            self.timeline.append(("stopped", domain))
            if domain == "airplay" or holds_bridge_client(bridge.client, bridge.role):
                await self.server.remove_client(bridge.client.client_id)

        bridge.start = AsyncMock(side_effect=_start)
        bridge.stop = AsyncMock(side_effect=_stop)
        self.bridges[protocol_id] = bridge
        return bridge

    def _on_sendspin_event(self, _server: SendspinServer, event: SendspinEvent) -> None:
        """Handle a client the server added or removed, as the Sendspin provider does."""
        if isinstance(event, ClientAddedEvent):
            self.mass.create_task(self._client_added(event.client_id))
        elif isinstance(event, ClientRemovedEvent):
            self.mass.create_task(self._unregister_sendspin(event.client_id))

    async def _client_added(self, client_id: str) -> None:
        """Give a client of the server its Sendspin protocol, riding on its bridged protocol."""
        if self.server.get_client(client_id) is None:
            return
        rides_on = (
            self._underlying_ids.pop(client_id, None)
            or self.sendspin_protocols.get(client_id)
            or self.rides_on(client_id)
        )
        if rides_on is None or not self.mass.players.wants_protocol(client_id):
            return
        self.sendspin_protocols[client_id] = rides_on
        if client_id in self.configs:
            self.configs[client_id]["values"][CONF_UNDERLYING_PLAYER_ID] = rides_on
        else:
            self.store_output(client_id, rides_on)
        self._outputs_changed()

    async def _discover_players(self) -> None:
        """Give the clients of the server without a Sendspin protocol theirs."""
        for client in self.server.clients:
            client_id = client.client_id
            if (
                self.server.is_external_player(client_id)
                and client_id not in self.sendspin_protocols
            ):
                await self._client_added(client_id)

    def _get_protocol(self, protocol_id: str) -> Any:
        """Return an attached protocol: one of the device, or a Sendspin protocol."""
        if (rides_on := self.sendspin_protocols.get(protocol_id)) is not None:
            return MagicMock(underlying_protocol_id=rides_on)
        return self.player.attached_protocols.get(protocol_id)

    def _get_value(self, protocol_id: str, key: str, default: Any = None) -> Any:
        """Return a stored value of a protocol."""
        return (self.configs.get(protocol_id) or {}).get("values", {}).get(key, default)

    def _set_value(self, protocol_id: str, key: str, value: Any) -> None:
        """Store a value of a protocol."""
        self.configs.setdefault(protocol_id, {}).setdefault("values", {})[key] = value

    async def _save_protocol_config(self, protocol_id: str, values: dict[str, Any]) -> None:
        """Save values of a protocol: a Sendspin output switched off is detached."""
        self.configs[protocol_id].update(values)
        if values.get("enabled") is False:
            await self._unregister_sendspin(protocol_id)

    async def _unregister_sendspin(self, client_id: str) -> None:
        """Detach the Sendspin protocol of a client with its real on_detach."""
        if (rides_on := self.sendspin_protocols.pop(client_id, None)) is None:
            return
        protocol = SendspinPlayer.__new__(SendspinPlayer)
        protocol._protocol_id = client_id
        protocol._player = None
        protocol._attr_underlying_protocol_id = rides_on
        protocol._on_detach_callbacks = []
        protocol._stop_taking_volume_reports = None
        protocol.playback_session = MagicMock(close=AsyncMock())
        protocol.mass = self.mass
        with patch.object(protocol, "_unsubscribe_client_callbacks"):
            await protocol.on_detach()
        if rides_on in self.player.attached_protocols:
            # its output is unavailable already while the protocol it rides on is away
            self._outputs_changed()

    async def _remove_protocol(self, protocol_id: str) -> None:
        """Remove a protocol for good, which changes the outputs of the player."""
        await self._unregister_sendspin(protocol_id)
        self.configs.pop(protocol_id, None)
        self._outputs_changed()

    def _outputs_changed(self) -> None:
        """Let every manager hear that the outputs of the player changed."""
        for manager in self.managers:
            manager._on_player_state_updated(self.player, {"output_protocols": (None, None)})


async def _airplay_bridged(device: _Device, client_id: str = AIRPLAY_CLIENT) -> None:
    """Bring the device to AirPlay carrying its Sendspin output, with everything attached."""
    await device.attach(device.airplay)
    for name in ("squeezelite", "cast"):
        if protocol := getattr(device, name, None):
            await device.attach(protocol)
    assert device.sendspin_output() == client_id


def _assert_carried_by(device: _Device, client_id: str, protocol_id: str) -> None:
    """Assert the bridge of a protocol carries the Sendspin output of the device."""
    assert device.server.get_client(client_id) is not None, "Sendspin lost the client"
    assert device.owner_of(client_id) == protocol_id, "the client is held by another bridge"
    assert device.rides_on(client_id) == protocol_id, "the stored output rides on another protocol"
    assert device.sendspin_protocols.get(client_id) == protocol_id, (
        "the Sendspin protocol rides on another protocol"
    )
    assert device.sendspin_output() == client_id


@pytest.mark.parametrize("slow_teardown", [False, True])
async def test_switching_airplay_off_hands_the_output_to_squeezelite(slow_teardown: bool) -> None:
    """The squeezelite bridge takes the device once the user switched AirPlay off."""
    device = _Device(slow_teardown=slow_teardown)
    await _airplay_bridged(device)
    assert device.manager_of(device.squeezelite).get_bridge(SQUEEZELITE_ID) is None

    await device.switch(device.airplay, False)

    assert device.manager_of(device.airplay).get_bridge(AIRPLAY_ID) is None
    assert device.manager_of(device.squeezelite).get_bridge(SQUEEZELITE_ID) is not None
    # the output AirPlay left behind would keep the place of the one of squeezelite
    assert AIRPLAY_CLIENT not in device.configs
    assert device.sendspin_output() == SQUEEZELITE_CLIENT
    assert device.rides_on(SQUEEZELITE_CLIENT) == SQUEEZELITE_ID


async def test_switching_airplay_back_on_hands_the_output_back() -> None:
    """AirPlay takes the device back once it is switched on and found again."""
    device = _Device()
    await _airplay_bridged(device)
    await device.switch(device.airplay, False)
    assert device.sendspin_output() == SQUEEZELITE_CLIENT

    await device.switch(device.airplay, True)
    await device.attach(device.airplay)

    assert device.manager_of(device.squeezelite).get_bridge(SQUEEZELITE_ID) is None
    assert SQUEEZELITE_CLIENT not in device.configs
    assert device.manager_of(device.airplay).get_bridge(AIRPLAY_ID) is not None
    assert device.sendspin_output() == AIRPLAY_CLIENT
    assert device.rides_on(AIRPLAY_CLIENT) == AIRPLAY_ID


async def test_starting_up_with_airplay_switched_off_gives_squeezelite_the_device() -> None:
    """The squeezelite bridge takes the device that starts up with AirPlay switched off."""
    device = _Device()
    device.store_output(AIRPLAY_CLIENT, AIRPLAY_ID)
    device.configs[AIRPLAY_ID]["enabled"] = False

    await device.attach(device.squeezelite)

    assert device.manager_of(device.squeezelite).get_bridge(SQUEEZELITE_ID) is not None
    assert AIRPLAY_CLIENT not in device.configs
    assert device.sendspin_output() == SQUEEZELITE_CLIENT


async def test_disabling_the_airplay_provider_switches_airplay_off() -> None:
    """An AirPlay output whose provider the user disabled leaves the device to squeezelite."""
    device = _Device()
    device.store_output(AIRPLAY_CLIENT, AIRPLAY_ID)
    device.configs["providers/airplay"]["enabled"] = False

    await device.attach(device.squeezelite)

    assert device.sendspin_output() == SQUEEZELITE_CLIENT


async def test_an_airplay_output_that_is_away_keeps_the_device() -> None:
    """Nothing takes the device while its AirPlay output is switched on but does not answer."""
    device = _Device()
    await _airplay_bridged(device)
    output = device.configs[AIRPLAY_CLIENT]

    await device.detach(device.airplay)

    assert device.manager_of(device.squeezelite).get_bridge(SQUEEZELITE_ID) is None
    assert device.configs[AIRPLAY_CLIENT] is output
    assert device.rides_on(AIRPLAY_CLIENT) == AIRPLAY_ID


async def test_an_output_under_the_same_client_id_is_taken_over_with_its_settings() -> None:
    """Squeezelite keeps the output AirPlay left behind under the client id of both."""
    device = _Device(airplay_id="apaabbccddeeff")
    await device.attach(device.airplay)
    device.configs[SQUEEZELITE_CLIENT]["values"]["static_delay"] = 120
    output = device.configs[SQUEEZELITE_CLIENT]
    await device.attach(device.squeezelite)

    await device.switch(device.airplay, False)

    assert device.manager_of(device.squeezelite).get_bridge(SQUEEZELITE_ID) is not None
    assert device.configs[SQUEEZELITE_CLIENT] is output
    assert output["values"]["static_delay"] == 120
    assert device.rides_on(SQUEEZELITE_CLIENT) == SQUEEZELITE_ID
    assert device.sendspin_output() == SQUEEZELITE_CLIENT


@pytest.mark.parametrize("slow_teardown", [False, True])
async def test_switching_airplay_off_hands_the_output_to_cast(slow_teardown: bool) -> None:
    """The Cast bridge takes a device without squeezelite once AirPlay is switched off."""
    device = _Device(cast=True, squeezelite=False, slow_teardown=slow_teardown)
    await _airplay_bridged(device)
    assert device.manager_of(device.cast).get_bridge(CAST_ID) is None

    await device.switch(device.airplay, False)

    assert CAST_ID in device.bridges, "the Cast bridge was not set up"
    device.bridges[CAST_ID].start.assert_awaited_once()
    assert AIRPLAY_CLIENT not in device.configs
    assert device.rides_on(CAST_CLIENT) == CAST_ID
    assert device.configs[CAST_CLIENT]["values"][CONF_PROTOCOL_EXPERIMENTAL_NOTE]
    # Sendspin over Cast is opt-in for an output that Cast registers for the first time
    assert device.configs[CAST_CLIENT]["enabled"] is False
    assert device.manager_of(device.cast).get_bridge(CAST_ID) is None

    await device.switch(device.airplay, True)
    await device.attach(device.airplay)

    assert device.manager_of(device.cast).get_bridge(CAST_ID) is None
    assert device.sendspin_output() == AIRPLAY_CLIENT


async def test_squeezelite_goes_before_cast_when_airplay_is_switched_off() -> None:
    """The order between the bridges that take over stays: squeezelite before Cast."""
    device = _Device(cast=True)
    await _airplay_bridged(device)

    await device.switch(device.airplay, False)

    assert device.manager_of(device.cast).get_bridge(CAST_ID) is None
    assert device.manager_of(device.squeezelite).get_bridge(SQUEEZELITE_ID) is not None
    assert device.sendspin_output() == SQUEEZELITE_CLIENT


async def test_an_output_riding_on_a_protocol_that_is_away_keeps_its_place() -> None:
    """A bridge that registers leaves the output of a protocol that is switched on alone."""
    device = _Device(cast=True)
    device.cast.cast_info.mac_address = "A8:BB:CC:DD:EE:FF"
    device.configs[AIRPLAY_ID]["enabled"] = False
    # the output of the Cast protocol of the device, which is away
    device.store_output(AIRPLAY_CLIENT, CAST_ID)

    await device.attach(device.squeezelite)

    assert device.manager_of(device.squeezelite).get_bridge(SQUEEZELITE_ID) is not None
    assert device.rides_on(AIRPLAY_CLIENT) == CAST_ID


@pytest.mark.parametrize("taker", ["squeezelite", "chromecast"])
@pytest.mark.parametrize(
    "slow_teardown", [False, True], ids=["taker stops at once", "taker takes a moment"]
)
async def test_airplay_takes_back_a_client_of_the_same_id(taker: str, slow_teardown: bool) -> None:
    """AirPlay switched on again holds the client the taker registered under the same id."""
    device = _Device(
        airplay_id=SAME_ID_AIRPLAY_ID,
        cast=taker == "chromecast",
        squeezelite=taker == "squeezelite",
        slow_teardown=slow_teardown,
    )
    protocol = device.squeezelite if taker == "squeezelite" else device.cast
    await _airplay_bridged(device, SQUEEZELITE_CLIENT)
    await device.switch(device.airplay, False)
    _assert_carried_by(device, SQUEEZELITE_CLIENT, protocol.protocol_id)
    device.timeline.clear()

    await device.switch(device.airplay, True)
    await device.attach(device.airplay)

    # AirPlay registers from its attach, before the taker hears that the outputs changed
    assert device.timeline == [("registered", "airplay"), ("stopped", taker)]
    assert device.manager_of(protocol).get_bridge(protocol.protocol_id) is None
    _assert_carried_by(device, SQUEEZELITE_CLIENT, SAME_ID_AIRPLAY_ID)


async def test_squeezelite_and_cast_wait_for_each_others_client() -> None:
    """Squeezelite and Cast hand a client of the same id over in turn, never side by side."""
    device = _Device(cast=True)
    device.configs[AIRPLAY_ID]["enabled"] = False
    # the output the Cast bridge carried before, so it is no first-time opt-in
    device.store_output(CAST_CLIENT, CAST_ID)
    await device.attach(device.cast)
    _assert_carried_by(device, CAST_CLIENT, CAST_ID)
    device.timeline.clear()

    await device.attach(device.squeezelite)

    assert device.timeline == [("stopped", "chromecast"), ("registered", "squeezelite")]
    _assert_carried_by(device, SQUEEZELITE_CLIENT, SQUEEZELITE_ID)
    device.timeline.clear()

    await device.detach(device.squeezelite)

    assert device.timeline == [("stopped", "squeezelite"), ("registered", "chromecast")]
    _assert_carried_by(device, CAST_CLIENT, CAST_ID)


@pytest.mark.parametrize("kind", ["squeezelite", "chromecast"])
@pytest.mark.parametrize("taken_over", [True, False])
async def test_a_bridge_that_stops_leaves_a_client_another_bridge_took_over(
    kind: str, taken_over: bool
) -> None:
    """The real squeezelite and Cast bridges only remove the client they still hold."""
    device = _Device(cast=True)
    server = device.server
    provider = MagicMock(mass=device.mass, logger=logging.getLogger(f"test.{kind}"))
    bridge: Any
    if kind == "squeezelite":
        bridge = SendspinSqueezeliteBridge(provider, device.squeezelite, server)
        role_id = SLIMPROTO_BRIDGE_ROLE_ID
    else:
        bridge = SendspinChromecastBridge(provider, device.cast, server, CAST_CLIENT)
        role_id = BRIDGE_ROLE_ID
    bridge._sendspin_client = server.register_external_player(
        _hello(SQUEEZELITE_CLIENT, role_id), on_stream_start=MagicMock()
    )
    (bridge._bridge_role,) = bridge._sendspin_client.roles_by_family("player")
    bridge._bridge_client_id = SQUEEZELITE_CLIENT
    if taken_over:
        # the AirPlay bridge of the device registers the same client id
        server.register_external_player(
            _hello(SQUEEZELITE_CLIENT, BRIDGE_ROLE_ID), on_stream_start=MagicMock()
        )

    await bridge.stop()

    assert server.is_external_player(SQUEEZELITE_CLIENT) is taken_over, (
        "the bridge removed the client another bridge holds"
        if taken_over
        else "the bridge left its own client behind"
    )


async def test_airplay_gets_the_device_back_from_a_connected_cast_receiver() -> None:
    """AirPlay switched on again bridges the device once a connected Cast receiver let go."""
    device = _Device(
        airplay_id=SAME_ID_AIRPLAY_ID,
        cast=True,
        squeezelite=False,
        slow_teardown=True,
        cast_connected=True,
    )
    await _airplay_bridged(device, CAST_CLIENT)
    await device.switch(device.airplay, False)
    _assert_carried_by(device, CAST_CLIENT, CAST_ID)
    client = device.server.get_client(CAST_CLIENT)
    assert client is not None
    assert client.is_connected

    await device.switch(device.airplay, True)
    await device.attach(device.airplay)

    assert device.manager_of(device.cast).get_bridge(CAST_ID) is None
    _assert_carried_by(device, CAST_CLIENT, SAME_ID_AIRPLAY_ID)


async def test_stepping_back_lets_the_others_evaluate_once_when_none_can_take_over() -> None:
    """The evaluations a bridge that steps back starts end, also when no bridge can register."""
    device = _Device(cast=True)
    device.configs[AIRPLAY_ID]["enabled"] = False
    await device.attach(device.squeezelite)
    await device.attach(device.cast)
    _assert_carried_by(device, SQUEEZELITE_CLIENT, SQUEEZELITE_ID)
    # AirPlay can not register, so squeezelite and Cast are both denied and nobody takes over
    airplay_starts = AsyncMock(side_effect=RuntimeError("the client is connected"))
    device.manager_of(device.airplay)._create_bridge = MagicMock(
        side_effect=lambda _: MagicMock(start=airplay_starts, stop=AsyncMock())
    )
    evaluations: Counter[str] = Counter()
    for manager in device.managers:

        async def _counted(protocol: Any, evaluate: Any = manager.evaluate_bridge) -> None:
            evaluations[protocol.provider.domain] += 1
            await evaluate(protocol)

        manager.evaluate_bridge = _counted

    await device.switch(device.airplay, True)
    await device.attach(device.airplay)
    settled = dict(evaluations)
    for _ in range(20):
        await device.settle()

    assert dict(evaluations) == settled, "the evaluations do not come to an end"
    # its attach, the AirPlay provider and the step-back of squeezelite tried AirPlay
    assert airplay_starts.await_count == 3, "the step-back of squeezelite did not let AirPlay try"
    assert device.manager_of(device.squeezelite).get_bridge(SQUEEZELITE_ID) is None
    assert device.sendspin_output() is None
