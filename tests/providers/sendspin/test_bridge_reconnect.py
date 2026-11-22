"""Tests for the bridge client of a Cast or AirPlay device, whichever of the two connects first."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import pytest
from aiosendspin.models.core import ClientHelloPayload
from aiosendspin.models.core import DeviceInfo as SendspinDeviceInfo
from aiosendspin.models.player import ClientHelloPlayerSupport, SupportedAudioFormat
from aiosendspin.models.types import AudioCodec, PlayerCommand
from aiosendspin.noise.trust_store import PskCategory
from music_assistant_models.enums import EventType, IdentifierType, PlayerFeature, ProtocolRole
from pychromecast.const import CAST_TYPE_AUDIO

from music_assistant.constants import CONF_PLAYER_PROTOCOLS, CONF_UNDERLYING_PLAYER_ID
from music_assistant.providers.airplay.player import GenericAirPlayPlayer
from music_assistant.providers.chromecast.helpers import ChromecastInfo
from music_assistant.providers.chromecast.player import ChromecastPlayer
from music_assistant.providers.chromecast.sendspin_bridge import SendspinBridgeManager
from music_assistant.providers.sendspin.constants import (
    CONF_SENDSPIN_BRIDGE_STATIC_DELAYS,
    CONF_SENDSPIN_STATIC_DELAY,
    DEFAULT_SENDSPIN_STATIC_DELAY,
)
from music_assistant.providers.sendspin.player import SendspinPlayer
from music_assistant.providers.sendspin.provider import SendspinProvider
from music_assistant.providers.sendspin.sources import SendspinSources

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
    "hold_bridge_client",
    "get_bridge_holder",
    "release_bridge_client",
    "wait_for_bridge_client",
    "stop_waiting_for_bridge_client",
    "wake_bridge_waiters",
    "refresh_bridge_client",
    "bridge_lock",
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
    provider.sources = SendspinSources(provider)
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
    provider._bridge_holders = {}
    provider._bridge_waiters = {}
    provider._bridge_locks = {}
    provider._multi_channel_speakers = {}
    return provider


def _hello(product_name: str, commands: list[PlayerCommand] | None = None) -> ClientHelloPayload:
    """
    Return the hello of the bridge client.

    :param product_name: What it reports as product: the bridge's, or the Cast receiver app's.
    :param commands: The volume controls the hello lists, as a bridge's does; None lists none.
    """
    player_support = (
        None
        if commands is None
        else ClientHelloPlayerSupport(
            supported_formats=[
                SupportedAudioFormat(
                    codec=AudioCodec.PCM, channels=2, sample_rate=44100, bit_depth=16
                )
            ],
            buffer_capacity=1_000,
            supported_commands=commands,
        )
    )
    return ClientHelloPayload(
        client_id=BRIDGE_ID,
        name="Speaker A",
        version=1,
        supported_roles=["player@v1"],
        device_info=SendspinDeviceInfo(product_name=product_name, manufacturer="Acme"),
        player_support=player_support,
    )


async def _connect(scenario: Scenario, sendspin: Any, hello: ClientHelloPayload) -> None:
    """
    Let the bridge client connect, as the Sendspin server reports it.

    :param scenario: The running server.
    :param sendspin: The Sendspin provider.
    :param hello: The hello the client connects with.
    """
    client_id = hello.client_id
    client = MagicMock(
        client_id=client_id,
        info=hello,
        info_or_none=hello,
        negotiated_role_ids=["player@v1"],
        active_roles=[object()],
        connection_security=SimpleNamespace(psk_category=PskCategory.LONG_TERM),
        is_connected=True,
    )
    client.roles_by_family.return_value = []
    sendspin.clients[client_id] = client
    await sendspin._handle_client_added(client_id, sendspin._begin_client_event(client_id))
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


async def _set_up_bridge(
    scenario: Scenario,
    sendspin: Any,
    carrier: PlayerProtocol,
    hello: ClientHelloPayload | None = None,
) -> None:
    """
    Register the bridge of an attached protocol, as its bridge manager does.

    :param scenario: The running server.
    :param sendspin: The Sendspin provider.
    :param carrier: The protocol the bridge rides on.
    :param hello: The hello the bridge registers with; a plain bridge hello when None.
    """
    identifier = (
        (IdentifierType.CAST_UUID, str(CAST_UUID))
        if isinstance(carrier, ChromecastPlayer)
        else (IdentifierType.AIRPLAY_ID, AIRPLAY_ID)
    )
    sendspin.register_bridge_identifiers(BRIDGE_ID, dict([identifier]))
    sendspin.register_bridge_underlying_protocol(BRIDGE_ID, carrier.protocol_id)
    await _connect(scenario, sendspin, hello or _hello("Speaker Bridge"))


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


async def _bridged_device(
    scenario: Scenario, domain: str, hello: ClientHelloPayload | None = None
) -> tuple[str, dict[str, Any]]:
    """
    Set up a device with its bridge; return the id of its player and the records of both.

    :param scenario: The running server.
    :param domain: The domain of the protocol the bridge rides on.
    :param hello: The hello the bridge registers with; a plain bridge hello when None.
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
    await _set_up_bridge(scenario, sendspin, carrier, hello)
    assert carrier.player.get_role(BRIDGE_ID) == ProtocolRole.AUDIO
    records = {
        protocol_id: deepcopy(scenario.mass.config.get_protocol_record(protocol_id))
        for protocol_id in (carrier.protocol_id, BRIDGE_ID)
    }
    return player_id, records


