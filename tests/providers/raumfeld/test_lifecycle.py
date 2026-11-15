"""Tests for Raumfeld rooms on a running server: attach and detach, identity, settings."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from music_assistant_models.enums import IdentifierType, ProtocolRole

from music_assistant.controllers.players.protocol_linking import PLAYER_ID_PREFIX
from music_assistant.providers.raumfeld.constants import CONF_RESTART_ON_JOIN
from music_assistant.providers.raumfeld.host import Devices, RaumfeldHost, Room, ZoneConfig
from music_assistant.providers.raumfeld.player import RaumfeldPlayer
from music_assistant.providers.raumfeld.provider import RaumfeldPlayerProvider

if TYPE_CHECKING:
    from collections.abc import Iterator

    from music_assistant.models.player import Player
    from tests.scenarios.harness import Scenario

HOST_IP = "192.168.1.41"
SERIAL_BAR = "04:a3:16:f3:b6:c4"
# the protocol ids the rooms get from the serials of their speakers
BAR = "raumfeld_04a316f3b6c4"
TV = "raumfeld_b827eb57daf9"


@dataclass(frozen=True)
class _Speaker:
    """A room's speaker as the host lists it, with what the provider reads from it once."""

    room_udn: str
    renderer_udn: str
    serial: str
    ip_address: str


BAR_SPEAKER = _Speaker("uuid:room-bar", "uuid:renderer-bar", SERIAL_BAR, "192.168.1.42")
# the speaker of the room that runs on the host
TV_SPEAKER = _Speaker("uuid:room-tv", "uuid:renderer-tv", "b8:27:eb:57:da:f9", HOST_IP)


@pytest.fixture(autouse=True)
def no_polls() -> Iterator[None]:
    """Keep the rooms from reading their speakers, which are not there."""
    with patch.object(RaumfeldPlayer, "poll", AsyncMock()):
        yield


async def _raumfeld(scenario: Scenario) -> RaumfeldPlayerProvider:
    """Load the Raumfeld provider on the running server, connected to a host without rooms."""
    fake = await scenario.add_provider("raumfeld")
    provider = RaumfeldPlayerProvider(scenario.mass, fake.manifest, fake.config, set())
    # the supervisor would look for the host on the network
    with patch.object(RaumfeldPlayerProvider, "_supervise", AsyncMock()):
        await provider.handle_async_init()
    provider.available = True
    provider._connected = True
    scenario.mass._providers[fake.instance_id] = provider
    await scenario.settle()
    return provider


async def _sync(
    scenario: Scenario, provider: RaumfeldPlayerProvider, rooms: dict[str, _Speaker]
) -> None:
    """
    Have the host list the given rooms, and the provider sync its rooms with it.

    :param rooms: The speaker of each room the host lists, by room name.
    """
    provider.host = RaumfeldHost(MagicMock(), f"http://{HOST_IP}:47365")
    provider.host._zone_config = ZoneConfig(
        rooms={
            name: Room(
                name,
                speaker.room_udn,
                speaker.renderer_udn,
                "ACTIVE",
                False,
                renderer_udns=(speaker.renderer_udn,),
            )
            for name, speaker in rooms.items()
        },
        zones={},
    )
    provider.host._devices = Devices(
        locations={
            speaker.renderer_udn: f"http://{speaker.ip_address}:50000/description.xml"
            for speaker in rooms.values()
        },
        media_server_udn=None,
    )
    for name, speaker in rooms.items():
        provider._room_serials[name] = speaker.serial
        provider._room_serial_renderers[name] = speaker.renderer_udn
        provider._versions_read.add(name)
    await provider._sync_rooms()
    await scenario.settle()


def _attached(provider: RaumfeldPlayerProvider, protocol_id: str) -> RaumfeldPlayer:
    """Return the attached protocol of a room."""
    protocol = provider.get_protocol(protocol_id)
    assert isinstance(protocol, RaumfeldPlayer)
    assert protocol.player is not None
    return protocol


def _player(protocol: RaumfeldPlayer) -> Player:
    """Return the player a room's protocol is attached to."""
    assert protocol.player is not None
    return protocol.player


