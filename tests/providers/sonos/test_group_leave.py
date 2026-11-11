"""Tests for a Sonos speaker that is removed from the group of another speaker."""

import asyncio
import logging
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiosonos.client import SonosLocalApiClient
from aiosonos.const import EventType as SonosEventType
from aiosonos.group import SonosGroup as AiosonosGroup
from aiosonos.player import SonosPlayer as AiosonosPlayer

from music_assistant.providers.sonos.player import SonosPlayer

HOUSEHOLD = "Sonos_household"
LEADER = "RINCON_F0F6C100000101400"
MEMBER = "RINCON_38420B00000201400"
SHARED_GROUP = f"{LEADER}:400"
OWN_GROUP = f"{MEMBER}:401"


def _groups(*groups: tuple[str, str, list[str]]) -> dict[str, Any]:
    """Build a groups event (or getGroups reply) as the speakers send it."""
    return {
        "_objectType": "groups",
        "groups": [
            {
                "_objectType": "group",
                "id": group_id,
                "coordinatorId": coordinator,
                "playerIds": members,
                "name": coordinator,
                "playbackState": "PLAYBACK_STATE_IDLE",
            }
            for group_id, coordinator, members in groups
        ],
        "players": [
            {"_objectType": "player", "id": player_id, "name": player_id}
            for player_id in (LEADER, MEMBER)
        ],
    }


SYNCED = _groups((SHARED_GROUP, LEADER, [LEADER, MEMBER]))
LEFT = _groups((SHARED_GROUP, LEADER, [LEADER]), (OWN_GROUP, MEMBER, [MEMBER]))


def _client(player_id: str, api: AsyncMock) -> SonosLocalApiClient:
    """Create an aiosonos client of one speaker, set up from the synced household."""
    client = SonosLocalApiClient("192.168.1.10", MagicMock())
    client._loop = cast("Any", asyncio.get_running_loop())
    client._player_id = player_id
    client._household_id = HOUSEHOLD
    client.api = api
    for group_data in SYNCED["groups"]:
        client._groups[group_data["id"]] = AiosonosGroup(client, cast("Any", dict(group_data)))
    player_data = next(x for x in SYNCED["players"] if x["id"] == player_id)
    client._player = AiosonosPlayer(client, cast("Any", dict(player_data)))
    return client


def _player(mass: MagicMock, player_id: str, api: AsyncMock) -> SonosPlayer:
    """Create a connected SonosPlayer around a real aiosonos client."""
    player = SonosPlayer.__new__(SonosPlayer)
    player.mass = mass
    player.logger = logging.getLogger("test.sonos.player")
    player._player_id = player_id
    player._attr_name = player_id
    player.connected = True
    player.client = _client(player_id, api)
    player.update_attributes = MagicMock()  # type: ignore[method-assign]
    player.update_state = MagicMock()  # type: ignore[method-assign, misc]
    player.client.subscribe(
        player.on_player_event,
        (SonosEventType.GROUP_UPDATED, SonosEventType.PLAYER_UPDATED),
    )
    return player


@pytest.mark.asyncio
async def test_a_removed_member_reports_its_leave_before_its_own_late_event() -> None:
    """A speaker taken out of a group is no longer synced once the removal returns."""
    mass = MagicMock()
    leader_api, member_api = AsyncMock(), AsyncMock()
    leader = _player(mass, LEADER, leader_api)
    member = _player(mass, MEMBER, member_api)
    mass.players.get_player.side_effect = {LEADER: leader, MEMBER: member}.get
    synced_to_before = member.synced_to

    async def _modify_group_members(*_args: object) -> None:
        # the leader reports the leave in its groups event right away
        leader.client._handle_groups_event(cast("Any", LEFT))

    leader_api.groups.modify_group_members.side_effect = _modify_group_members
    # the member already knows it left when asked, but its own groups event comes seconds
    # later: it is never delivered here
    member_api.groups.get_groups.return_value = LEFT

    await leader.set_members(player_ids_to_remove=[MEMBER])
    await asyncio.sleep(0)

    assert synced_to_before == LEADER
    assert member.synced_to is None
    assert not member.client.player.is_passive
    assert member.client.player.group is not None
    assert member.client.player.group.id == OWN_GROUP
    cast("MagicMock", member.update_state).assert_called()
    member_api.groups.get_groups.assert_awaited_once_with(HOUSEHOLD)
