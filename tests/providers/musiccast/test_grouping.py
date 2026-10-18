"""Tests for MusicCast grouping of zones and devices."""

from collections.abc import Callable
from contextlib import nullcontext
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiomusiccast.exceptions import MusicCastConnectionException, MusicCastGroupException
from music_assistant_models.errors import PlayerCommandFailed

from music_assistant.providers.musiccast.constants import (
    CONF_PLAYER_SWITCH_SOURCE_NON_NET,
    MC_NULL_GROUP,
)
from music_assistant.providers.musiccast.musiccast import MusicCastZoneDevice
from music_assistant.providers.musiccast.player import MusicCastPlayer

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


def _bare_zone_in_group_of_rebooted_server(network: MusicCastNetwork) -> None:
    # the server rebooted and lost the group, the zone still links to it
    _bare_zone_in_group(network)
    a_data = _zone(network, "A___main").device.data
    a_data.group_role = "none"
    a_data.group_id = MC_NULL_GROUP
    a_data.group_client_list.clear()


def _client_role_left_behind(network: MusicCastNetwork) -> None:
    # a zone left another group by switching its input, the device keeps its client role
    data = _zone(network, "B___main").device.data
    data.group_role = "client"
    data.group_id = "OTHER"


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
        pytest.param(
            _client_role_left_behind,
            "A___main",
            ["B___main"],
            {"B___main": "mc_link"},
            False,
            id="device-left-other-group-joins",
        ),
        pytest.param(
            _bare_zone_in_group_of_rebooted_server,
            "A___main",
            ["B___main"],
            {"B___main": "mc_link"},
            False,
            id="device-in-group-of-rebooted-server-joins",
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


async def test_remote_main_power_failure_hits_its_own_receiver(
    network: MusicCastNetwork, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A main of another receiver failing to power on fails the join and only marks that receiver."""
    monkeypatch.setattr(MusicCastPlayer, "update_state", MagicMock())
    _main_in_group(network)
    b_main = _zone(network, "B___main")
    b_main.zone_data.power = "standby"
    monkeypatch.setattr(
        b_main.device, "turn_on", AsyncMock(side_effect=MusicCastConnectionException)
    )

    with pytest.raises(PlayerCommandFailed):
        await network.player("A___main").set_members(player_ids_to_add=["B___zone2"])

    assert network.player("A___main")._attr_available
    assert not network.player("B___zone2")._attr_available
    assert _zone(network, "B___zone2").zone_data.input == "audio2"


async def test_main_leaves_when_synced_zone_fails_to_leave(
    network: MusicCastNetwork, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A synced zone failing to leave does not keep its main in the group."""
    _main_in_group(network)
    leader = network.player("A___main")
    await leader.set_members(player_ids_to_add=["B___zone2"])
    monkeypatch.setattr(
        _zone(network, "B___zone2").device,
        "zone_unjoin",
        AsyncMock(side_effect=MusicCastGroupException),
    )

    await leader.set_members(player_ids_to_remove=["B___main"])

    assert leader.zone_device.device.data.group_client_list == []


@pytest.mark.parametrize("reverse_discovery_order", [False, True])
async def test_zone_synced_through_main_resolves_network_server(
    network: MusicCastNetwork, reverse_discovery_order: bool
) -> None:
    """A zone following its main into a group resolves the group's server, not its own main."""
    _main_in_group(network)
    await network.player("A___main").set_members(player_ids_to_add=["B___zone2"])
    if reverse_discovery_order:
        network.provider.mc_controller.physical_devices.reverse()

    assert _zone(network, "B___zone2").group_server is _zone(network, "A___main")


async def test_main_sync_zone_resolves_main_not_sibling_server(network: MusicCastNetwork) -> None:
    """A zone on main_sync belongs to main, also while another zone of its device leads a group."""
    link(_zone(network, "A___zone2"), _zone(network, "B___main"))
    _zone(network, "A___zone3").zone_data.input = "main_sync"

    assert _zone(network, "A___zone3").group_server is _zone(network, "A___main")
    assert _zone(network, "A___zone3") not in _zone(network, "A___zone2").musiccast_group


async def test_zone_leaving_main_sync_joins_powered_on(network: MusicCastNetwork) -> None:
    """A zone leaving its local main_sync for another group is powered on when it joins."""
    _main_plays_spotify(network)
    _zone(network, "B___zone2").zone_data.input = "main_sync"

    await network.player("A___main").set_members(player_ids_to_add=["B___zone2"])

    zone_data = _zone(network, "B___zone2").zone_data
    assert (zone_data.input, zone_data.power) == ("mc_link", "on")
    assert _zone(network, "B___main").zone_data.input == "audio1"


async def test_group_update_keeps_client_main_with_synced_zone(network: MusicCastNetwork) -> None:
    """A client main with a synced zone does not close a group when its server lost the group."""
    _main_in_group(network)
    await network.player("A___main").set_members(player_ids_to_add=["B___zone2"])
    # A rebooted and lost its group
    a_data = _zone(network, "A___main").device.data
    a_data.group_role = "none"
    a_data.group_id = MC_NULL_GROUP
    a_data.group_client_list.clear()

    await _zone(network, "B___main")._check_client_list()

    b_data = _zone(network, "B___main").device.data
    assert (b_data.group_role, b_data.group_id) == ("client", "GROUP")


async def test_failed_network_join_is_reported(
    network: MusicCastNetwork, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A network join the device does not complete fails the request."""
    monkeypatch.setattr(
        _zone(network, "B___main").device,
        "mc_client_join",
        AsyncMock(side_effect=MusicCastGroupException),
    )

    with pytest.raises(PlayerCommandFailed):
        await network.player("A___main").set_members(player_ids_to_add=["B___main"])


async def test_standby_leader_powered_on_for_own_zone(network: MusicCastNetwork) -> None:
    """Syncing a zone to its main in standby powers that main on."""
    _zone(network, "A___main").zone_data.power = "standby"

    await network.player("A___main").set_members(player_ids_to_add=["A___zone2"])

    assert _zone(network, "A___main").zone_data.power == "on"
    assert _zone(network, "A___zone2").zone_data.input == "main_sync"


@pytest.mark.parametrize(
    ("input_list", "expected_input", "rejected"),
    [
        pytest.param(["server", "audio2", "audio1"], "audio2", False, id="first-non-net-source"),
        pytest.param(["server", "spotify"], "server", True, id="no-non-net-source"),
    ],
)
async def test_zone_switch_fallback_source(
    network: MusicCastNetwork, input_list: list[str], expected_input: str, rejected: bool
) -> None:
    """An invalid switch source falls back to the first non-net source, or fails."""
    network.config_values[CONF_PLAYER_SWITCH_SOURCE_NON_NET] = "spotify"
    zone2 = _zone(network, "A___zone2")
    zone2.zone_data.input_list = input_list
    zone2.zone_data.input = "server"

    with pytest.raises(PlayerCommandFailed) if rejected else nullcontext():
        await network.player("A___main")._handle_zone_grouping(zone2)

    assert zone2.zone_data.input == expected_input
