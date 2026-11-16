"""Tests for the lifecycle of AirPlay devices on a running server: discovery, loss, removal."""

from __future__ import annotations

import socket
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from music_assistant_models.enums import IdentifierType, ProtocolRole
from zeroconf import ServiceStateChange
from zeroconf.asyncio import AsyncServiceInfo

from music_assistant.controllers.players.protocol_linking import PLAYER_ID_PREFIX
from music_assistant.providers.airplay.constants import AIRPLAY_DISCOVERY_TYPE, CONF_STORED_VOLUME
from music_assistant.providers.airplay.control_player import AirPlayControlPlayer
from music_assistant.providers.airplay.player import AirPlayPlayer, GenericAirPlayPlayer
from music_assistant.providers.airplay.provider import AirPlayProvider
from music_assistant.providers.airplay.stream import AirPlayStream
from tests.scenarios.harness import Scenario

if TYPE_CHECKING:
    import pathlib
    from collections.abc import AsyncGenerator

    from music_assistant.models.player import Player

DEVICE_ID = "00:11:22:33:44:55"
PROTOCOL_ID = "ap001122334455"
ADDRESS = "192.168.1.50"
MEMBER_MAC = "00:11:22:33:44:66"


@pytest.fixture
async def scenario(tmp_path: pathlib.Path) -> AsyncGenerator[Scenario]:
    """
    Boot a real server with fake player providers; nothing reaches the network.

    :param tmp_path: Temporary directory for the server's storage.
    """
    scenario = Scenario(tmp_path)
    with (
        patch(
            "music_assistant.controllers.discovery.controller.DiscoveryController"
            ".async_find_mdns_service",
            AsyncMock(return_value=None),
        ),
        patch(
            "music_assistant.providers.airplay.provider.probe_audio_formats",
            AsyncMock(return_value=0),
        ),
    ):
        try:
            await scenario.start()
            yield scenario
        finally:
            await scenario.close()


@pytest.fixture
async def provider(scenario: Scenario) -> AirPlayProvider:
    """
    Return a real AirPlay provider loaded in the server, without its network services.

    :param scenario: The running server.
    """
    fake = await scenario.add_provider("airplay")
    provider = AirPlayProvider(scenario.mass, fake.manifest, fake.config)
    provider._bridge_manager = MagicMock(
        evaluate_bridge=AsyncMock(),
        remove_bridge=AsyncMock(),
        close=AsyncMock(),
        get_transport_command_target=MagicMock(return_value=None),
        get_bridge=MagicMock(return_value=None),
        stop_streaming=MagicMock(return_value=False),
    )
    provider.dashboards = MagicMock(unload=AsyncMock())
    provider._companion_info_by_address = {}
    provider._mrp_info_by_address = {}
    provider._announce_plans = {}
    provider._dacp_server = MagicMock()
    provider._dacp_info = None  # type: ignore[assignment]
    scenario.mass._providers[fake.instance_id] = provider
    return provider


def _service(
    name: str = "Receiver A",
    device_id: str = DEVICE_ID,
    address: str = ADDRESS,
    *,
    manufacturer: str = "Acme",
    model: str = "Receiver1,1",
    flags: str = "0x4",
) -> AsyncServiceInfo:
    """Return the _airplay._tcp record a receiver advertises."""
    return AsyncServiceInfo(
        AIRPLAY_DISCOVERY_TYPE,
        f"{name}.{AIRPLAY_DISCOVERY_TYPE}",
        addresses=[socket.inet_aton(address)],
        port=7000,
        properties={
            "deviceid": device_id,
            "manufacturer": manufacturer,
            "model": model,
            "features": "0x4A7FDFD5,0x3C177FDE",
            "flags": flags,
        },
    )


async def _announce(
    scenario: Scenario,
    provider: AirPlayProvider,
    info: AsyncServiceInfo,
    state_change: ServiceStateChange = ServiceStateChange.Added,
) -> AirPlayPlayer | None:
    """Hand an mDNS record to the provider as discovery does; return its protocol object."""
    await provider.on_mdns_service_state_change(info.name, state_change, info)
    await scenario.settle()
    device_id = str(info.decoded_properties["deviceid"])
    protocol_id = f"ap{device_id.replace(':', '').lower()}"
    return provider.get_player(protocol_id)


async def _lines(*lines: str) -> AsyncGenerator[str]:
    """Yield the stderr lines of a cliairplay process."""
    for line in lines:
        yield line


