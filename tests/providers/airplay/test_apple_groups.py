"""Tests for AirPlay Apple groups (Apple TV home theater groups and stereo pairs)."""

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from music_assistant.constants import (
    AIRPLAY_GROUP_HOME_THEATER,
    AIRPLAY_GROUP_STEREO_PAIR,
    CONF_AIRPLAY_GROUP_ID,
    CONF_AIRPLAY_GROUP_LEADER,
    CONF_AIRPLAY_GROUP_TYPE,
    CONF_GROUP_MEMBERS,
    CONF_PLAYERS,
)
from music_assistant.providers.airplay.apple_groups import (
    AppleGroupManager,
    get_home_theater_membership,
    get_stereo_pair_membership,
    group_player_id,
)
from music_assistant.providers.airplay.constants import CONF_APPLE_HOME_GROUPS

# TXT records as an Apple TV and a HomePod stereo pair advertise them (trimmed to the
# grouping fields), in the setups the Home app produces
HOME_THEATER_ID = "FBE1CA53-98F2-573F-872E-2805C5918EE7"
PAIR_ID = "C672647A-920D-51DE-9372-59D923569682"
APPLE_TV_LINKED = {
    "model": "AppleTV11,1",
    "gpn": "Living Room TV",
    "gid": HOME_THEATER_ID,
    "igl": "1",
    "gcgl": "1",
    "psgid": HOME_THEATER_ID,
    "psgtp": "1",
    "psgsz": "3",
    "pgm": "0",
    "pgmid": "0",
}
LEFT_LINKED = {
    "model": "AudioAccessory1,1",
    "gpn": "Living Room TV",
    "gid": HOME_THEATER_ID,
    "igl": "0",
    "gcgl": "1",
    "psgid": HOME_THEATER_ID,
    "psgtp": "1",
    "psgsz": "3",
    "pgid": HOME_THEATER_ID,
    "pgm": "0",
    "pgmid": "2",
    "tsid": "45886F4D-F128-569C-865B-2617893EEBF7",
}
RIGHT_LINKED = {**LEFT_LINKED, "pgmid": "1"}
LEFT_PAIRED = {
    "model": "AudioAccessory1,1",
    "gpn": "Bedroom",
    "gid": f"{PAIR_ID}+1+936C22C6-C719-484C-8427-60352257117E",
    "igl": "1",
    "gcgl": "1",
    "tsid": PAIR_ID,
}
RIGHT_PAIRED = {
    **LEFT_PAIRED,
    "gid": f"{PAIR_ID}+0+2A324828-C582-49FE-BCD6-D5DB9F6231E8",
}
APPLE_TV_ALONE = {
    "model": "AppleTV11,1",
    "gid": "04F9B7A2-135B-4394-A409-52EA4DB2FC1D",
    "igl": "1",
    "gcgl": "1",
}
SPEAKER_ALONE = {
    "model": "AudioAccessory1,1",
    "gid": "F36CEB65-A90F-4BFA-8963-171C7A76A4F8+936C22C6-C719-484C-8427-60352257117E",
    "igl": "1",
    "gcgl": "1",
}


def _player(player_id: str, properties: dict[str, str] | None) -> MagicMock:
    """
    Create a mock AirPlay player advertising the given TXT record.

    :param player_id: The player id.
    :param properties: The decoded TXT record, or None for no AirPlay discovery info.
    """
    player = MagicMock()
    player.player_id = player_id
    player.display_name = player_id
    player.airplay_discovery_info = (
        None if properties is None else SimpleNamespace(decoded_properties=properties)
    )
    return player


