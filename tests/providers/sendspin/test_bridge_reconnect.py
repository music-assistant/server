"""Tests for the bridge client of a Cast or AirPlay device, whichever of the two connects first."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import pytest
from aiosendspin.models.core import ClientHelloPayload
from aiosendspin.models.core import DeviceInfo as SendspinDeviceInfo
from aiosendspin.noise.trust_store import PskCategory
from music_assistant_models.enums import EventType, IdentifierType, ProtocolRole
from pychromecast.const import CAST_TYPE_AUDIO

from music_assistant.providers.airplay.player import GenericAirPlayPlayer
from music_assistant.providers.chromecast.helpers import ChromecastInfo
from music_assistant.providers.chromecast.player import ChromecastPlayer
from music_assistant.providers.chromecast.sendspin_bridge import SendspinBridgeManager
from music_assistant.providers.sendspin.player import SendspinPlayer
from music_assistant.providers.sendspin.provider import SendspinProvider

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigEntry

    from music_assistant.models.player_protocol import PlayerProtocol
    from tests.scenarios.harness import Scenario

MAC = "a0:b7:65:00:11:22"
BRIDGE_ID = "spb_a0b765001122"
CAST_UUID = UUID("4f6c2b1a-0000-4000-8000-a0b765001122")
AIRPLAY_ID = "apa0b765001122"

# the code of the Sendspin provider that turns a connected client into its protocol
_CLIENT_CODE = (
    "_handle_client_added",
    "_handle_client_updated",
    "_create_player",
    "_device_role",
    "apply_bridge_claim",
    "register_bridge_identifiers",
    "register_bridge_underlying_protocol",
    "register_bridge_static_delay_default",
    "_begin_client_event",
    "_finish_client_event",
    "_is_current_client_event",
    "_await_client_hello",
    "_apply_hass_esphome_enrichment",
)


@pytest.fixture(autouse=True)
def _no_cast_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep an attached Cast protocol from connecting to its device."""
    monkeypatch.setattr("pychromecast.get_chromecast_from_cast_info", MagicMock())


