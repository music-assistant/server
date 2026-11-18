"""Tests for the lifecycle of AirPlay devices on a running server: discovery, loss, removal."""

from __future__ import annotations

import socket
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from music_assistant_models.enums import (
    ChannelLayout,
    FlowStepType,
    IdentifierType,
    PlayerType,
    ProtocolRole,
)
from zeroconf import ServiceStateChange
from zeroconf.asyncio import AsyncServiceInfo

from music_assistant.controllers.players.protocol_linking import PLAYER_ID_PREFIX
from music_assistant.providers.airplay.constants import (
    AIRPLAY_DISCOVERY_TYPE,
    CONF_AIRPLAY_CREDENTIALS,
    CONF_HOMEPODS_ONLY,
    CONF_STORED_VOLUME,
    RAOP_DISCOVERY_TYPE,
)
from music_assistant.providers.airplay.control_player import AirPlayControlPlayer
from music_assistant.providers.airplay.player import AirPlayPlayer, GenericAirPlayPlayer
from music_assistant.providers.airplay.provider import AirPlayProvider
from music_assistant.providers.airplay.stream import AirPlayStream
from tests.scenarios.harness import Scenario

if TYPE_CHECKING:
    import pathlib
    from collections.abc import AsyncGenerator, Iterator

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
    provider._speakers = {}
    provider._set_carriers = {}
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


# A home theater set: an Apple TV with a HomePod (or a stereo pair of them) as its speakers,
# with the TXT keys tvOS 27 advertises for it (ids, names and addresses made up).
SET_ID = "3F2A9C1E-7B44-4D0E-9A61-2C8D5E7F1A30"
APPLE_TV_ID = "AA:BB:CC:00:11:01"
APPLE_TV_PROTOCOL_ID = "apaabbcc001101"
HOMEPOD_ID = "AA:BB:CC:00:11:02"
HOMEPOD_PROTOCOL_ID = "apaabbcc001102"
SECOND_HOMEPOD_ID = "AA:BB:CC:00:11:03"
SECOND_HOMEPOD_PROTOCOL_ID = "apaabbcc001103"


def _set_device(
    name: str,
    device_id: str,
    address: str,
    position: int | None,
    *,
    model: str,
    flags: str,
    size: int = 2,
    set_id: str = SET_ID,
) -> AsyncServiceInfo:
    """
    Return the _airplay._tcp record of a device of a home theater set.

    :param position: Place in the set (0 for the Apple TV), None for a device outside a set.
    """
    properties = {
        "deviceid": device_id,
        "model": model,
        "features": "0x4A7FDFD5,0x3C177FDE",
        "flags": flags,
        "gid": set_id,
        "srcvers": "1005.13.2",
    }
    if position is not None:
        properties |= {
            "gpn": "Study",
            "psgid": set_id,
            "psgsz": str(size),
            "psgtp": "1",
            "pgm": "3",
            "pgmid": str(position),
        }
    return AsyncServiceInfo(
        AIRPLAY_DISCOVERY_TYPE,
        f"{name}.{AIRPLAY_DISCOVERY_TYPE}",
        addresses=[socket.inet_aton(address)],
        port=7000,
        properties=properties,
    )


def _apple_tv(*, size: int = 2, set_id: str = SET_ID) -> AsyncServiceInfo:
    """Return the record of the Apple TV that carries the set (it advertises legacy pairing)."""
    return _set_device(
        "Apple TV Study",
        APPLE_TV_ID,
        "192.168.1.60",
        0,
        model="AppleTV14,1",
        flags="0x644",
        size=size,
        set_id=set_id,
    )


def _homepod(
    position: int | None = 1,
    *,
    device_id: str = HOMEPOD_ID,
    address: str = "192.168.1.61",
    size: int = 2,
) -> AsyncServiceInfo:
    """Return the record of a HomePod, a speaker of the set unless position is None."""
    return _set_device(
        f"HomePod {device_id[-2:]}",
        device_id,
        address,
        position,
        model="AudioAccessory11,1",
        flags="0xa0c04",
        size=size,
    )


