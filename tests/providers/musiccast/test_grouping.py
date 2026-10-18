"""Tests for MusicCast grouping of zones and devices."""

from collections.abc import Callable
from contextlib import nullcontext

import pytest
from music_assistant_models.errors import PlayerCommandFailed

from music_assistant.providers.musiccast.constants import CONF_PLAYER_SWITCH_SOURCE_NON_NET
from music_assistant.providers.musiccast.musiccast import MusicCastZoneDevice

from .conftest import MusicCastNetwork, link


def _zone(network: MusicCastNetwork, player_id: str) -> MusicCastZoneDevice:
    return network.player(player_id).zone_device


def _main_in_group(network: MusicCastNetwork) -> None:
    link(_zone(network, "A___main"), _zone(network, "B___main"))


def _main_off(network: MusicCastNetwork) -> None:
    _zone(network, "B___main").device.data.zones["main"].power = "standby"


def _main_plays_spotify(network: MusicCastNetwork) -> None:
    data = _zone(network, "B___main").device.data
    data.netusb_input = "spotify"
    data.zones["main"].input = "spotify"


def _bare_zone_in_group(network: MusicCastNetwork) -> None:
    link(_zone(network, "A___main"), _zone(network, "B___zone2"))


def _zone_leads_on_netusb(network: MusicCastNetwork) -> None:
    _zone(network, "A___zone2").device.data.zones["zone2"].input = "server"


def _zone_off_on_netusb(network: MusicCastNetwork) -> None:
    zone_data = _zone(network, "B___zone2").device.data.zones["zone2"]
    zone_data.power = "standby"
    zone_data.input = "server"


@pytest.mark.parametrize(
    ("setup", "leader_id", "player_ids_to_add", "expected_inputs", "rejected"),
    [
        pytest.param(
            None,
            "A___main",
            ["A___zone2"],
            {"A___zone2": "main_sync"},
            False,
            id="zone-syncs-to-own-main",
        ),
        pytest.param(
            _main_in_group,
            "A___main",
            ["B___zone2"],
            {"B___main": "mc_link", "B___zone2": "main_sync"},
            False,
            id="zone-syncs-to-main-already-in-group",
        ),
        pytest.param(
            _main_off,
            "A___main",
            ["B___zone2", "B___main"],
            {"B___main": "mc_link", "B___zone2": "main_sync"},
            False,
            id="zone-syncs-to-main-joining-in-same-call",
        ),
        pytest.param(
            _main_plays_spotify,
            "A___main",
            ["B___zone2"],
            {"B___main": "audio1", "B___zone2": "mc_link"},
            False,
            id="bare-zone-joins-while-main-plays-other-audio",
        ),
        pytest.param(
            _bare_zone_in_group,
            "A___main",
            ["B___main"],
            {"B___main": "audio1", "B___zone2": "mc_link"},
            True,
            id="main-skipped-after-bare-zone-joined",
        ),
        pytest.param(
            _bare_zone_in_group,
            "A___main",
            ["B___main", "A___zone2"],
            {"B___main": "audio1", "A___zone2": "main_sync"},
            True,
            id="valid-join-done-despite-rejected-main",
        ),
        pytest.param(
            None,
            "A___main",
            ["B___zone2", "B___zone3"],
            {"B___zone2": "mc_link", "B___zone3": "audio3"},
            True,
            id="second-bare-zone-of-device-rejected",
        ),
        pytest.param(
            None,
            "A___main",
            ["B___zone3", "B___zone2"],
            {"B___zone2": "audio2", "B___zone3": "mc_link"},
            True,
            id="second-bare-zone-of-device-rejected-reversed",
        ),
        pytest.param(
            _bare_zone_in_group,
            "A___main",
            ["B___main", "B___zone3"],
            {"B___main": "audio1", "B___zone3": "audio3"},
            True,
            id="zone-not-synced-to-skipped-main",
        ),
        pytest.param(
            _bare_zone_in_group,
            "A___main",
            ["B___zone3", "B___main"],
            {"B___main": "audio1", "B___zone3": "audio3"},
            True,
            id="zone-not-synced-to-skipped-main-zone-first",
        ),
        pytest.param(
            _zone_leads_on_netusb,
            "A___zone2",
            ["A___main"],
            {"A___main": "audio1", "A___zone2": "server"},
            True,
            id="main-skipped-for-group-led-by-own-zone",
        ),
        pytest.param(
            _zone_off_on_netusb,
            "A___main",
            ["B___main"],
            {"B___main": "mc_link", "B___zone2": "server"},
            False,
            id="powered-off-zone-keeps-its-source",
        ),
    ],
)
async def test_set_members(
    network: MusicCastNetwork,
    setup: Callable[[MusicCastNetwork], None] | None,
    leader_id: str,
    player_ids_to_add: list[str],
    expected_inputs: dict[str, str],
    rejected: bool,
) -> None:
    """Zones follow their main via main_sync and impossible joins fail without changing devices."""
    if setup is not None:
        setup(network)

    with pytest.raises(PlayerCommandFailed) if rejected else nullcontext():
        await network.player(leader_id).set_members(player_ids_to_add=player_ids_to_add)

    assert {
        player_id: _zone(network, player_id).zone_data.input for player_id in expected_inputs
    } == expected_inputs