def _manager(
    players: list[MagicMock],
    saved_groups: dict[str, dict[str, Any]] | None = None,
    enabled: bool = True,
    registered: bool = True,
) -> tuple[AppleGroupManager, MagicMock]:
    """
    Create a manager over the given players and saved sync group configs.

    :param players: The AirPlay players discovery shows.
    :param saved_groups: Raw config values per saved sync group player id.
    :param enabled: Whether Apple Home groupings are followed.
    :param registered: Whether the saved sync groups are registered players.
    """
    saved = saved_groups or {}
    mass = MagicMock()
    mass.closing = False
    sync_prov = MagicMock()
    sync_prov.instance_id = "sync_group"
    sync_prov.ensure_owned_group_player = AsyncMock()
    sync_prov.remove_group_player = AsyncMock()
    mass.get_provider = MagicMock(return_value=sync_prov)
    raw_player_confs = {
        pid: {"player_id": pid, "default_name": "Living Room TV", "values": values}
        for pid, values in saved.items()
    }
    mass.config.get = MagicMock(
        side_effect=lambda key, default=None: raw_player_confs if key == CONF_PLAYERS else default
    )
    mass.players.get_player = MagicMock(
        side_effect=lambda pid: MagicMock() if registered and pid in saved else None
    )
    # a group the user has not renamed has only its default name
    mass.config.get_player_configs = AsyncMock(
        return_value=[
            SimpleNamespace(player_id=pid, name=None, default_name="Living Room TV")
            for pid in saved
        ]
    )
    mass.config.get_raw_player_config_value = MagicMock(
        side_effect=lambda pid, key, default=None: saved.get(pid, {}).get(key, default)
    )
    prov = MagicMock()
    prov.mass = mass
    prov.instance_id = "airplay"
    prov.get_players = MagicMock(return_value=players)
    prov.config.get_value = MagicMock(
        side_effect=lambda key, default=None: enabled if key == CONF_APPLE_HOME_GROUPS else default
    )
    return AppleGroupManager(prov), sync_prov


def _saved_home_theater(members: list[str], group_type: Any = AIRPLAY_GROUP_HOME_THEATER) -> dict:
    """
    Return the raw config values of a saved Apple TV home theater group.

    :param members: The saved group members.
    :param group_type: The saved group type.
    """
    return {
        CONF_AIRPLAY_GROUP_ID: HOME_THEATER_ID,
        CONF_AIRPLAY_GROUP_TYPE: group_type,
        CONF_AIRPLAY_GROUP_LEADER: "apple_tv",
        CONF_GROUP_MEMBERS: members,
    }


def test_home_theater_membership_marks_the_apple_tv_as_leader() -> None:
    """The Apple TV leads its home theater group; its speakers hold their slots."""
    apple_tv = get_home_theater_membership(_player("apple_tv", APPLE_TV_LINKED))
    right = get_home_theater_membership(_player("right", RIGHT_LINKED))
    assert apple_tv is not None
    assert right is not None
    assert apple_tv.group_id == right.group_id == HOME_THEATER_ID
    assert apple_tv.group_type == AIRPLAY_GROUP_HOME_THEATER
    assert apple_tv.name == "Living Room TV"
    assert right.position == 1
    assert apple_tv.position > right.position


def test_speaker_paired_into_another_group_is_no_home_theater_member() -> None:
    """A speaker whose pgid differs from its psgid is not a member of that group."""
    assert get_home_theater_membership(_player("x", {**LEFT_LINKED, "pgid": "OTHER"})) is None


def test_stereo_pair_membership_orders_by_role() -> None:
    """A stereo pair is keyed on tsid and ordered by the role inside gid."""
    left = get_stereo_pair_membership(_player("left", LEFT_PAIRED))
    right = get_stereo_pair_membership(_player("right", RIGHT_PAIRED))
    assert left is not None
    assert right is not None
    assert left.group_id == right.group_id == PAIR_ID
    assert left.group_type == AIRPLAY_GROUP_STEREO_PAIR
    assert left.name == "Bedroom"
    assert (right.position, left.position) == (0, 1)


def test_standalone_devices_advertise_no_group() -> None:
    """Devices outside any Home app grouping belong to no Apple group."""
    for properties in (APPLE_TV_ALONE, SPEAKER_ALONE):
        assert get_home_theater_membership(_player("x", properties)) is None
        assert get_stereo_pair_membership(_player("x", properties)) is None


def test_stereo_pair_inside_a_home_theater_group_plays_as_part_of_it() -> None:
    """A pair linked to an Apple TV forms one home theater group, the speakers first."""
    players = [
        _player("apple_tv", APPLE_TV_LINKED),
        _player("left", LEFT_LINKED),
        _player("right", RIGHT_LINKED),
    ]
    manager, _ = _manager(players)
    found = manager._find_groups({p.player_id: p for p in players})
    assert list(found) == [HOME_THEATER_ID]
    group = found[HOME_THEATER_ID]
    assert group.ordered_member_ids() == ["right", "left", "apple_tv"]
    assert group.leader_id == "apple_tv"
    assert group.name == "Living Room TV"


