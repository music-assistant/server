"""A device with several bridgeable protocols, for tests of the bridge that carries its output."""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

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

from music_assistant.constants import (
    CONF_PLAYER_PROTOCOLS,
    CONF_PLAYERS,
    CONF_PROTOCOL_OFF_BY_DEFAULT,
    CONF_PROVIDERS,
    CONF_UNDERLYING_PLAYER_ID,
)
from music_assistant.controllers.config.players import PlayerConfigMixin
from music_assistant.providers.airplay.sendspin_bridge import (
    SendspinBridgeManager as AirPlaySendspinBridgeManager,
)
from music_assistant.providers.chromecast.sendspin_bridge import (
    SendspinBridgeManager as CastSendspinBridgeManager,
)
from music_assistant.providers.sendspin.bridge_manager import (
    evaluate_bridge_of,
    evaluate_bridges_of_player,
    hand_static_delay_over,
)
from music_assistant.providers.sendspin.bridge_role import BRIDGE_ROLE_ID
from music_assistant.providers.sendspin.helpers import holds_bridge_client
from music_assistant.providers.sendspin.player import SendspinPlayer
from music_assistant.providers.squeezelite.bridge_role import SLIMPROTO_BRIDGE_ROLE_ID
from music_assistant.providers.squeezelite.sendspin_bridge import (
    SendspinBridgeManager as SqueezeliteSendspinBridgeManager,
)
from tests.common import use_real_bridge_holders, use_real_create_task

PLAYER_ID = "pl0123456789ab"
# the player a protocol of the device is on until it is linked to the player of the device
OTHER_PLAYER_ID = "pl00000000000a"
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
# an AirPlay protocol on another interface of the device, with an unrelated MAC address
OTHER_INTERFACE_AIRPLAY_ID = "ap001122334455"
OTHER_INTERFACE_AIRPLAY_CLIENT = "spb_001122334455"

DOMAINS = ("airplay", "squeezelite", "chromecast")


def hello(client_id: str, role_id: str) -> ClientHelloPayload:
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


def connect_cast_receiver(client: Any, *, state_received: bool = True) -> None:
    """
    Connect the Sendspin Cast receiver app to a client, as the server attaches its connection.

    The receiver negotiates its own player role, which takes the place of the bridge role.

    :param client: The client of the Cast bridge.
    :param state_received: Whether the receiver sent its initial state already, which is
        when the server calls the client connected.
    """
    client.attach_connection(
        MagicMock(disconnect=AsyncMock()),
        client_info=hello(client.client_id, "player@v1"),
        negotiated_roles=["player@v1"],
        active_roles=["player@v1"],
    )
    if state_received:
        client.mark_connected()


class _Config(PlayerConfigMixin):
    """The stored settings, with the real config code of players and protocols on top."""

    def __init__(self, device: BridgedDevice) -> None:
        """
        Initialize the settings of a device.

        :param device: The device, which stands in for the players controller.
        """
        self.data: dict[str, Any] = {
            CONF_PLAYERS: {},
            CONF_PLAYER_PROTOCOLS: {},
            CONF_PROVIDERS: {},
        }
        # the config code sees every protocol as away: it has no entries to validate against
        self.mass = SimpleNamespace(  # type: ignore[assignment]
            players=SimpleNamespace(
                get_protocol=lambda _protocol_id: None,
                on_protocol_config_change=device.on_protocol_config_change,
            )
        )

    def get(self, key: str, default: Any = None) -> Any:
        """Return the value stored under a path, as the config controller does."""
        parent: Any = self.data
        for subkey in key.split("/"):
            if not isinstance(parent, dict) or subkey not in parent:
                return default
            parent = parent[subkey]
        return default if parent is None else parent

    def set(self, key: str, value: Any, immediate: bool = False) -> None:
        """Store a value under a path, as the config controller does."""
        *parents, last = key.split("/")
        parent = self.data
        for subkey in parents:
            parent = parent.setdefault(subkey, {})
        parent[last] = value

    def remove(self, key: str) -> None:
        """Remove what is stored under a path, as the config controller does."""
        *parents, last = key.split("/")
        parent = self.data
        for subkey in parents:
            if subkey not in parent:
                return
            parent = parent[subkey]
        parent.pop(last, None)