def _homepod_raop(device_id: str = HOMEPOD_ID) -> AsyncServiceInfo:
    """Return the _raop._tcp record of a HomePod, which names no set."""
    raw_id = device_id.replace(":", "")
    return AsyncServiceInfo(
        RAOP_DISCOVERY_TYPE,
        f"{raw_id}@HomePod.{RAOP_DISCOVERY_TYPE}",
        addresses=[socket.inet_aton("192.168.1.61")],
        port=7000,
        properties={"am": "AudioAccessory11,1", "ft": "0x4A7FDFD5,0x3C177FDE", "sf": "0xa0c04"},
    )


async def _homepod_gone(provider: AirPlayProvider, device_id: str = HOMEPOD_ID) -> None:
    """Hand the provider the goodbye of a HomePod's RAOP record."""
    await provider.on_mdns_service_state_change(
        _homepod_raop(device_id).name, ServiceStateChange.Removed, None
    )


@pytest.fixture
def apple_controls() -> Iterator[AsyncMock]:
    """Keep the control channels of the Apple devices off the network."""
    with patch.object(
        AirPlayControlPlayer, "_connect_control_services", AsyncMock(return_value=False)
    ) as connect:
        yield connect


@pytest.mark.usefixtures("apple_controls")
@pytest.mark.parametrize("apple_tv_first", [True, False])
async def test_an_apple_tv_and_its_homepod_are_one_player_named_after_the_room(
    scenario: Scenario, provider: AirPlayProvider, apple_tv_first: bool
) -> None:
    """Whichever record comes first, the HomePod is a speaker of the Apple TV, no player."""
    records = [_apple_tv(), _homepod()] if apple_tv_first else [_homepod(), _apple_tv()]
    for record in records:
        await _announce(scenario, provider, record)

    assert provider.get_player(HOMEPOD_PROTOCOL_ID) is None
    apple_tv = provider.get_player(APPLE_TV_PROTOCOL_ID)
    assert apple_tv is not None
    assert [speaker.protocol_id for speaker in apple_tv.speakers] == [HOMEPOD_PROTOCOL_ID]
    state = scenario.player(_player_of(apple_tv).player_id)
    assert state is not None
    assert state.name == "Study"
    # one HomePod makes no multi-channel player
    assert state.type == PlayerType.PLAYER
    assert state.channel_layout is None
    # the Apple TV takes part in the sessions of the room, so it is paired as a plain one is
    assert state.setup_reason == "pairing_required"
    assert state.available is False
    assert [player.name for player in scenario.visible_players()] == ["Study"]


@pytest.mark.usefixtures("apple_controls")
async def test_an_apple_tv_with_a_pair_of_homepods_is_a_stereo_player(
    scenario: Scenario, provider: AirPlayProvider
) -> None:
    """A stereo pair as the speakers of an Apple TV makes it a multi-channel player."""
    await _announce(scenario, provider, _apple_tv(size=3))
    await _announce(
        scenario,
        provider,
        _homepod(2, device_id=SECOND_HOMEPOD_ID, address="192.168.1.62", size=3),
    )
    await _announce(scenario, provider, _homepod(1, size=3))

    apple_tv = provider.get_player(APPLE_TV_PROTOCOL_ID)
    assert apple_tv is not None
    assert [speaker.protocol_id for speaker in apple_tv.speakers] == [
        HOMEPOD_PROTOCOL_ID,
        SECOND_HOMEPOD_PROTOCOL_ID,
    ]
    state = scenario.player(_player_of(apple_tv).player_id)
    assert state is not None
    assert state.type == PlayerType.MULTI_CHANNEL
    assert state.channel_layout == ChannelLayout.STEREO


@pytest.mark.usefixtures("apple_controls")
async def test_the_player_a_homepod_had_goes_once_its_apple_tv_names_it(
    scenario: Scenario, provider: AirPlayProvider
) -> None:
    """
    The player of a HomePod that joined a set is removed, on what both records say only.

    A HomePod that names a set nobody carries yet, or an Apple TV of another set, removes
    nothing; nor does discovery offering the HomePod again bring its player back.
    """
    old_player_id = _player_of(await _announce(scenario, provider, _homepod(None))).player_id

    # the HomePod joins a set while the server runs, before its Apple TV is seen
    assert await _announce(scenario, provider, _homepod()) is None
    assert await scenario.player_config(old_player_id) is not None
    await _announce(scenario, provider, _apple_tv(set_id="0B6E3D2C-1A90-4F77-8E25-6D4C3B2A1908"))
    assert await scenario.player_config(old_player_id) is not None

    await _announce(scenario, provider, _apple_tv())

    assert scenario.player(old_player_id) is None
    assert await scenario.player_config(old_player_id) is None
    assert await _announce(scenario, provider, _homepod()) is None
    assert await scenario.player_config(old_player_id) is None


