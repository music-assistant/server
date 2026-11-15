"""Tests for which player a Sendspin client belongs to, on a running server."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import MagicMock

import pytest
from aiosendspin.models.core import ClientHelloPayload
from aiosendspin.models.core import DeviceInfo as SendspinDeviceInfo
from aiosendspin.noise.trust_store import PskCategory
from aiosendspin.server.roles.player.events import OutputDelayChangedEvent
from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import ConfigEntryType, EventType, IdentifierType, ProtocolRole

from music_assistant.controllers.players.protocol_linking import PLAYER_ID_PREFIX
from music_assistant.models.player import Player
from music_assistant.providers.sendspin.constants import CONF_SENDSPIN_STATIC_DELAY
from music_assistant.providers.sendspin.player import SendspinPlayer
from music_assistant.providers.sendspin.provider import SendspinProvider

if TYPE_CHECKING:
    from tests.scenarios.harness import FakePlayerProvider, Scenario

MAC = "a0:b7:65:00:11:22"
# an ESPHome device on old firmware connects with its MAC address as client id
MAC_CLIENT_ID = MAC
# the same device on encrypted firmware: its key id
KEY_CLIENT_ID = "jyu3dS9mC2xHq0yGm7Hn3oUuZz8kPq1wLr5tXv0aB4c"


@pytest.fixture(autouse=True)
def _no_sendspin_entries(monkeypatch: pytest.MonkeyPatch) -> list[ConfigEntry]:
    """Keep the config entries of a client to the static delay, without the pairing state."""
    entries = [
        ConfigEntry(
            key=CONF_SENDSPIN_STATIC_DELAY,
            type=ConfigEntryType.INTEGER,
            required=False,
            default_value=0,
        )
    ]

    async def _entries(_self: SendspinPlayer) -> list[ConfigEntry]:
        return list(entries)

    monkeypatch.setattr(SendspinPlayer, "get_config_entries", _entries)
    return entries


async def _sendspin(scenario: Scenario) -> FakePlayerProvider:
    """Add a Sendspin provider instance whose clients are real Sendspin protocols."""
    provider = await scenario.add_provider("sendspin")
    clients: dict[str, Any] = {}
    cast("Any", provider).clients = clients
    cast("Any", provider).server_api = SimpleNamespace(get_client=clients.get)
    cast("Any", provider).is_virtual_player = lambda _client_id: False
    cast("Any", provider).is_held_unapproved = lambda _client_id: False
    cast("Any", provider)._device_role = SendspinProvider._device_role.__get__(provider)
    return provider


def _client(
    provider: FakePlayerProvider,
    client_id: str,
    *,
    name: str = "esp32-loud",
    mac_address: str | None = MAC,
    product_name: str = "ESP32 Speaker",
) -> SendspinPlayer:
    """
    Connect a client and return the Sendspin protocol the provider creates for it.

    :param provider: The Sendspin provider.
    :param client_id: The client id the client connects with.
    :param name: The name it reports.
    :param mac_address: The MAC address it reports in its hello.
    :param product_name: The product it reports.
    """
    hello = ClientHelloPayload(
        client_id=client_id,
        name=name,
        version=1,
        supported_roles=["player@v1"],
        device_info=SendspinDeviceInfo(
            product_name=product_name, manufacturer="Espressif", mac_address=mac_address
        ),
    )
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
    cast("Any", provider).clients[client_id] = client
    protocol = SendspinPlayer(cast("SendspinProvider", provider), client_id, initial_hello=hello)
    protocol._attr_role = cast("SendspinProvider", provider)._device_role(protocol)
    return protocol


async def _register(scenario: Scenario, protocol: SendspinPlayer) -> Player:
    """Register a protocol, as the provider does for a connected client; return its player."""
    await scenario.mass.players.register(protocol)
    await scenario.settle()
    assert protocol.player is not None
    return protocol.player


async def _unregister(scenario: Scenario, protocol: SendspinPlayer) -> None:
    """Unregister a protocol, as the provider does for a client that disconnected."""
    await scenario.mass.players.unregister(protocol.protocol_id)
    await scenario.settle()


async def test_device_that_comes_back_under_its_key_id_keeps_its_player_and_settings(
    scenario: Scenario,
) -> None:
    """
    An ESPHome device moving from its MAC address id to its key id stays the same player.

    Its new protocol takes over the record of the old one, so its settings come along, and
    the stale record of the old id goes.
    """
    sendspin = await _sendspin(scenario)
    by_mac = _client(sendspin, MAC_CLIENT_ID)
    player_id = (await _register(scenario, by_mac)).player_id
    await scenario.configure_player(player_id, name="Kitchen Speaker")
    scenario.mass.config.set_protocol_value(MAC_CLIENT_ID, CONF_SENDSPIN_STATIC_DELAY, 80)
    await _unregister(scenario, by_mac)

    by_key = _client(sendspin, KEY_CLIENT_ID)
    player = await _register(scenario, by_key)

    assert player.player_id == player_id
    assert player.name == "Kitchen Speaker"
    assert scenario.mass.config.get_protocol_value(KEY_CLIENT_ID, CONF_SENDSPIN_STATIC_DELAY) == 80
    assert scenario.mass.config.get_protocol_record(MAC_CLIENT_ID) is None
    assert scenario.mass.config.get_protocol_ids_of_player(player_id) == [KEY_CLIENT_ID]


async def test_a_device_with_nothing_else_is_the_native_protocol_of_its_player(
    scenario: Scenario,
) -> None:
    """A client seen before stands for its device, as the native protocol of its player."""
    sendspin = await _sendspin(scenario)
    first = _client(sendspin, KEY_CLIENT_ID)
    assert first.role == ProtocolRole.AUDIO
    player_id = (await _register(scenario, first)).player_id
    await _unregister(scenario, first)

    again = _client(sendspin, KEY_CLIENT_ID)
    player = await _register(scenario, again)

    assert again.role == ProtocolRole.NATIVE
    assert player.player_id == player_id
    assert player.own_protocol is again


@pytest.mark.parametrize(
    ("mac_address", "role"),
    [
        (MAC, ProtocolRole.AUDIO),
        (None, ProtocolRole.NATIVE),
        # locally administered: no address of the hardware
        ("a2:b7:65:00:11:22", ProtocolRole.NATIVE),
    ],
    ids=["hardware_mac", "no_mac", "local_mac"],
)
async def test_a_new_client_gets_its_player_at_once(
    scenario: Scenario, mac_address: str | None, role: ProtocolRole
) -> None:
    """
    A client seen for the first time is on a player right away.

    One with a hardware MAC address is an audio protocol, so the native protocol of its
    device can still join; any other is the native protocol of a player of its own, as no
    other identifier can tie it to a device.
    """
    sendspin = await _sendspin(scenario)
    first = _client(
        sendspin,
        "sendspin-cli-1111",
        name="Pi Kitchen",
        mac_address=mac_address,
        product_name="sendspin-cli",
    )

    player = await _register(scenario, first)

    assert first.role == role
    assert player.get_role(first.protocol_id) == role


async def test_two_clients_on_one_host_stay_two_players(scenario: Scenario) -> None:
    """A second software client on a host does not take over the offline first one's player."""
    sendspin = await _sendspin(scenario)
    first = _client(sendspin, "sendspin-cli-1111", name="Pi Kitchen", product_name="sendspin-cli")
    first_id = (await _register(scenario, first)).player_id
    await scenario.configure_player(first_id, name="Kitchen")
    await _unregister(scenario, first)

    second = _client(sendspin, "sendspin-cli-2222", name="Pi Office", product_name="sendspin-cli")
    second_player = await _register(scenario, second)

    assert second_player.player_id != first_id
    assert scenario.mass.config.get_protocol_ids_of_player(first_id) == ["sendspin-cli-1111"]
    first_player = scenario.mass.players.get_player(first_id)
    assert first_player is not None
    assert first_player.name == "Kitchen"