async def _airplay_and_cast_device(scenario: Scenario) -> tuple[Any, PlayerProtocol]:
    """
    Set up a device whose AirPlay bridge carries the output, with its Cast protocol attached.

    :param scenario: The running server.
    :return: The Sendspin provider and the Cast protocol.
    """
    await _bridged_device(scenario, "airplay")
    await scenario.add_provider("chromecast")
    cast = _carrier(scenario, "chromecast")
    await scenario.mass.players.register(cast)
    await scenario.settle()
    return scenario.provider("sendspin"), cast


async def _cast_takes_over(scenario: Scenario, sendspin: Any, cast: PlayerProtocol) -> None:
    """
    Let the Cast bridge register the client the AirPlay bridge removed: AirPlay was switched off.

    :param scenario: The running server.
    :param sendspin: The Sendspin provider.
    :param cast: The Cast protocol.
    """
    await scenario.mass.players.unregister(BRIDGE_ID)
    del sendspin.clients[BRIDGE_ID]
    # the Cast bridge sets the default delay of its speaker model
    sendspin.register_bridge_static_delay_default(BRIDGE_ID, 330)
    await _set_up_bridge(scenario, sendspin, cast)


async def _airplay_takes_over(scenario: Scenario, sendspin: Any) -> None:
    """
    Let the AirPlay bridge register the client the Cast bridge holds: AirPlay is switched on.

    :param scenario: The running server.
    :param sendspin: The Sendspin provider.
    """
    sendspin.register_bridge_identifiers(BRIDGE_ID, {IdentifierType.AIRPLAY_ID: AIRPLAY_ID})
    sendspin.register_bridge_underlying_protocol(BRIDGE_ID, AIRPLAY_ID)
    sendspin.hold_bridge_client(BRIDGE_ID, AIRPLAY_ID)
    await scenario.settle()


def _stored_delays(scenario: Scenario, client_id: str = BRIDGE_ID) -> tuple[Any, Any, Any]:
    """
    Return what the output stores: the protocol it rides on, its delay and those put aside.

    :param scenario: The running server.
    :param client_id: The client id of the output.
    """
    values = (scenario.mass.config.get_protocol_record(client_id) or {}).get("values") or {}
    return (
        values.get(CONF_UNDERLYING_PLAYER_ID),
        values.get(CONF_SENDSPIN_STATIC_DELAY),
        values.get(CONF_SENDSPIN_BRIDGE_STATIC_DELAYS),
    )


def _set_aside(scenario: Scenario, delays: dict[str, int]) -> None:
    """
    Store the static delays put aside for the protocols that do not carry the output.

    :param scenario: The running server.
    :param delays: The delays, by the id of their protocol.
    """
    scenario.mass.config.set(
        f"{CONF_PLAYER_PROTOCOLS}/{BRIDGE_ID}/values/{CONF_SENDSPIN_BRIDGE_STATIC_DELAYS}", delays
    )


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
    assert carrier.player.name == "Kitchen"
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