@pytest.mark.usefixtures("apple_controls")
async def test_a_homepod_that_is_gone_keeps_the_player_it_had(
    scenario: Scenario, provider: AirPlayProvider
) -> None:
    """A speaker that is not on the network is not named by its set, so nothing is removed."""
    old_player_id = _player_of(await _announce(scenario, provider, _homepod(None))).player_id
    assert await _announce(scenario, provider, _homepod()) is None
    await _homepod_gone(provider)
    await scenario.settle()

    apple_tv = await _announce(scenario, provider, _apple_tv())

    assert apple_tv is not None
    assert apple_tv.speakers == []
    assert await scenario.player_config(old_player_id) is not None


@pytest.mark.usefixtures("apple_controls")
async def test_a_disabled_player_of_a_homepod_is_removed_once_its_set_names_it(
    scenario: Scenario, provider: AirPlayProvider
) -> None:
    """The HomePod is a speaker of its set also while the user had disabled its old player."""
    old_player_id = _player_of(await _announce(scenario, provider, _homepod(None))).player_id
    await scenario.disable(old_player_id)

    await _announce(scenario, provider, _homepod())
    apple_tv = await _announce(scenario, provider, _apple_tv())

    assert apple_tv is not None
    assert [speaker.protocol_id for speaker in apple_tv.speakers] == [HOMEPOD_PROTOCOL_ID]
    assert await scenario.player_config(old_player_id) is None


@pytest.mark.usefixtures("apple_controls")
@pytest.mark.parametrize("apple_tv_first", [True, False])
async def test_the_homepod_of_a_disabled_apple_tv_is_a_player_of_its_own(
    scenario: Scenario, provider: AirPlayProvider, apple_tv_first: bool
) -> None:
    """Disabling the player of the set gives its HomePod back; enabling it takes it again."""
    apple_tv_player_id = _player_of(await _announce(scenario, provider, _apple_tv())).player_id
    await _announce(scenario, provider, _homepod())
    assert provider.get_player(HOMEPOD_PROTOCOL_ID) is None

    await scenario.disable(apple_tv_player_id)

    homepod_player_id = _player_of(provider.get_player(HOMEPOD_PROTOCOL_ID)).player_id
    # discovery offering both again changes nothing while the Apple TV stays disabled
    assert await _announce(scenario, provider, _apple_tv()) is None
    assert _player_of(await _announce(scenario, provider, _homepod())).player_id == (
        homepod_player_id
    )

    await scenario.enable(apple_tv_player_id)
    # discovery replays the records in any order
    records = [_apple_tv(), _homepod()] if apple_tv_first else [_homepod(), _apple_tv()]
    for record in records:
        await _announce(scenario, provider, record)

    assert provider.get_player(HOMEPOD_PROTOCOL_ID) is None
    assert await scenario.player_config(homepod_player_id) is None


@pytest.mark.usefixtures("apple_controls")
async def test_a_homepod_seen_before_its_disabled_apple_tv_is_a_player_of_its_own(
    scenario: Scenario, provider: AirPlayProvider
) -> None:
    """After a restart the HomePod can be seen first: the disabled Apple TV gives it back."""
    apple_tv_player_id = _player_of(await _announce(scenario, provider, _apple_tv())).player_id
    await scenario.disable(apple_tv_player_id)
    # what a restarted provider knows: nothing about the set yet
    provider._set_carriers.clear()

    assert await _announce(scenario, provider, _homepod()) is None
    await _announce(scenario, provider, _apple_tv())

    assert provider.get_player(HOMEPOD_PROTOCOL_ID) is not None
    assert provider._speakers == {}