def _player_of(protocol: AirPlayPlayer | None) -> Player:
    """Return the player a protocol is attached to."""
    assert protocol is not None
    assert protocol.player is not None
    return protocol.player


async def test_a_new_receiver_is_an_audio_protocol_of_a_minted_player(
    scenario: Scenario, provider: AirPlayProvider
) -> None:
    """A receiver first seen now gets a player of its own, its AirPlay id as identifier."""
    protocol = await _announce(scenario, provider, _service())

    assert isinstance(protocol, GenericAirPlayPlayer)
    assert protocol.role == ProtocolRole.AUDIO
    player = _player_of(protocol)
    assert player.player_id.startswith(PLAYER_ID_PREFIX)
    assert player.player_id != PROTOCOL_ID
    assert player.available is True
    assert (IdentifierType.AIRPLAY_ID, PROTOCOL_ID) in protocol.identifiers
    assert (IdentifierType.MAC_ADDRESS, DEVICE_ID) in protocol.identifiers


async def test_the_stored_volume_comes_back_with_the_receiver(
    scenario: Scenario, provider: AirPlayProvider
) -> None:
    """The volume is stored on the protocol record and survives the device going away."""
    protocol = await _announce(scenario, provider, _service())
    player_id = _player_of(protocol).player_id
    assert protocol is not None

    await protocol.volume_set(33)

    config = scenario.mass.config
    assert config.get_protocol_value(PROTOCOL_ID, CONF_STORED_VOLUME) == 33
    assert config.get_player_value(player_id, CONF_STORED_VOLUME) is None

    await _announce(scenario, provider, _service(), ServiceStateChange.Removed)
    assert provider.get_player(PROTOCOL_ID) is None
    lost = scenario.player(player_id)
    assert lost is not None
    assert lost.available is False

    back = await _announce(scenario, provider, _service())

    assert back is not None
    assert back is not protocol
    assert back.volume_level == 33
    assert _player_of(back).player_id == player_id
    assert _player_of(back).available is True


async def test_an_attached_receiver_is_left_alone_by_a_repeated_record(
    scenario: Scenario, provider: AirPlayProvider
) -> None:
    """Discovery replays the records it holds; an attached receiver is not attached again."""
    protocol = await _announce(scenario, provider, _service())

    with patch.object(
        scenario.mass.players, "register", wraps=scenario.mass.players.register
    ) as spy:
        again = await _announce(scenario, provider, _service(name="Receiver A2"))

    spy.assert_not_called()
    assert again is protocol
    assert protocol is not None
    assert protocol.name == "Receiver A2"


async def test_a_mac_is_no_player_until_enabled(
    scenario: Scenario, provider: AirPlayProvider
) -> None:
    """A Mac is disabled by default: listed in the settings, a player once the user enables it."""
    mac = _service(name="Office Mac", manufacturer="Apple Inc.", model="MacBookPro18,3")

    assert await _announce(scenario, provider, mac) is None
    listed = [
        config for config in await scenario.player_configs() if config.default_name == "Office Mac"
    ]
    assert [config.enabled for config in listed] == [False]
    # nothing is set up for the device, and discovery offering it again does not attach it
    bridge_manager = provider.bridge_manager
    assert isinstance(bridge_manager, MagicMock)
    bridge_manager.evaluate_bridge.assert_not_awaited()
    with patch.object(
        scenario.mass.players, "register", wraps=scenario.mass.players.register
    ) as spy:
        assert await _announce(scenario, provider, mac) is None
    spy.assert_not_called()
    assert scenario.visible_players() == []

    await scenario.enable(listed[0].player_id)
    protocol = await _announce(scenario, provider, mac)

    assert _player_of(protocol).player_id == listed[0].player_id
    assert _player_of(protocol).available is True


async def test_a_removed_player_forgets_its_bridge_and_comes_back_as_a_new_player(
    scenario: Scenario, provider: AirPlayProvider
) -> None:
    """Removal reaches the protocol; a receiver still on the network returns with a new id."""
    protocol = await _announce(scenario, provider, _service())
    player_id = _player_of(protocol).player_id

    await scenario.remove(player_id)

    bridge_manager = provider.bridge_manager
    assert isinstance(bridge_manager, MagicMock)
    bridge_manager.remove_bridge.assert_awaited_with(PROTOCOL_ID, permanent=True)
    assert scenario.player(player_id) is None
    assert provider.get_player(PROTOCOL_ID) is None

    back = await _announce(scenario, provider, _service())

    assert _player_of(back).player_id not in (player_id, PROTOCOL_ID)