def test_group_player_id_is_stable_for_a_group_id() -> None:
    """The group player id follows from the Apple group id alone."""
    assert group_player_id(HOME_THEATER_ID) == group_player_id(HOME_THEATER_ID.lower())
    assert group_player_id(HOME_THEATER_ID).startswith("syncgroup_")


async def test_reconcile_forms_a_home_theater_group() -> None:
    """Discovery of a linked Apple TV and pair forms one group, its members hidden."""
    players = [
        _player("apple_tv", APPLE_TV_LINKED),
        _player("left", LEFT_LINKED),
        _player("right", RIGHT_LINKED),
    ]
    manager, sync_prov = _manager(players)
    await manager.reconcile()
    sync_prov.ensure_owned_group_player.assert_awaited_once_with(
        group_player_id(HOME_THEATER_ID),
        "Living Room TV",
        ["right", "left", "apple_tv"],
        {
            CONF_AIRPLAY_GROUP_ID: HOME_THEATER_ID,
            CONF_AIRPLAY_GROUP_TYPE: AIRPLAY_GROUP_HOME_THEATER,
            CONF_AIRPLAY_GROUP_LEADER: "apple_tv",
        },
    )
    for player in players:
        player.set_represented_by.assert_called_with("Living Room TV")


async def test_reconcile_forms_a_stereo_pair() -> None:
    """A stereo pair not linked to an Apple TV forms a group named after the pair."""
    players = [_player("left", LEFT_PAIRED), _player("right", RIGHT_PAIRED)]
    manager, sync_prov = _manager(players)
    await manager.reconcile()
    sync_prov.ensure_owned_group_player.assert_awaited_once_with(
        group_player_id(PAIR_ID),
        "Bedroom",
        ["right", "left"],
        {CONF_AIRPLAY_GROUP_ID: PAIR_ID, CONF_AIRPLAY_GROUP_TYPE: AIRPLAY_GROUP_STEREO_PAIR},
    )


async def test_reconcile_forms_no_group_of_one() -> None:
    """A lone member of a group forms nothing until another member shows up."""
    manager, sync_prov = _manager([_player("left", LEFT_PAIRED)])
    await manager.reconcile()
    sync_prov.ensure_owned_group_player.assert_not_awaited()


async def test_reconcile_keeps_a_group_whose_member_is_offline() -> None:
    """A member missing from discovery is no conflict: the group stays as it is."""
    group_id = group_player_id(HOME_THEATER_ID)
    players = [_player("apple_tv", APPLE_TV_LINKED), _player("right", RIGHT_LINKED)]
    manager, sync_prov = _manager(
        players, {group_id: _saved_home_theater(["right", "left", "apple_tv"])}
    )
    await manager.reconcile()
    sync_prov.remove_group_player.assert_not_awaited()
    sync_prov.ensure_owned_group_player.assert_not_awaited()


async def test_reconcile_adds_a_member_that_shows_up_later() -> None:
    """A member advertising the group's id joins the saved group."""
    group_id = group_player_id(HOME_THEATER_ID)
    players = [
        _player("apple_tv", APPLE_TV_LINKED),
        _player("left", LEFT_LINKED),
        _player("right", RIGHT_LINKED),
    ]
    manager, sync_prov = _manager(players, {group_id: _saved_home_theater(["right", "apple_tv"])})
    await manager.reconcile()
    sync_prov.ensure_owned_group_player.assert_awaited_once_with(
        group_id, "Living Room TV", ["right", "left", "apple_tv"], {}
    )
    for player in players:
        player.set_represented_by.assert_called_with("Living Room TV")


async def test_reconcile_dissolves_a_group_a_member_left() -> None:
    """A member advertising another group (unpaired in the Home app) dissolves the group."""
    group_id = group_player_id(HOME_THEATER_ID)
    players = [_player("apple_tv", APPLE_TV_LINKED), _player("right", SPEAKER_ALONE)]
    manager, sync_prov = _manager(players, {group_id: _saved_home_theater(["right", "apple_tv"])})
    await manager.reconcile()
    sync_prov.remove_group_player.assert_awaited_once_with(group_id)