async def test_zone_synced_through_main_is_group_member(network: MusicCastNetwork) -> None:
    """A zone following its main into another receiver's group is reported as group member."""
    _main_in_group(network)
    leader = network.player("A___main")

    await leader.set_members(player_ids_to_add=["B___zone2"])

    members = [
        leader._get_player_id_from_zone_device(zone_device)
        for zone_device in leader.zone_device.musiccast_group
    ]
    assert members == ["A___main", "B___main", "B___zone2"]


async def test_main_leaving_takes_its_synced_zone_along(network: MusicCastNetwork) -> None:
    """A main leaving the group as client takes the zone synced to it along."""
    _main_in_group(network)
    leader = network.player("A___main")
    await leader.set_members(player_ids_to_add=["B___zone2"])

    await leader.set_members(player_ids_to_remove=["B___main"])

    members = [
        leader._get_player_id_from_zone_device(zone_device)
        for zone_device in leader.zone_device.musiccast_group
    ]
    assert members == ["A___main"]
    assert _zone(network, "B___zone2").zone_data.power == "standby"
    assert _zone(network, "A___main").device.data.group_role == "server"


async def test_zone_of_device_in_other_group_rejected(network: MusicCastNetwork) -> None:
    """A zone following its main in one group cannot move to another group on its own."""
    network.add_device("192.168.1.30", "C")
    _main_in_group(network)
    await network.player("A___main").set_members(player_ids_to_add=["B___zone2"])

    with pytest.raises(PlayerCommandFailed):
        await network.player("C___main").set_members(player_ids_to_add=["B___zone2"])

    a_data = _zone(network, "A___main").device.data
    assert (a_data.group_role, a_data.group_client_list) == ("server", ["192.168.1.20"])
    assert _zone(network, "B___main").zone_data.input == "mc_link"
    assert _zone(network, "B___zone2").zone_data.input == "main_sync"


async def test_standby_leader_powered_on_for_own_zone(network: MusicCastNetwork) -> None:
    """Syncing a zone to its main in standby powers that main on."""
    _zone(network, "A___main").zone_data.power = "standby"

    await network.player("A___main").set_members(player_ids_to_add=["A___zone2"])

    assert _zone(network, "A___main").zone_data.power == "on"
    assert _zone(network, "A___zone2").zone_data.input == "main_sync"


@pytest.mark.parametrize(
    ("input_list", "expected_input"),
    [
        pytest.param(["server", "audio2", "audio1"], "audio1", id="first-non-net-source"),
        pytest.param(["server", "spotify"], "server", id="no-non-net-source"),
    ],
)
async def test_zone_switch_fallback_source(
    network: MusicCastNetwork, input_list: list[str], expected_input: str
) -> None:
    """An invalid switch source falls back to a non-net source, or leaves the zone alone."""
    network.config_values[CONF_PLAYER_SWITCH_SOURCE_NON_NET] = "spotify"
    zone2 = _zone(network, "A___zone2")
    zone2.zone_data.input_list = input_list
    zone2.zone_data.input = "server"

    await network.player("A___main")._handle_zone_grouping(zone2)

    assert zone2.zone_data.input == expected_input