class _Player:
    """A player of the device, with the protocols attached to it."""

    def __init__(self, player_id: str) -> None:
        """
        Initialize the player.

        :param player_id: The id of the player.
        """
        self.player_id = player_id
        self.attached_protocols: dict[str, Any] = {}
        self.update_state = MagicMock()


class BridgedDevice:
    """
    A device with AirPlay, squeezelite and Cast protocols as asked, with real managers.

    The bridges register with a real Sendspin server, and the settings are stored and
    switched with the real config code. The Sendspin provider and the players controller
    around it are stood in for, as they behave:
    - a client the server reports as added gets a Sendspin protocol that rides on the
      protocol its bridge registered for it, and is stored as an output of that player;
    - a client the server reports as removed, or a Sendspin output that is switched off,
      is detached with the real on_detach of its Sendspin protocol;
    - a protocol that attaches, detaches or is removed, and a client that is added, let the
      managers hear that the outputs of the player changed; a client that leaves only does so
      while the protocol it rides on is attached;
    - a saved config of the player evaluates its bridges, as the Sendspin provider does;
    - a Sendspin protocol is only attached to the player while no other Sendspin output of
      the player is switched on.
    A protocol is attached to the player of the device, or to a player of its own until it is
    linked to it.
    """

    def __init__(
        self,
        *,
        airplay_id: str = AIRPLAY_ID,
        airplay: bool = True,
        cast: bool = False,
        squeezelite: bool = True,
        slow_teardown: bool = False,
        cast_connected: bool = False,
        cast_state_received: bool = True,
    ) -> None:
        """
        Set up the device with none of its protocols attached.

        :param airplay_id: The id of the AirPlay protocol.
        :param airplay: Whether the device has an AirPlay protocol.
        :param cast: Whether the device has a Cast protocol.
        :param squeezelite: Whether the device has a squeezelite protocol.
        :param slow_teardown: Let a bridge that stops take a moment before its client leaves,
            as one that streams, or waits for its lock or its Cast app launch, does.
        :param cast_connected: Let the Sendspin Cast receiver connect to the client of the
            Cast bridge, as it does while the Cast app runs.
        :param cast_state_received: Whether that receiver sent its initial state already.
        """
        self._slow_teardown = slow_teardown
        self._cast_connected = cast_connected
        self._cast_state_received = cast_state_received
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
        sendspin = MagicMock()
        sendspin.mass = mass
        sendspin.server_api = self.server
        sendspin.instance_id = "sendspin"
        sendspin.register_bridge_underlying_protocol = self._underlying_ids.__setitem__
        use_real_bridge_holders(sendspin)
        sendspin.refresh_bridge_client = MagicMock(
            side_effect=lambda client_id: mass.create_task(self._client_added(client_id))
        )
        # the loaded providers, by domain
        self.providers: dict[str, Any] = {"sendspin": sendspin}
        mass.get_provider = MagicMock(
            side_effect=lambda domain, *_, **__: self.providers.get(domain)
        )
        self.config = mass.config = _Config(self)
        for player_id in (PLAYER_ID, OTHER_PLAYER_ID):
            self.config.set(f"{CONF_PLAYERS}/{player_id}", {"enabled": True, "values": {}})
        for domain in (*DOMAINS, "sendspin"):
            self.config.set(f"{CONF_PROVIDERS}/{domain}", {"domain": domain, "enabled": True})
        self.player = _Player(PLAYER_ID)
        self.other_player = _Player(OTHER_PLAYER_ID)
        players = {PLAYER_ID: self.player, OTHER_PLAYER_ID: self.other_player}
        mass.players.wants_protocol = MagicMock(
            side_effect=lambda protocol_id, *_: (self.records().get(protocol_id) or {}).get(
                "enabled", True
            )
        )
        mass.players.remove_protocol = AsyncMock(side_effect=self._remove_protocol)
        mass.players.unregister = AsyncMock(side_effect=self._unregister_sendspin)
        mass.players.get_protocol = MagicMock(side_effect=self._get_protocol)
        mass.players.get_player_id_of_protocol = MagicMock(
            side_effect=lambda protocol_id: (self.records().get(protocol_id) or {}).get("player_id")
        )
        mass.players.get_player = MagicMock(side_effect=players.get)
        self.managers: list[Any] = []
        self.bridges: dict[str, MagicMock] = {}
        # every provider of the device, loaded or not, and its protocol, by domain
        self._all_providers: dict[str, Any] = {}
        self.protocols: dict[str, Any] = {}
        # what the bridges did with the Sendspin server, in order
        self.timeline: list[tuple[str, str]] = []
        if airplay:
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
        records: dict[str, dict[str, Any]] = self.config.data[CONF_PLAYER_PROTOCOLS]
        return records

    def outputs(self) -> dict[str, dict[str, Any]]:
        """Return the stored Sendspin outputs, by their client id."""
        return {
            protocol_id: record
            for protocol_id, record in self.records().items()
            if record["provider"] == "sendspin"
        }

    def output(self) -> dict[str, Any]:
        """Return the one Sendspin output that is stored."""
        (record,) = self.outputs().values()
        return record

    def manager_of(self, protocol: Any) -> Any:
        """Return the bridge manager of a protocol."""
        return protocol.provider.bridge_manager

    def store_output(
        self,
        client_id: str,
        rides_on: str,
        *,
        enabled: bool = True,
        values: dict[str, Any] | None = None,
    ) -> None:
        """
        Store the Sendspin output of a bridge client, riding on a protocol.

        :param client_id: The client id of the bridge.
        :param rides_on: The id of the protocol the output rides on.
        :param enabled: Whether the output is switched on.
        :param values: Other values of the output, such as its settings.
        """
        self.records()[client_id] = {
            "protocol_id": client_id,
            "enabled": enabled,
            "provider": "sendspin",
            "player_id": (self.records().get(rides_on) or {}).get("player_id", PLAYER_ID),
            "values": {CONF_UNDERLYING_PLAYER_ID: rides_on, **(values or {})},
        }

    async def unload_provider(self, domain: str) -> None:
        """
        Unload a provider, which takes its protocol and its bridge along.

        Before anything is attached, this is a provider that is not loaded yet.

        :param domain: The domain of the provider.
        """
        protocol = self.protocols[domain]
        if protocol.player is not None:
            await self.manager_of(protocol).stop_all()
            await self.detach(protocol)
        self.providers.pop(domain)
        for manager in self.managers:
            if manager.provider.domain != domain:
                await manager._on_providers_updated(MagicMock())
        await self.settle()

    async def load_provider(self, domain: str) -> None:
        """
        Load a provider that was not loaded yet, which every manager hears of.

        :param domain: The domain of the provider.
        """
        self.providers[domain] = self._all_providers[domain]
        for manager in self.managers:
            await manager._on_providers_updated(MagicMock())
        await self.settle()

    async def attach(self, protocol: Any, player: _Player | None = None) -> None:
        """
        Attach a protocol to a player, then let its manager evaluate its bridge.

        In the order of the players controller: the hooks of the protocol start the
        evaluation of its bridge (AirPlay and squeezelite at once, Cast as a task that
        waits), then the changed outputs of the player are published, which the managers
        act on at once as Music Assistant starts its tasks eagerly, and then the AirPlay
        provider evaluates its bridge again once the registration returned.

        :param protocol: The protocol that attaches, as its provider registers it.
        :param player: The player it attaches to; the player of the device by default.
        """
        domain = protocol.provider.domain
        assert self.providers.get(domain) is protocol.provider, f"{domain} is not loaded"
        player = player or self.player
        self.records()[protocol.protocol_id]["player_id"] = player.player_id
        player.attached_protocols[protocol.protocol_id] = protocol
        protocol.player = player
        manager = self.manager_of(protocol)
        if domain == "airplay":
            self.mass.create_task(manager.evaluate_bridge(protocol))
        elif domain == "squeezelite":
            self.mass.create_task(evaluate_bridge_of(self.mass, protocol.protocol_id))
        else:
            self.mass.create_task(manager.evaluate_bridge(protocol), eager_start=False)
        self._outputs_changed(player)
        if domain == "airplay":
            await manager.evaluate_bridge(protocol)
        await self.settle()

    async def switch(self, protocol: Any, enabled: bool) -> None:
        """
        Let the user switch a protocol of the player on or off, by saving the player config.

        A protocol switched off is detached; one switched on attaches once its provider
        finds the device again, which the test does with attach().

        :param protocol: The protocol.
        :param enabled: Whether it is switched on.
        """
        await self.config.save_protocol_config(protocol.protocol_id, {"enabled": enabled})
        await self._player_config_saved(protocol.protocol_id)

    async def switch_output(self, enabled: bool) -> None:
        """
        Let the user switch the Sendspin output of the player on or off.

        :param enabled: Whether the output is switched on.
        """
        (client_id,) = self.outputs()
        await self.config.save_protocol_config(client_id, {"enabled": enabled})
        await self._player_config_saved(client_id)

    async def detach(self, protocol: Any, *, settle: bool = True) -> None:
        """
        Detach a protocol from the player, as a disconnect or switching it off does.

        :param protocol: The protocol that is detached.
        :param settle: Whether to let what the detach started finish before returning.
        """
        player = protocol.player
        del player.attached_protocols[protocol.protocol_id]
        protocol.player = None
        callbacks, protocol._on_detach_callbacks = protocol._on_detach_callbacks, []
        for callback in callbacks:
            callback()
        self._outputs_changed(player)
        if settle:
            await self.settle()

    async def link(self, protocol: Any) -> None:
        """
        Move a protocol from a player of its own to the player of the device, as linking does.

        :param protocol: The protocol that is linked.
        """
        await self.detach(protocol)
        await self.attach(protocol)

    def sendspin_output(self) -> str | None:
        """Return the client id of the Sendspin output attached to the player, if any."""
        for client_id in self.sendspin_protocols:
            if self.server.get_client(client_id) is None:
                continue
            if not self.mass.players.wants_protocol(client_id):
                continue
            others = [
                other_id
                for other_id, record in self.outputs().items()
                if other_id != client_id and record.get("enabled", True)
            ]
            if not others:
                return client_id
        return None

    def owner_of(self, client_id: str) -> str | None:
        """
        Return the protocol whose bridge holds the client of the server.

        That is the bridge that registered it last, by its stream-start callback, whose role
        the client carries, or the role of a connection its receiver attached.

        :param client_id: The client id.
        """
        if (client := self.server.get_client(client_id)) is None:
            return None
        roles = client.roles_by_family("player")
        for protocol_id, bridge in self.bridges.items():
            if (
                bridge.client is client
                and (bridge.role in roles or client.connection is not None)
                and self.server._external_stream_start_cbs.get(client_id) is bridge.on_stream_start
            ):
                return protocol_id
        return None

    def rides_on(self, client_id: str) -> str | None:
        """Return the id of the protocol a stored Sendspin output rides on, if it is stored."""
        rides_on = self.config.get_protocol_value(client_id, CONF_UNDERLYING_PLAYER_ID)
        return rides_on if isinstance(rides_on, str) else None

    def state(self) -> tuple[str | None, str | None, str | None, str | None]:
        """
        Return where the Sendspin output of the device is at.

        :return: The domain of the bridge that is registered, the client id of the stored
            output, the domain of its carrier and whether it is "on", "off-user" (switched
            off by the user) or "off-default" (left off by an experimental carrier).
        """
        if not (outputs := self.outputs()):
            return (self._registered(None), None, None, None)
        assert len(outputs) == 1, f"more than one Sendspin output is stored: {sorted(outputs)}"
        ((client_id, record),) = outputs.items()
        carrier = self.records().get(self.rides_on(client_id) or "") or {}
        if record.get("enabled", True):
            kind = "on"
        elif record["values"].get(CONF_PROTOCOL_OFF_BY_DEFAULT):
            kind = "off-default"
        else:
            kind = "off-user"
        return (self._registered(client_id), client_id, carrier.get("provider"), kind)

    async def settle(self) -> None:
        """Let the evaluations the managers scheduled run."""
        for _ in range(50):
            await asyncio.sleep(0)

    async def close(self) -> None:
        """Stop the bridges and close the Sendspin server of the device."""
        for manager in self.managers:
            await manager.close()
        await self.settle()
        await self.server.close()

    async def on_protocol_config_change(self, protocol_id: str, changed_keys: set[str]) -> None:
        """
        Handle the changed config of a protocol, as the players controller does.

        A protocol that is switched off is detached; a Sendspin output that is switched on
        while it is not attached is looked for again by the Sendspin provider.

        :param protocol_id: Id of the protocol.
        :param changed_keys: The keys that changed.
        """
        if "enabled" not in changed_keys:
            return
        record = self.records()[protocol_id]
        if record["provider"] == "sendspin":
            if not record["enabled"]:
                await self._unregister_sendspin(protocol_id)
            elif (rides_on := self.rides_on(protocol_id)) is not None:
                # the Sendspin provider looks for its devices again, as a task of its own
                self.mass.create_task(evaluate_bridge_of(self.mass, rides_on))
        elif not record["enabled"] and (protocol := self._get_protocol(protocol_id)) is not None:
            await self.detach(protocol, settle=False)

    def _registered(self, client_id: str | None) -> str | None:
        """Return the domain of the one bridge that is registered with the Sendspin server."""
        registered: list[str] = [
            manager.provider.domain for manager in self.managers if manager._bridges
        ]
        assert len(registered) <= 1, f"more than one bridge is registered: {registered}"
        if not registered:
            assert self.sendspin_output() is None, "an output without a bridge is attached"
            return None
        assert client_id is not None, "a bridge is registered without a stored output"
        protocol_id = self.protocols[registered[0]].protocol_id
        assert self.owner_of(client_id) == protocol_id, "the client is held by another bridge"
        assert self.sendspin_protocols.get(client_id) == protocol_id, (
            "the Sendspin protocol rides on another protocol"
        )
        assert self.sendspin_output() == client_id, "the output of the bridge is not attached"
        return registered[0]

    async def _player_config_saved(self, protocol_id: str) -> None:
        """Let the Sendspin provider hear that a config of a player was saved."""
        if player_id := self.records()[protocol_id].get("player_id"):
            # the listener of an event runs as a task of its own
            self.mass.create_task(evaluate_bridges_of_player(self.mass, player_id))
        await self.settle()

    def _protocol(self, domain: str, manager_class: type, protocol_id: str) -> MagicMock:
        """Create a protocol of the device, with the provider and real bridge manager of it."""
        provider = MagicMock(mass=self.mass, domain=domain, instance_id=domain)
        provider.logger = logging.getLogger(f"test.{domain}")
        protocol = MagicMock(protocol_id=protocol_id, provider=provider, player=None)
        protocol.display_name = f"Den ({domain})"
        protocol._on_detach_callbacks = []
        provider.protocols = [protocol]
        provider.get_protocol = lambda pid: (
            protocol if pid == protocol_id and protocol.player is not None else None
        )
        self.records()[protocol_id] = {
            "protocol_id": protocol_id,
            "enabled": True,
            "provider": domain,
            "player_id": PLAYER_ID,
            "values": {},
        }
        self.providers[domain] = self._all_providers[domain] = provider
        self.protocols[domain] = protocol
        manager = provider.bridge_manager = manager_class(provider)
        manager._create_bridge = MagicMock(side_effect=lambda _: self._bridge(protocol, domain))
        self.managers.append(manager)
        return protocol

    def _bridge(self, protocol: Any, domain: str) -> MagicMock:
        """
        Return a bridge that registers its client with the Sendspin server.

        It stops as the bridges do: it only removes the client it still holds.
        """
        protocol_id = protocol.protocol_id
        bridge = MagicMock(sendspin_server=self.server, shared_group=None)
        bridge.client = None
        # as the real bridges: registered once the registration returned its client
        type(bridge).is_registered = PropertyMock(side_effect=lambda: bridge.client is not None)

        async def _start() -> None:
            client_id = self.manager_of(protocol).bridge_client_id(protocol)
            self._underlying_ids[client_id] = protocol_id
            bridge.client = self.server.register_external_player(
                hello(
                    client_id,
                    SLIMPROTO_BRIDGE_ROLE_ID if domain == "squeezelite" else BRIDGE_ROLE_ID,
                ),
                on_stream_start=bridge.on_stream_start,
            )
            (bridge.role,) = bridge.client.roles_by_family("player")
            if domain == "chromecast" and self._cast_connected:
                connect_cast_receiver(bridge.client, state_received=self._cast_state_received)
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
            if holds_bridge_client(self.mass, bridge.client.client_id, protocol_id):
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
        # the Sendspin protocol of the client rides on it from now on, with its delay
        hand_static_delay_over(self.mass, client_id, rides_on)
        self.sendspin_protocols[client_id] = rides_on
        if client_id not in self.records():
            self.store_output(client_id, rides_on)
        # the attach stores the player it belongs to and the protocol it rides on
        record = self.records()[client_id]
        record["player_id"] = self.records()[rides_on]["player_id"]
        record["values"][CONF_UNDERLYING_PLAYER_ID] = rides_on
        self._outputs_changed(self._player_of(rides_on))

    def _get_protocol(self, protocol_id: str) -> Any:
        """Return an attached protocol: one of the device, or a Sendspin protocol."""
        if (rides_on := self.sendspin_protocols.get(protocol_id)) is not None:
            return MagicMock(underlying_protocol_id=rides_on, player=self._player_of(rides_on))
        if (player := self._player_of(protocol_id)) is not None:
            return player.attached_protocols[protocol_id]
        return None

    def _player_of(self, protocol_id: str) -> _Player | None:
        """Return the player a protocol of the device is attached to, if it is."""
        for player in (self.player, self.other_player):
            if protocol_id in player.attached_protocols:
                return player
        return None

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
        if (player := self._player_of(rides_on)) is not None:
            # its output is unavailable already while the protocol it rides on is away
            self._outputs_changed(player)

    async def _remove_protocol(self, protocol_id: str) -> None:
        """Remove a protocol for good, which changes the outputs of the player."""
        await self._unregister_sendspin(protocol_id)
        record = self.records().pop(protocol_id, None) or {}
        self._outputs_changed(
            self.other_player if record.get("player_id") == OTHER_PLAYER_ID else None
        )

    def _outputs_changed(self, player: _Player | None = None) -> None:
        """
        Let every manager hear that the outputs of a player changed.

        :param player: The player; the player of the device by default.
        """
        for manager in self.managers:
            manager._on_player_state_updated(
                player or self.player, {"output_protocols": (None, None)}
            )