@pytest.mark.parametrize("group_type", [None, "something_else"])
async def test_reconcile_dissolves_a_group_with_an_unknown_type(group_type: Any) -> None:
    """A saved group without a known Apple group type is not guessed at but dissolved."""
    group_id = group_player_id(HOME_THEATER_ID)
    players = [_player("apple_tv", APPLE_TV_LINKED), _player("right", RIGHT_LINKED)]
    manager, sync_prov = _manager(
        players, {group_id: _saved_home_theater(["right", "apple_tv"], group_type)}
    )
    await manager.reconcile()
    sync_prov.remove_group_player.assert_awaited_once_with(group_id)


async def test_reconcile_dissolves_groups_when_turned_off() -> None:
    """Turning the setting off dissolves the groups and shows the members again."""
    group_id = group_player_id(HOME_THEATER_ID)
    players = [_player("apple_tv", APPLE_TV_LINKED), _player("right", RIGHT_LINKED)]
    manager, sync_prov = _manager(
        players, {group_id: _saved_home_theater(["right", "apple_tv"])}, enabled=False
    )
    await manager.reconcile()
    sync_prov.remove_group_player.assert_awaited_once_with(group_id)
    sync_prov.ensure_owned_group_player.assert_not_awaited()
    for player in players:
        player.set_represented_by.assert_called_with(None)


async def test_reconcile_leaves_user_made_sync_groups_alone() -> None:
    """A sync group without an Apple group id is the user's, and never touched."""
    manager, sync_prov = _manager(
        [_player("left", SPEAKER_ALONE)],
        {"syncgroup_mine": {CONF_GROUP_MEMBERS: ["left", "kitchen"]}},
    )
    await manager.reconcile()
    sync_prov.remove_group_player.assert_not_awaited()


async def test_reconcile_dissolves_a_group_none_of_whose_members_is_discoverable() -> None:
    """A group shows only while discovery shows a member of it, as in the AirPlay menu."""
    group_id = group_player_id(HOME_THEATER_ID)
    manager, sync_prov = _manager(
        [_player("kitchen", SPEAKER_ALONE)],
        {group_id: _saved_home_theater(["right", "left", "apple_tv"])},
    )
    await manager.reconcile()
    sync_prov.remove_group_player.assert_awaited_once_with(group_id)


def _saved_groups() -> dict[str, dict[str, Any]]:
    """Return the saved configs of an Apple group and of a sync group the user made."""
    return {
        group_player_id(HOME_THEATER_ID): _saved_home_theater(["right", "apple_tv"]),
        "syncgroup_mine": {CONF_GROUP_MEMBERS: ["left", "kitchen"]},
    }


async def test_unload_removes_formed_groups() -> None:
    """Unloading stops regrouping and removes the formed groups, not the user's."""
    manager, sync_prov = _manager([], _saved_groups())
    await manager.unload()
    manager.mass.cancel_timer.assert_called_once()
    sync_prov.remove_group_player.assert_awaited_once_with(group_player_id(HOME_THEATER_ID))


async def test_unload_leaves_the_groups_while_the_server_shuts_down() -> None:
    """A shutdown leaves the players alone: the next start removes the groups."""
    manager, sync_prov = _manager([], _saved_groups())
    manager.mass.closing = True
    await manager.unload()
    sync_prov.remove_group_player.assert_not_awaited()
    manager.mass.players.delete_player_config.assert_not_called()


@pytest.mark.parametrize("registered", [True, False])
async def test_setup_removes_groups_a_previous_run_left(registered: bool) -> None:
    """A start begins without groups: one left behind is removed, with its config."""
    manager, sync_prov = _manager([], _saved_groups(), registered=registered)
    await manager.setup()
    group_id = group_player_id(HOME_THEATER_ID)
    if registered:
        sync_prov.remove_group_player.assert_awaited_once_with(group_id)
        manager.mass.players.delete_player_config.assert_not_called()
    else:
        sync_prov.remove_group_player.assert_not_awaited()
        manager.mass.players.delete_player_config.assert_called_once_with(group_id)