@pytest.fixture(autouse=True)
def _no_sendspin_entries(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the config entries of a client out of the pairing state."""

    async def _entries(_self: SendspinPlayer) -> list[ConfigEntry]:
        return []

    monkeypatch.setattr(SendspinPlayer, "get_config_entries", _entries)


def _sendspin(scenario: Scenario) -> Any:
    """
    Return the loaded Sendspin provider, with the real code that handles its clients.

    :param scenario: The running server.
    """
    provider: Any = scenario.provider("sendspin")
    for name in _CLIENT_CODE:
        setattr(provider, name, getattr(SendspinProvider, name).__get__(provider))
    provider.clients = {}
    provider.server_api = SimpleNamespace(get_client=provider.clients.get)
    provider.is_virtual_player = lambda _client_id: False
    provider.is_held_unapproved = lambda _client_id: False
    provider.is_held_elsewhere = lambda _client_id: False
    provider._auto_trust_guest_access = AsyncMock()
    provider._unloading = False
    provider._pending_unregisters = {}
    provider._client_event_versions = {}
    provider._client_event_task_counts = {}
    provider._bridge_identifiers = {}
    provider._bridge_underlying_protocols = {}
    provider._bridge_static_delay_defaults = {}
    provider._bridge_player_types = {}
    return provider


def _hello(product_name: str) -> ClientHelloPayload:
    """
    Return the hello of the bridge client.

    :param product_name: What it reports as product: the bridge's, or the Cast receiver app's.
    """
    return ClientHelloPayload(
        client_id=BRIDGE_ID,
        name="Speaker A",
        version=1,
        supported_roles=["player@v1"],
        device_info=SendspinDeviceInfo(product_name=product_name, manufacturer="Acme"),
    )


async def _connect(scenario: Scenario, sendspin: Any, hello: ClientHelloPayload) -> None:
    """
    Let the bridge client connect, as the Sendspin server reports it.

    :param scenario: The running server.
    :param sendspin: The Sendspin provider.
    :param hello: The hello the client connects with.
    """
    client = MagicMock(
        client_id=BRIDGE_ID,
        info=hello,
        info_or_none=hello,
        negotiated_role_ids=["player@v1"],
        active_roles=[object()],
        connection_security=SimpleNamespace(psk_category=PskCategory.LONG_TERM),
        is_connected=True,
    )
    client.roles_by_family.return_value = []
    sendspin.clients[BRIDGE_ID] = client
    await sendspin._handle_client_added(BRIDGE_ID, sendspin._begin_client_event(BRIDGE_ID))
    await scenario.settle()


async def _send_hello(scenario: Scenario, sendspin: Any, hello: ClientHelloPayload) -> None:
    """
    Let the connected bridge client send a new hello: the Cast receiver app took it over.

    :param scenario: The running server.
    :param sendspin: The Sendspin provider.
    :param hello: The new hello.
    """
    client = sendspin.clients[BRIDGE_ID]
    client.info = client.info_or_none = hello
    await sendspin._handle_client_updated(BRIDGE_ID, sendspin._begin_client_event(BRIDGE_ID))
    await scenario.settle()


def _carrier(scenario: Scenario, domain: str) -> PlayerProtocol:
    """
    Return a new object of the protocol the bridge rides on, as its provider creates it.

    :param scenario: The running server.
    :param domain: ``chromecast`` for a Google speaker, ``airplay`` for an AirPlay speaker.
    """
    provider: Any = scenario.provider(domain)
    if domain == "chromecast":
        provider.mz_mgr = MagicMock()
        cast_info = ChromecastInfo(
            services=set(),
            uuid=CAST_UUID,
            model_name="Speaker Model",
            friendly_name="Speaker A",
            host="192.168.1.71",
            port=8009,
            cast_type=CAST_TYPE_AUDIO,
            manufacturer="Google Inc.",
            mac_address=MAC,
            is_dynamic_group=False,
            is_multichannel_group=False,
        )
        provider.bridge_manager = MagicMock(evaluate_bridge=AsyncMock())
        cast_player = ChromecastPlayer(provider, str(CAST_UUID), cast_info)
        # connected, as a device that runs the receiver app is
        cast_player._attr_available = True
        return cast_player
    provider.bridge_manager = MagicMock(evaluate_bridge=AsyncMock(), remove_bridge=AsyncMock())
    provider.get_players = lambda: list(provider.protocols)
    return GenericAirPlayPlayer(
        provider, AIRPLAY_ID, None, None, "192.168.1.72", "Speaker A", "Acme", "Speaker1,1"
    )


async def _set_up_bridge(scenario: Scenario, sendspin: Any, carrier: PlayerProtocol) -> None:
    """
    Register the bridge of an attached protocol, as its bridge manager does.

    :param scenario: The running server.
    :param sendspin: The Sendspin provider.
    :param carrier: The protocol the bridge rides on.
    """
    identifier = (
        (IdentifierType.CAST_UUID, str(CAST_UUID))
        if isinstance(carrier, ChromecastPlayer)
        else (IdentifierType.AIRPLAY_ID, AIRPLAY_ID)
    )
    sendspin.register_bridge_identifiers(BRIDGE_ID, dict([identifier]))
    sendspin.register_bridge_underlying_protocol(BRIDGE_ID, carrier.protocol_id)
    await _connect(scenario, sendspin, _hello("Speaker Bridge"))


async def _claim(scenario: Scenario, sendspin: Any, carrier: PlayerProtocol) -> None:
    """
    Claim the connected receiver app of a Cast device, as the Cast bridge manager does.

    :param scenario: The running server.
    :param sendspin: The Sendspin provider.
    :param carrier: The Cast protocol.
    """
    assert await sendspin.apply_bridge_claim(
        BRIDGE_ID,
        {IdentifierType.CAST_UUID: str(CAST_UUID)},
        _hello("Speaker Bridge"),
        underlying_protocol_id=carrier.protocol_id,
    )
    await scenario.settle()


async def _register_or_fail(scenario: Scenario, protocol: PlayerProtocol) -> None:
    """
    Register a protocol and let the server settle; fail when the register does not finish.

    :param scenario: The running server.
    :param protocol: The protocol to register.
    """
    register = asyncio.create_task(scenario.mass.players.register(protocol))
    await scenario.settle()
    if not register.done():
        register.cancel()
        await asyncio.wait([register])
        pytest.fail(f"the register of {protocol.protocol_id} never finished")
    register.result()


def _assert_the_only_player(scenario: Scenario, player_id: str, *, available: bool) -> None:
    """
    Assert what clients see: one player, the one of the device, which is not private.

    :param scenario: The running server.
    :param player_id: Id of the player of the device.
    :param available: Whether the player and its Sendspin output are available.
    """
    players = scenario.visible_players()
    assert [player.player_id for player in players] == [player_id]
    state = scenario.player(player_id)
    assert state is not None
    assert state.private is False
    assert players[0].available is available
    assert dict(players[0].outputs)["sendspin"] is available


async def _bridged_device(scenario: Scenario, domain: str) -> tuple[str, dict[str, Any]]:
    """
    Set up a device with its bridge; return the id of its player and the records of both.

    :param scenario: The running server.
    :param domain: The domain of the protocol the bridge rides on.
    """
    await scenario.add_provider(domain)
    await scenario.add_provider("sendspin")
    sendspin = _sendspin(scenario)
    carrier = _carrier(scenario, domain)
    await scenario.mass.players.register(carrier)
    await scenario.settle()
    assert carrier.player is not None
    player_id = carrier.player.player_id
    await scenario.configure_player(player_id, name="Kitchen")
    await _set_up_bridge(scenario, sendspin, carrier)
    assert carrier.player.get_role(BRIDGE_ID) == ProtocolRole.AUDIO
    records = {
        protocol_id: deepcopy(scenario.mass.config.get_protocol_record(protocol_id))
        for protocol_id in (carrier.protocol_id, BRIDGE_ID)
    }
    return player_id, records


@pytest.mark.parametrize("domain", ["chromecast", "airplay"])
@pytest.mark.parametrize("bridge_first", [True, False], ids=["bridge_first", "carrier_first"])
async def test_a_bridged_device_keeps_its_player_whichever_connects_first(
    scenario: Scenario, domain: str, bridge_first: bool
) -> None:
    """
    After a restart the bridge client of a device is an output of its player again.

    Whether the bridge client (for Cast: the receiver app on the speaker) or the protocol
    it rides on connects first, the player keeps its id, name and records and is never
    private, and no other player appears.
    """
    player_id, records = await _bridged_device(scenario, domain)

    await scenario.restart()
    scenario.clear_events()
    sendspin = _sendspin(scenario)
    carrier = _carrier(scenario, domain)
    if bridge_first:
        # a Cast receiver app reports as a browser
        first_hello = _hello("Web Browser" if domain == "chromecast" else "Speaker Bridge")
        await _connect(scenario, sendspin, first_hello)
        # a new hello (the app reconnects once more) leaves it a bridge
        await _send_hello(scenario, sendspin, first_hello)
        # until the protocol it rides on is back, the bridge is no output to play on
        _assert_the_only_player(scenario, player_id, available=False)
        await scenario.mass.players.register(carrier)
        await scenario.settle()
        if domain == "chromecast":
            await _claim(scenario, sendspin, carrier)
        else:
            # the AirPlay bridge registers its client at every attach of its protocol
            await _set_up_bridge(scenario, sendspin, carrier)
    else:
        await scenario.mass.players.register(carrier)
        await scenario.settle()
        await _set_up_bridge(scenario, sendspin, carrier)
        if domain == "chromecast":
            # the receiver app connects to the client the bridge registered
            await _send_hello(scenario, sendspin, _hello("Web Browser"))

    _assert_the_only_player(scenario, player_id, available=True)
    assert {event.object_id for event in scenario.events(EventType.PLAYER_ADDED)} <= {player_id}
    bridge = scenario.mass.players.get_protocol(BRIDGE_ID)
    assert bridge is not None
    assert carrier.player is not None
    assert bridge.player is carrier.player
    assert carrier.player.player_id == player_id
    assert carrier.player.display_name == "Kitchen"
    assert carrier.player.get_role(BRIDGE_ID) == ProtocolRole.AUDIO
    assert bridge.session_only is False
    for protocol_id, record in records.items():
        assert scenario.mass.config.get_protocol_record(protocol_id) == record

    await scenario.mass.players.unregister(carrier.protocol_id)
    await scenario.settle()

    _assert_the_only_player(scenario, player_id, available=False)


async def test_a_cast_device_claims_the_receiver_app_that_connected_before_it(
    scenario: Scenario,
) -> None:
    """
    A Cast device claims the Sendspin client its receiver app already runs, at its attach.

    The claim attaches that client, so the attach of the Cast device must not hold the
    attach of other protocols up: it completes, and a later device still attaches.
    """
    await scenario.add_provider("chromecast")
    await scenario.add_provider("sendspin")
    sendspin = _sendspin(scenario)
    # the receiver app connected by itself: no bridge registered it
    sendspin.server_api.is_external_player = lambda _client_id: False
    sendspin.server_api.add_event_listener = MagicMock()
    # a Cast receiver app reports as a browser
    await _connect(scenario, sendspin, _hello("Web Browser"))
    carrier = _carrier(scenario, "chromecast")
    provider: Any = carrier.provider
    provider.bridge_manager = SendspinBridgeManager(provider)

    await _register_or_fail(scenario, carrier)

    # the claimed client is an output of the Cast device's player (switched off for now:
    # Sendspin over Cast is only offered once the user enables it)
    assert carrier.player is not None
    record = scenario.mass.config.get_protocol_record(BRIDGE_ID) or {}
    assert record.get("player_id") == carrier.player.player_id
    other = (await scenario.add_provider("sonos")).protocol("RINCON_OTHER", "Speaker B")
    await _register_or_fail(scenario, other)
    assert scenario.player_of(other) is not None