async def test_a_client_another_bridge_registered_rides_on_that_bridge_once_refreshed(
    scenario: Scenario,
) -> None:
    """A client the AirPlay bridge registered again moves its output over to AirPlay."""
    player_id, _records = await _bridged_device(scenario, "chromecast")
    await scenario.add_provider("airplay")
    airplay = _carrier(scenario, "airplay")
    await scenario.mass.players.register(airplay)
    await scenario.settle()
    assert airplay.player is not None
    assert airplay.player.player_id == player_id
    # the provider _bridged_device prepared, with the client it connected
    sendspin: Any = scenario.provider("sendspin")
    scenario.mass.config.set_protocol_value(BRIDGE_ID, "static_delay", 120)
    # an add of the client that started before the AirPlay bridge registered it
    stale_add = sendspin._begin_client_event(BRIDGE_ID)
    # the AirPlay bridge registers the same client id, which Sendspin reports no event for
    sendspin.register_bridge_identifiers(BRIDGE_ID, {IdentifierType.AIRPLAY_ID: AIRPLAY_ID})
    sendspin.register_bridge_underlying_protocol(BRIDGE_ID, AIRPLAY_ID)

    sendspin.refresh_bridge_client(BRIDGE_ID)
    await scenario.settle()

    bridge = scenario.mass.players.get_protocol(BRIDGE_ID)
    assert bridge is not None
    assert bridge.underlying_protocol_id == AIRPLAY_ID
    assert bridge.player is airplay.player
    record = scenario.mass.config.get_protocol_record(BRIDGE_ID)
    assert record is not None
    assert record["values"]["underlying_player_id"] == AIRPLAY_ID
    assert record["values"]["static_delay"] == 120

    await sendspin._handle_client_added(BRIDGE_ID, stale_add)
    await scenario.settle()

    assert scenario.mass.players.get_protocol(BRIDGE_ID) is bridge, "the stale add replaced it"


async def test_a_client_refreshed_twice_rides_on_the_bridge_that_holds_it(
    scenario: Scenario,
) -> None:
    """A refresh that follows one still waiting to register leaves the output on the holder."""
    await _bridged_device(scenario, "chromecast")
    await scenario.add_provider("airplay")
    airplay = _carrier(scenario, "airplay")
    await scenario.mass.players.register(airplay)
    await scenario.settle()
    sendspin: Any = scenario.provider("sendspin")
    # the AirPlay bridge registered the same client id and holds it from then on
    sendspin.register_bridge_identifiers(BRIDGE_ID, {IdentifierType.AIRPLAY_ID: AIRPLAY_ID})
    sendspin.register_bridge_underlying_protocol(BRIDGE_ID, AIRPLAY_ID)
    sendspin.hold_bridge_client(BRIDGE_ID, AIRPLAY_ID)

    async with scenario.mass.players._register_lock:
        # the first refresh waits to register its protocol when the second one starts
        sendspin.refresh_bridge_client(BRIDGE_ID)
        sendspin.refresh_bridge_client(BRIDGE_ID)
    await scenario.settle()

    bridge = scenario.mass.players.get_protocol(BRIDGE_ID)
    assert bridge is not None
    assert bridge.underlying_protocol_id == AIRPLAY_ID
    record = scenario.mass.config.get_protocol_record(BRIDGE_ID)
    assert record is not None
    assert record["values"]["underlying_player_id"] == AIRPLAY_ID