@pytest.mark.usefixtures("apple_controls")
async def test_a_homepod_that_leaves_its_set_is_found_as_a_new_player(
    scenario: Scenario, provider: AirPlayProvider
) -> None:
    """A HomePod whose record no longer names the set is a player of its own again."""
    apple_tv = await _announce(scenario, provider, _apple_tv())
    await _announce(scenario, provider, _homepod())

    homepod = await _announce(scenario, provider, _homepod(None))

    assert homepod is not None
    assert _player_of(homepod) is not _player_of(apple_tv)
    assert apple_tv is not None
    assert apple_tv.speakers == []


@pytest.mark.usefixtures("apple_controls")
async def test_the_homepod_of_an_apple_tv_that_is_gone_stays_a_speaker(
    scenario: Scenario, provider: AirPlayProvider
) -> None:
    """Without its Apple TV the room is unavailable, and the HomePod is still no player."""
    apple_tv_player_id = _player_of(await _announce(scenario, provider, _apple_tv())).player_id
    await _announce(scenario, provider, _homepod())

    await provider.on_mdns_service_state_change(
        f"AABBCC001101@Apple TV Study.{RAOP_DISCOVERY_TYPE}", ServiceStateChange.Removed, None
    )
    await scenario.settle()

    room = scenario.player(apple_tv_player_id)
    assert room is not None
    assert room.available is False
    assert await _announce(scenario, provider, _homepod()) is None


@pytest.mark.usefixtures("apple_controls")
async def test_a_homepod_that_goes_offline_keeps_the_stream_it_plays(
    scenario: Scenario, provider: AirPlayProvider
) -> None:
    """
    A speaker whose records go away plays on for its set until its stream ends.

    A stream that really lost its device ends by itself, and stops its set with it.
    """
    apple_tv = await _announce(scenario, provider, _apple_tv())
    await _announce(scenario, provider, _homepod())
    speaker = provider._speakers[HOMEPOD_PROTOCOL_ID]
    session = MagicMock(remove_client=AsyncMock())
    speaker.stream = MagicMock(running=True, session=session)

    await _homepod_gone(provider)

    assert apple_tv is not None
    assert apple_tv.speakers == [speaker]
    session.remove_client.assert_not_awaited()
    speaker.stream.running = False
    assert apple_tv.speakers == []


@pytest.mark.usefixtures("apple_controls")
@pytest.mark.parametrize("homepods_only", [False, True])
async def test_a_homepod_that_goes_offline_takes_the_commands_of_its_set_while_it_plays(
    scenario: Scenario, provider: AirPlayProvider, homepods_only: bool
) -> None:
    """The volume and a stop of a set reach a speaker whose records went away while it plays."""
    apple_tv = await _announce(scenario, provider, _apple_tv())
    await _announce(scenario, provider, _homepod())
    assert apple_tv is not None
    scenario.mass.config.set_protocol_value(APPLE_TV_PROTOCOL_ID, CONF_HOMEPODS_ONLY, homepods_only)
    speaker = provider._speakers[HOMEPOD_PROTOCOL_ID]
    session = MagicMock(stop=AsyncMock(), remove_client=AsyncMock())
    speaker.stream = MagicMock(
        running=True, session=session, send_cli_command=AsyncMock(return_value=True)
    )
    if not homepods_only:
        apple_tv.stream = MagicMock(running=True, session=session, send_cli_command=AsyncMock())
    await _homepod_gone(provider)

    await apple_tv.volume_set(5)
    await apple_tv.stop()

    speaker.stream.send_cli_command.assert_awaited_once_with("VOLUME=5")
    session.stop.assert_awaited_once()


@pytest.mark.usefixtures("apple_controls")
async def test_only_a_fresh_airplay_record_brings_a_homepod_back_into_its_set(
    scenario: Scenario, provider: AirPlayProvider
) -> None:
    """
    After a goodbye the old _airplay record of a HomePod is no evidence of its set.

    Its _raop record coming back says nothing about the set, so its old player stays until
    a current _airplay record names the set again.
    """
    old_player_id = _player_of(await _announce(scenario, provider, _homepod(None))).player_id
    assert await _announce(scenario, provider, _homepod()) is None
    await _homepod_gone(provider)
    await provider.on_mdns_service_state_change(
        _homepod_raop().name, ServiceStateChange.Added, _homepod_raop()
    )
    await scenario.settle()

    apple_tv = await _announce(scenario, provider, _apple_tv())

    assert apple_tv is not None
    assert apple_tv.speakers == []
    assert await scenario.player_config(old_player_id) is not None

    await _announce(scenario, provider, _homepod())

    assert [speaker.protocol_id for speaker in apple_tv.speakers] == [HOMEPOD_PROTOCOL_ID]
    assert await scenario.player_config(old_player_id) is None