def _player_values(scenario: Scenario, player_id: str) -> dict[str, object]:
    """Return the values stored on a player's own record."""
    return dict(scenario.mass.config.get(f"players/{player_id}/values") or {})


async def test_a_new_room_is_the_native_protocol_of_a_new_player(scenario: Scenario) -> None:
    """A room first seen gets a player of its own; its own defaults go on its own record."""
    provider = await _raumfeld(scenario)

    await _sync(scenario, provider, {"Bar": BAR_SPEAKER})

    player = _player(_attached(provider, BAR))
    assert player.player_id.startswith(PLAYER_ID_PREFIX)
    assert player.get_role(BAR) == ProtocolRole.NATIVE
    assert player.name == "Bar"
    assert scenario.protocol_config(BAR)["enable_icy_metadata"] == "basic"  # type: ignore[index]
    assert "enable_icy_metadata" not in _player_values(scenario, player.player_id)


async def test_a_setting_of_a_new_room_is_read_from_its_own_record(scenario: Scenario) -> None:
    """A room setting saved on its player is the room's, also after the room was away."""
    provider = await _raumfeld(scenario)
    await _sync(scenario, provider, {"Bar": BAR_SPEAKER})
    player_id = _player(_attached(provider, BAR)).player_id
    assert not _attached(provider, BAR)._group_restarts_on_join()

    await scenario.configure_player(player_id, **{CONF_RESTART_ON_JOIN: True})

    assert scenario.protocol_config(BAR)[CONF_RESTART_ON_JOIN] is True  # type: ignore[index]
    assert CONF_RESTART_ON_JOIN not in _player_values(scenario, player_id)
    assert _attached(provider, BAR)._group_restarts_on_join()
    await _sync(scenario, provider, {})
    await _sync(scenario, provider, {"Bar": BAR_SPEAKER})
    returned = _attached(provider, BAR)
    assert _player(returned).player_id == player_id
    assert returned._group_restarts_on_join()


async def test_a_room_the_host_no_longer_lists_is_detached_until_it_returns(
    scenario: Scenario,
) -> None:
    """The player of a room gone from the host stays, unavailable, and is the room's again."""
    provider = await _raumfeld(scenario)
    await _sync(scenario, provider, {"Bar": BAR_SPEAKER, "TV": TV_SPEAKER})
    tv_player = _player(_attached(provider, TV)).player_id

    await _sync(scenario, provider, {"Bar": BAR_SPEAKER})

    assert provider.get_protocol(TV) is None
    assert scenario.player(tv_player).available is False  # type: ignore[union-attr]
    assert _player(_attached(provider, BAR)).available is True
    await _sync(scenario, provider, {"Bar": BAR_SPEAKER, "TV": TV_SPEAKER})
    assert _player(_attached(provider, TV)).player_id == tv_player
    assert scenario.player(tv_player).available is True  # type: ignore[union-attr]


async def test_a_room_renamed_while_the_host_was_away_keeps_its_player(
    scenario: Scenario,
) -> None:
    """A room renamed while the host did not answer is the same player under the new name."""
    scenario.expect_log("pausing")
    provider = await _raumfeld(scenario)
    await _sync(scenario, provider, {"Marshall": BAR_SPEAKER})
    marshall = _attached(provider, BAR)
    player_id = _player(marshall).player_id
    await provider._disconnect("stopped answering")
    await scenario.settle()
    assert scenario.player(player_id).available is False  # type: ignore[union-attr]

    await _sync(scenario, provider, {"Werkplaats": BAR_SPEAKER})

    assert _attached(provider, BAR) is marshall
    assert marshall.room == "Werkplaats"
    state = scenario.player(player_id)
    assert state is not None
    assert (state.name, state.available) == ("Werkplaats", True)