async def test_two_receivers_on_one_host_stay_two_players(
    scenario: Scenario, provider: AirPlayProvider
) -> None:
    """
    Two AirPlay receivers on one host stay two players.

    Software receivers advertise a locally administered device id, so both resolve to
    the hardware MAC address of their host, which they then share.
    """
    scenario.arp[ADDRESS] = "00:11:22:33:44:01"
    first = await _announce(scenario, provider, _service("Zone A", "0A:11:22:33:44:0A"))
    second = await _announce(scenario, provider, _service("Zone B", "0A:11:22:33:44:0B"))

    assert first is not None
    assert second is not None
    assert (IdentifierType.MAC_ADDRESS, "00:11:22:33:44:01") in first.identifiers
    assert (IdentifierType.MAC_ADDRESS, "00:11:22:33:44:01") in second.identifiers
    assert _player_of(first) is not _player_of(second)


async def test_a_rejected_password_is_published_to_clients(
    scenario: Scenario, provider: AirPlayProvider
) -> None:
    """The setup requirement a rejected password brings reaches the player state."""
    protocol = await _announce(scenario, provider, _service(flags="0x80"))
    assert protocol is not None
    player_id = _player_of(protocol).player_id
    protocol._store_device_password("wrong")
    protocol.update_state()
    await scenario.settle()
    before = scenario.player(player_id)
    assert before is not None
    assert before.needs_setup is False

    protocol.set_password_invalid(True)
    await scenario.settle()

    after = scenario.player(player_id)
    assert after is not None
    assert after.needs_setup is True
    assert after.setup_reason == "password_required"
    assert after.available is False


async def test_a_member_whose_stream_dies_leaves_the_airplay_group_for_its_own_protocol(
    scenario: Scenario, provider: AirPlayProvider
) -> None:
    """A device that falls out of an AirPlay group gets its volume on its own protocol again."""
    native = (await scenario.add_provider("sonos")).protocol(
        "RINCON_M", "Speaker M", identifiers={IdentifierType.MAC_ADDRESS: MEMBER_MAC}
    )
    await scenario.register(native)
    member_airplay = await _announce(
        scenario, provider, _service("Speaker M", MEMBER_MAC, "192.168.1.51")
    )
    leader = _player_of(await _announce(scenario, provider, _service()))
    member = _player_of(member_airplay)
    assert member_airplay is not None
    assert member.own_protocol is native
    await scenario.mass.players.cmd_set_members(
        leader.player_id, player_ids_to_add=[member.player_id]
    )
    await scenario.settle()
    assert member.player_id in leader.group_members
    assert member.active_output_protocol == member_airplay.protocol_id

    scenario.expect_log("cliairplay process stopped unexpectedly")
    stream = AirPlayStream(member_airplay)
    member_airplay.stream = stream
    stream._cli_proc = MagicMock(iter_stderr=MagicMock(return_value=_lines("final log line")))
    with (
        patch.object(member_airplay, "schedule_group_rejoin"),
        patch.object(stream.commands_pipe, "remove", new_callable=AsyncMock),
    ):
        await stream._stderr_reader()
        await scenario.idle()

    assert member.player_id not in leader.group_members
    assert member.synced_to is None
    assert member.active_output_protocol in (None, "native")
    native.commands.clear()
    await scenario.mass.players.cmd_volume_set(member.player_id, 25)
    assert ("volume_set", 25) in native.commands


async def test_an_apple_device_taken_off_its_player_connects_its_controls_again(
    scenario: Scenario, provider: AirPlayProvider
) -> None:
    """The control protocol of an Apple device reconnects its services on its new player."""
    connect = AsyncMock(return_value=False)
    with patch.object(AirPlayControlPlayer, "_connect_control_services", connect):
        protocol = await _announce(
            scenario, provider, _service("Apple TV", manufacturer="Apple", model="Apple TV 4K")
        )
        cast_receiver = (await scenario.add_provider("chromecast")).protocol(
            "cast_tv", "Apple TV", role="audio", identifiers={IdentifierType.MAC_ADDRESS: DEVICE_ID}
        )
        await scenario.register(cast_receiver)
        assert isinstance(protocol, AirPlayControlPlayer)
        old_player_id = _player_of(protocol).player_id
        assert scenario.player_of(cast_receiver) == old_player_id
        connect.assert_awaited()
        connect.reset_mock()

        new_player_id = await scenario.mass.players.unlink_protocol(PROTOCOL_ID)
        await scenario.idle()

        assert _player_of(protocol).player_id == new_player_id != old_player_id
        connect.assert_awaited()