@pytest.mark.parametrize(
    "previous_commands",
    [[], [PlayerCommand.VOLUME]],
    ids=["after a Cast bridge", "after a squeezelite bridge"],
)
async def test_a_client_another_bridge_takes_over_gets_the_settings_of_that_bridge(
    scenario: Scenario, previous_commands: list[PlayerCommand]
) -> None:
    """AirPlay holding a client another bridge registered has AirPlay's controls and delay."""
    await _bridged_device(scenario, "chromecast", _hello("Speaker Bridge", previous_commands))
    sendspin: Any = scenario.provider("sendspin")
    # the Cast bridge sets the default delay of its speaker model
    sendspin.register_bridge_static_delay_default(BRIDGE_ID, 330)
    await scenario.add_provider("airplay")
    airplay = _carrier(scenario, "airplay")
    await scenario.mass.players.register(airplay)
    await scenario.settle()
    # the AirPlay bridge registers the same client id with its own hello and holds it
    hello = _hello("Speaker Bridge", [PlayerCommand.VOLUME, PlayerCommand.MUTE])
    sendspin.clients[BRIDGE_ID].info = sendspin.clients[BRIDGE_ID].info_or_none = hello
    sendspin.register_bridge_identifiers(BRIDGE_ID, {IdentifierType.AIRPLAY_ID: AIRPLAY_ID})
    sendspin.register_bridge_underlying_protocol(BRIDGE_ID, AIRPLAY_ID)
    sendspin.hold_bridge_client(BRIDGE_ID, AIRPLAY_ID)

    sendspin.refresh_bridge_client(BRIDGE_ID)
    await scenario.settle()

    bridge = scenario.mass.players.get_protocol(BRIDGE_ID)
    assert isinstance(bridge, SendspinPlayer)
    assert bridge.underlying_protocol_id == AIRPLAY_ID
    assert {PlayerFeature.VOLUME_SET, PlayerFeature.VOLUME_MUTE} <= bridge.supported_features
    assert bridge.static_delay_default_ms == DEFAULT_SENDSPIN_STATIC_DELAY


async def test_a_client_added_again_on_the_same_bridge_keeps_its_first_settings(
    scenario: Scenario,
) -> None:
    """The controls and default delay of a Cast bridge outlast a new hello on its client."""
    await _bridged_device(scenario, "chromecast", _hello("Speaker Bridge", []))
    sendspin: Any = scenario.provider("sendspin")
    sendspin.register_bridge_static_delay_default(BRIDGE_ID, 330)
    hello = _hello("Sendspin Cast Receiver", [PlayerCommand.VOLUME, PlayerCommand.MUTE])
    sendspin.clients[BRIDGE_ID].info = sendspin.clients[BRIDGE_ID].info_or_none = hello

    await sendspin._handle_client_added(BRIDGE_ID, sendspin._begin_client_event(BRIDGE_ID))
    await scenario.settle()

    bridge = scenario.mass.players.get_protocol(BRIDGE_ID)
    assert isinstance(bridge, SendspinPlayer)
    assert bridge.underlying_protocol_id != AIRPLAY_ID
    assert not bridge.supported_features & {PlayerFeature.VOLUME_SET, PlayerFeature.VOLUME_MUTE}
    assert bridge.static_delay_default_ms == 330


async def test_a_static_delay_the_user_set_follows_the_bridge_that_carries_the_output(
    scenario: Scenario,
) -> None:
    """
    Each bridge that carries the output of a device brings back the static delay set for it.

    The output moves by each way a bridge takes it over: registering the client after the
    other bridge removed it, registering it while the other bridge holds it, and claiming it.
    """
    sendspin, cast = await _airplay_and_cast_device(scenario)
    cast_id = cast.protocol_id
    scenario.mass.config.set_protocol_value(BRIDGE_ID, CONF_SENDSPIN_STATIC_DELAY, 120)

    await _cast_takes_over(scenario, sendspin, cast)

    bridge = scenario.mass.players.get_protocol(BRIDGE_ID)
    assert isinstance(bridge, SendspinPlayer)
    assert bridge.underlying_protocol_id == cast_id
    # none set for Cast: its own default applies
    assert _stored_delays(scenario) == (cast_id, None, {AIRPLAY_ID: 120})
    assert bridge.static_delay_default_ms == 330

    scenario.mass.config.set_protocol_value(BRIDGE_ID, CONF_SENDSPIN_STATIC_DELAY, 80)
    await _airplay_takes_over(scenario, sendspin)

    bridge = scenario.mass.players.get_protocol(BRIDGE_ID)
    assert bridge is not None
    assert bridge.underlying_protocol_id == AIRPLAY_ID
    assert _stored_delays(scenario) == (AIRPLAY_ID, 120, {cast_id: 80})

    # AirPlay switched off again, while the Cast receiver app stayed connected
    await _claim(scenario, sendspin, cast)

    bridge = scenario.mass.players.get_protocol(BRIDGE_ID)
    assert bridge is not None
    assert bridge.underlying_protocol_id == cast_id
    assert _stored_delays(scenario) == (cast_id, 80, {AIRPLAY_ID: 120})