@pytest.mark.usefixtures("apple_controls")
async def test_the_goodbye_of_the_airplay_record_of_a_homepod_takes_it_out_of_its_set(
    scenario: Scenario, provider: AirPlayProvider
) -> None:
    """The _airplay record of a speaker going away is as good as a goodbye of the device."""
    apple_tv = await _announce(scenario, provider, _apple_tv())
    homepod = _homepod()
    await _announce(scenario, provider, homepod)

    await provider.on_mdns_service_state_change(homepod.name, ServiceStateChange.Removed, None)
    await scenario.settle()

    assert apple_tv is not None
    assert apple_tv.speakers == []


@pytest.mark.usefixtures("apple_controls")
async def test_a_homepod_that_is_back_joins_the_music_its_set_plays(
    scenario: Scenario, provider: AirPlayProvider
) -> None:
    """A speaker that was away when its set started is added to the session once it is back."""
    await _announce(scenario, provider, _apple_tv(size=3))
    await _announce(scenario, provider, _homepod(1, size=3))
    await _announce(
        scenario, provider, _homepod(2, device_id=SECOND_HOMEPOD_ID, address="192.168.1.62", size=3)
    )
    first = provider._speakers[HOMEPOD_PROTOCOL_ID]
    second = provider._speakers[SECOND_HOMEPOD_PROTOCOL_ID]
    await _homepod_gone(provider, SECOND_HOMEPOD_ID)
    # the set started on the speaker that was there
    session = MagicMock(add_client=AsyncMock(), remove_client=AsyncMock())
    session.sync_clients = [first]
    first.stream = MagicMock(running=True, session=session)

    await _announce(
        scenario, provider, _homepod(2, device_id=SECOND_HOMEPOD_ID, address="192.168.1.62", size=3)
    )

    session.add_client.assert_awaited_once_with(second, returning=True)
    # a record that only repeats what is known adds nothing
    await _announce(
        scenario, provider, _homepod(2, device_id=SECOND_HOMEPOD_ID, address="192.168.1.62", size=3)
    )
    session.add_client.assert_awaited_once()


@pytest.mark.usefixtures("apple_controls")
async def test_a_homepod_seen_for_the_first_time_joins_the_music_its_set_plays(
    scenario: Scenario, provider: AirPlayProvider
) -> None:
    """A speaker that turns up while its set plays, never seen before, is added to the session."""
    await _announce(scenario, provider, _apple_tv(size=3))
    await _announce(scenario, provider, _homepod(1, size=3))
    first = provider._speakers[HOMEPOD_PROTOCOL_ID]
    session = MagicMock(add_client=AsyncMock(), remove_client=AsyncMock())
    session.sync_clients = [first]
    first.stream = MagicMock(running=True, session=session)

    await _announce(
        scenario, provider, _homepod(2, device_id=SECOND_HOMEPOD_ID, address="192.168.1.62", size=3)
    )

    session.add_client.assert_awaited_once_with(
        provider._speakers[SECOND_HOMEPOD_PROTOCOL_ID], returning=True
    )


@pytest.mark.usefixtures("apple_controls")
async def test_an_apple_tv_that_gets_or_loses_a_set_while_it_runs_follows_it(
    scenario: Scenario, provider: AirPlayProvider
) -> None:
    """A plain Apple TV whose record starts naming a set is the player of that set, and back."""
    plain = _set_device(
        "Apple TV Study", APPLE_TV_ID, "192.168.1.60", None, model="AppleTV14,1", flags="0x644"
    )
    apple_tv = await _announce(scenario, provider, plain)
    assert apple_tv is not None
    player_id = _player_of(apple_tv).player_id
    await _announce(scenario, provider, _homepod())
    # the bridge follows the devices the player plays on both times
    update = cast("MagicMock", provider._bridge_manager.update_endpoints)
    update.reset_mock()

    assert await _announce(scenario, provider, _apple_tv()) is apple_tv
    assert [speaker.protocol_id for speaker in apple_tv.speakers] == [HOMEPOD_PROTOCOL_ID]
    state = scenario.player(player_id)
    assert state is not None
    assert state.name == "Study"
    update.assert_called_with(APPLE_TV_PROTOCOL_ID)
    update.reset_mock()

    assert await _announce(scenario, provider, plain) is apple_tv
    assert apple_tv.carries_set is False
    state = scenario.player(player_id)
    assert state is not None
    assert state.name == "Apple TV Study"
    update.assert_called_with(APPLE_TV_PROTOCOL_ID)