async def test_a_client_of_a_device_with_another_native_is_an_output_of_its_player(
    scenario: Scenario,
) -> None:
    """The Sendspin client of a device with a native protocol of another provider joins it."""
    squeezelite = await scenario.add_provider("squeezelite")
    native = squeezelite.protocol(
        "a0b765001122", "ESP32 Loud", identifiers={IdentifierType.MAC_ADDRESS: MAC}
    )
    await scenario.register(native)
    player_id = scenario.player_of(native)
    sendspin = await _sendspin(scenario)

    first = _client(sendspin, KEY_CLIENT_ID)
    assert (await _register(scenario, first)).player_id == player_id
    await _unregister(scenario, first)
    again = _client(sendspin, KEY_CLIENT_ID)
    player = await _register(scenario, again)

    assert again.role == ProtocolRole.AUDIO
    assert player.player_id == player_id
    assert player.own_protocol is not again


async def test_a_web_player_is_its_own_player_with_nothing_stored(scenario: Scenario) -> None:
    """
    A browser player has its client id as player id, and nothing is stored for it.

    A delay it reports is kept in the config it holds, and the player goes with it.
    """
    sendspin = await _sendspin(scenario)
    browser = _client(
        sendspin, "web-3f2a9c", name="Web Browser", mac_address=None, product_name="Web Browser"
    )
    assert browser.session_only is True
    browser._attr_role = ProtocolRole.NATIVE
    player = await _register(scenario, browser)

    browser.event_cb(MagicMock(), OutputDelayChangedEvent(output_delay_ms=150))

    assert player.player_id == "web-3f2a9c"
    assert scenario.mass.config.get_protocol_value("web-3f2a9c", CONF_SENDSPIN_STATIC_DELAY) == 150
    assert browser.config.get_value(CONF_SENDSPIN_STATIC_DELAY) == 150
    assert await scenario.stored_config_mentioning("web-3f2a9c") == []

    scenario.clear_events()
    await _unregister(scenario, browser)

    assert scenario.mass.players.get_player("web-3f2a9c") is None
    assert [event.object_id for event in scenario.events(EventType.PLAYER_REMOVED)] == [
        "web-3f2a9c"
    ]
    assert await scenario.stored_config_mentioning("web-3f2a9c") == []


async def test_a_setting_of_a_new_device_is_kept_on_its_protocol(scenario: Scenario) -> None:
    """
    A device first seen now gets a player of its own id; its Sendspin settings are its protocol's.

    The setting is stored on the protocol record, not on the player record, and the client
    has it again when it comes back.
    """
    sendspin = await _sendspin(scenario)
    first = _client(sendspin, KEY_CLIENT_ID)
    player_id = (await _register(scenario, first)).player_id
    assert player_id.startswith(PLAYER_ID_PREFIX)
    assert player_id != KEY_CLIENT_ID
    # the delay the device reports becomes its setting
    first.event_cb(MagicMock(), OutputDelayChangedEvent(output_delay_ms=80))
    await _unregister(scenario, first)

    again = _client(sendspin, KEY_CLIENT_ID)
    await _register(scenario, again)

    record = scenario.mass.config.get_protocol_record(KEY_CLIENT_ID) or {}
    assert record["values"][CONF_SENDSPIN_STATIC_DELAY] == 80
    assert scenario.mass.config.get_player_value(player_id, CONF_SENDSPIN_STATIC_DELAY) is None
    assert again.config.get_value(CONF_SENDSPIN_STATIC_DELAY) == 80