async def test_a_room_under_another_id_keeps_its_player_and_settings(scenario: Scenario) -> None:
    """
    A room whose id changed takes its player, its name and its settings along.

    The room whose speaker shares the serial of another one goes by its room UDN, which
    changes when the room is set up again in the Raumfeld app.
    """
    scenario.expect_log("reports the same serial")
    provider = await _raumfeld(scenario)
    tv = replace(TV_SPEAKER, serial=SERIAL_BAR)
    await _sync(scenario, provider, {"Bar": BAR_SPEAKER, "TV": tv})
    player_id = _player(_attached(provider, "raumfeld_room-tv")).player_id
    await scenario.configure_player(player_id, name="Television", **{CONF_RESTART_ON_JOIN: True})

    await _sync(
        scenario, provider, {"Bar": BAR_SPEAKER, "TV": replace(tv, room_udn="uuid:room-tv2")}
    )

    returned = _attached(provider, "raumfeld_room-tv2")
    assert _player(returned).player_id == player_id
    assert _player(returned).name == "Television"
    assert returned._group_restarts_on_join()
    assert scenario.mass.config.get_protocol_record("raumfeld_room-tv") is None
    assert [player.name for player in scenario.visible_players()] == ["Bar", "Television"]


async def test_two_rooms_on_one_address_stay_two_players(scenario: Scenario) -> None:
    """
    Two rooms whose speakers share an address are two players, also while one is away.

    The UUID of a room's renderer tells the rooms apart; their shared address alone would
    give the second room the player of the first one while that one is away.
    """
    provider = await _raumfeld(scenario)
    bar = replace(BAR_SPEAKER, ip_address=HOST_IP)
    await _sync(scenario, provider, {"Bar": bar})
    bar_player = _player(_attached(provider, BAR)).player_id
    await scenario.configure_player(bar_player, name="Bar room")
    await _sync(scenario, provider, {})

    await _sync(scenario, provider, {"TV": TV_SPEAKER})

    tv_player = _player(_attached(provider, TV))
    assert tv_player.player_id != bar_player
    assert tv_player.name == "TV"
    assert scenario.mass.config.get_protocol_record(BAR)["player_id"] == bar_player  # type: ignore[index]
    await _sync(scenario, provider, {"Bar": bar, "TV": TV_SPEAKER})
    assert _player(_attached(provider, BAR)).player_id == bar_player
    assert [player.name for player in scenario.visible_players()] == ["Bar room", "TV"]


async def test_a_disabled_room_is_not_set_up_until_it_is_enabled(scenario: Scenario) -> None:
    """A room whose player is disabled gets no protocol, until enabling has it discovered."""
    provider = await _raumfeld(scenario)
    rooms = {"Bar": BAR_SPEAKER, "TV": TV_SPEAKER}
    await _sync(scenario, provider, rooms)
    tv_player = _player(_attached(provider, TV)).player_id
    # disabling switches the room off first
    with patch.object(provider, "enter_standby", AsyncMock()):
        await scenario.disable(tv_player)
    assert provider.get_protocol(TV) is None

    with patch.object(
        RaumfeldPlayer, "__init__", autospec=True, side_effect=RaumfeldPlayer.__init__
    ) as build:
        await _sync(scenario, provider, rooms)

    build.assert_not_called()
    assert provider.get_protocol(TV) is None
    await scenario.enable(tv_player)
    await scenario.idle()
    assert _player(_attached(provider, TV)).player_id == tv_player


async def test_a_removed_room_comes_back_as_a_new_player(scenario: Scenario) -> None:
    """A room still on the host returns after its removal, as a new player with defaults."""
    provider = await _raumfeld(scenario)
    await _sync(scenario, provider, {"Bar": BAR_SPEAKER})
    removed = _attached(provider, BAR)
    player_id = _player(removed).player_id
    await scenario.configure_player(player_id, name="Bar room")

    await scenario.remove(player_id)
    assert provider.get_protocol(BAR) is None
    await scenario.idle()

    returned = _attached(provider, BAR)
    assert returned is not removed
    assert _player(returned).player_id != player_id
    assert _player(returned).name == "Bar"