async def test_a_stored_static_delay_belongs_to_the_bridge_that_carried_the_output(
    scenario: Scenario,
) -> None:
    """The only static delay stored is put aside for AirPlay once Cast takes over after a restart."""
    await _airplay_and_cast_device(scenario)
    scenario.mass.config.set_protocol_value(BRIDGE_ID, CONF_SENDSPIN_STATIC_DELAY, 120)
    assert _stored_delays(scenario) == (AIRPLAY_ID, 120, None)

    await scenario.restart()
    sendspin = _sendspin(scenario)
    cast = _carrier(scenario, "chromecast")
    await scenario.mass.players.register(cast)
    await scenario.settle()
    await _set_up_bridge(scenario, sendspin, cast)

    assert _stored_delays(scenario) == (cast.protocol_id, None, {AIRPLAY_ID: 120})


async def test_the_bridge_that_carries_the_output_leaves_the_static_delays_alone(
    scenario: Scenario,
) -> None:
    """A client the AirPlay bridge registers again, while held or after it left, keeps its delays."""
    sendspin, cast = await _airplay_and_cast_device(scenario)
    airplay = scenario.mass.players.get_protocol(AIRPLAY_ID)
    assert airplay is not None
    scenario.mass.config.set_protocol_value(BRIDGE_ID, CONF_SENDSPIN_STATIC_DELAY, 120)
    _set_aside(scenario, {cast.protocol_id: 80})
    record = deepcopy(scenario.mass.config.get_protocol_record(BRIDGE_ID))

    sendspin.hold_bridge_client(BRIDGE_ID, AIRPLAY_ID)
    await _set_up_bridge(scenario, sendspin, airplay)
    await scenario.mass.players.unregister(BRIDGE_ID)
    await _set_up_bridge(scenario, sendspin, airplay)

    assert scenario.mass.players.get_protocol(BRIDGE_ID) is not None
    assert scenario.mass.config.get_protocol_record(BRIDGE_ID) == record


async def test_a_take_over_handled_twice_before_it_attaches_hands_the_delays_over_once(
    scenario: Scenario,
) -> None:
    """A second refresh that starts before the first attached leaves the delays handed over."""
    sendspin, cast = await _airplay_and_cast_device(scenario)
    cast_id = cast.protocol_id
    scenario.mass.config.set_protocol_value(BRIDGE_ID, CONF_SENDSPIN_STATIC_DELAY, 120)
    _set_aside(scenario, {cast_id: 80})
    sendspin.register_bridge_identifiers(BRIDGE_ID, {IdentifierType.CAST_UUID: str(CAST_UUID)})

    async with scenario.mass.players._register_lock:
        # the Cast bridge registers the client the AirPlay bridge holds, which refreshes it
        sendspin.hold_bridge_client(BRIDGE_ID, cast_id)
        sendspin.refresh_bridge_client(BRIDGE_ID)
    await scenario.settle()

    bridge = scenario.mass.players.get_protocol(BRIDGE_ID)
    assert bridge is not None
    assert bridge.underlying_protocol_id == cast_id
    assert _stored_delays(scenario) == (cast_id, 80, {AIRPLAY_ID: 120})


async def test_a_bridge_client_of_another_id_leaves_the_static_delay_alone(
    scenario: Scenario,
) -> None:
    """A Cast bridge that registers a client id of its own (another MAC address) moves nothing."""
    sendspin, cast = await _airplay_and_cast_device(scenario)
    scenario.mass.config.set_protocol_value(BRIDGE_ID, CONF_SENDSPIN_STATIC_DELAY, 120)
    record = deepcopy(scenario.mass.config.get_protocol_record(BRIDGE_ID))
    other_id = "spb_001122334455"
    sendspin.register_bridge_identifiers(other_id, {IdentifierType.CAST_UUID: str(CAST_UUID)})
    sendspin.register_bridge_underlying_protocol(other_id, cast.protocol_id)

    await _connect(scenario, sendspin, replace(_hello("Speaker Bridge"), client_id=other_id))

    assert scenario.mass.config.get_protocol_record(BRIDGE_ID) == record
    assert scenario.mass.config.get_protocol_record(other_id) is None