@pytest.mark.usefixtures("apple_controls")
async def test_the_option_of_a_room_shows_at_once(
    scenario: Scenario, provider: AirPlayProvider
) -> None:
    """Switching the option of a room changes what its player reports straight away."""
    apple_tv = await _announce(scenario, provider, _apple_tv())
    await _announce(scenario, provider, _homepod())
    assert apple_tv is not None
    player_id = _player_of(apple_tv).player_id
    # the Apple TV is paired, so the room plays with or without it
    scenario.mass.config.set_protocol_setup_value(
        APPLE_TV_PROTOCOL_ID, CONF_AIRPLAY_CREDENTIALS, "a" * 192
    )
    apple_tv.update_state(force_update=True)
    await scenario.settle()

    for value in (True, False):
        await scenario.mass.players.set_option(player_id, CONF_HOMEPODS_ONLY, value)
        await scenario.settle()
        state = scenario.player(player_id)
        assert state is not None
        assert [(option.key, option.value) for option in state.options] == [
            (CONF_HOMEPODS_ONLY, value)
        ]


@pytest.mark.usefixtures("apple_controls")
async def test_the_goodbye_of_the_airplay_record_of_an_apple_tv_keeps_the_player_of_its_homepod(
    scenario: Scenario, provider: AirPlayProvider
) -> None:
    """
    Without a current record of the Apple TV, its last one is no evidence of its set.

    A HomePod that names the set meanwhile keeps the player it had until the record of the
    Apple TV is back.
    """
    old_player_id = _player_of(await _announce(scenario, provider, _homepod(None))).player_id
    await _announce(scenario, provider, _apple_tv())
    await provider.on_mdns_service_state_change(_apple_tv().name, ServiceStateChange.Removed, None)
    await scenario.settle()

    assert await _announce(scenario, provider, _homepod()) is None
    assert await scenario.player_config(old_player_id) is not None

    await _announce(scenario, provider, _apple_tv())

    assert await scenario.player_config(old_player_id) is None


@pytest.mark.usefixtures("apple_controls")
async def test_an_unpaired_room_is_set_up_to_play_on_its_homepods_only(
    scenario: Scenario, provider: AirPlayProvider
) -> None:
    """
    The setup of a room whose Apple TV is not paired offers to play on the HomePods only.

    Taken, the room plays without pairing, and its option is switched like any other.
    """
    apple_tv = await _announce(scenario, provider, _apple_tv())
    await _announce(scenario, provider, _homepod())
    player_id = _player_of(apple_tv).player_id
    state = scenario.player(player_id)
    assert state is not None
    assert (state.available, state.setup_reason) == (False, "pairing_required")

    with patch.object(AirPlayPlayer, "_run_protocol_pairing", AsyncMock(return_value=False)):
        step = await scenario.mass.config.setup_player(player_id)
        assert step.step_id == "homepods_only_offer"
        step = await scenario.mass.config.submit_setup_flow(
            step.flow_id, {CONF_HOMEPODS_ONLY: True}
        )
        await scenario.settle()
    assert step.type == FlowStepType.FINISH
    # the config controller leaves its sweep of idle flows armed when the server stops
    if (sweep_handle := scenario.mass.config._flow_sweep_handle) is not None:
        sweep_handle.cancel()

    state = scenario.player(player_id)
    assert state is not None
    assert (state.available, state.needs_setup) == (True, False)
    assert [(option.key, option.value) for option in state.options] == [(CONF_HOMEPODS_ONLY, True)]

    await scenario.mass.players.set_option(player_id, CONF_HOMEPODS_ONLY, False)
    await scenario.settle()
    state = scenario.player(player_id)
    assert state is not None
    assert state.setup_reason == "pairing_required"