async def test_discovery_leaves_attached_rooms_alone(scenario: Scenario) -> None:
    """Discovery attaches nothing for a room that is attached, nor while the host is away."""
    provider = await _raumfeld(scenario)
    await _sync(scenario, provider, {"Bar": BAR_SPEAKER})
    bar = _attached(provider, BAR)
    controller = scenario.mass.players

    with patch.object(controller, "register", wraps=controller.register) as attach:
        await provider.discover_players()
        await scenario.settle()
        await _sync(scenario, provider, {})
        provider._connected = False
        provider.host._zone_config = ZoneConfig(
            rooms={
                "Bar": Room("Bar", BAR_SPEAKER.room_udn, BAR_SPEAKER.renderer_udn, "ACTIVE", False)
            },
            zones={},
        )
        await provider.discover_players()
        await scenario.settle()

    attach.assert_not_called()
    assert provider.get_protocol(BAR) is None
    assert bar.player is None


async def test_the_dlna_renderer_of_a_rooms_speaker_is_an_output_of_its_room(
    scenario: Scenario,
) -> None:
    """
    The DLNA renderer of a room's speaker joins the room's player; another renderer does not.

    A room reports the UUID of its own renderer only: the renderer of a second speaker in
    the room stays a player of its own.
    """
    provider = await _raumfeld(scenario)
    dlna = await scenario.add_provider("dlna")
    await _sync(scenario, provider, {"Bar": BAR_SPEAKER})
    own = dlna.protocol(
        BAR_SPEAKER.renderer_udn,
        "Speaker Bar",
        role="audio",
        identifiers={
            IdentifierType.UUID: "renderer-bar",
            IdentifierType.IP_ADDRESS: BAR_SPEAKER.ip_address,
            IdentifierType.MAC_ADDRESS: "00:1e:c0:00:00:01",
        },
    )
    second = dlna.protocol(
        "uuid:renderer-bar-2",
        "Speaker Bar 2",
        role="audio",
        identifiers={
            IdentifierType.UUID: "renderer-bar-2",
            IdentifierType.IP_ADDRESS: "192.168.1.43",
            IdentifierType.MAC_ADDRESS: "00:1e:c0:00:00:02",
        },
    )

    await scenario.register(own)
    await scenario.register(second)
    await scenario.idle()

    room_player = _player(_attached(provider, BAR)).player_id
    assert scenario.player_of(own) == room_player
    assert scenario.player_of(second) not in (None, room_player)


async def test_a_room_whose_speaker_returns_under_another_address_reports_it(
    scenario: Scenario,
) -> None:
    """
    A room whose speaker the host lists under another address after an outage reports that one.

    The device that has its old address now has an audio output of its own player.
    """
    provider = await _raumfeld(scenario)
    scenario.arp[BAR_SPEAKER.ip_address] = "00:1e:c0:00:00:01"
    await _sync(scenario, provider, {"Bar": BAR_SPEAKER})
    protocol = _attached(provider, BAR)
    player_id = _player(protocol).player_id
    await provider._disconnect("is gone")
    scenario.expect_log("pausing")
    scenario.arp[BAR_SPEAKER.ip_address] = "00:1e:c0:00:00:02"
    scenario.arp["192.168.1.43"] = "00:1e:c0:00:00:01"

    await _sync(scenario, provider, {"Bar": replace(BAR_SPEAKER, ip_address="192.168.1.43")})
    other_device = (await scenario.add_provider("dlna")).protocol(
        "uuid:device-b",
        "Device B",
        role="audio",
        identifiers={
            IdentifierType.UUID: "device-b",
            IdentifierType.IP_ADDRESS: BAR_SPEAKER.ip_address,
            IdentifierType.MAC_ADDRESS: "00:1e:c0:00:00:02",
        },
    )
    await scenario.register(other_device)

    assert _attached(provider, BAR) is protocol
    assert protocol.device_info.ip_address == "192.168.1.43"
    # what is stored for the room while it is away follows it too
    record = scenario.mass.config.get_protocol_record(BAR)
    assert record is not None
    assert [IdentifierType.IP_ADDRESS.value, "192.168.1.43"] in record["values"]["identifiers"]
    assert scenario.player_of(other_device) != player_id
