"""Tests for bringing Sonos speakers back into their group after AirPlay playback started on it."""

import asyncio
import copy
import logging
from collections.abc import Awaitable, Callable
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiosonos.client import SonosLocalApiClient
from aiosonos.const import EventType as SonosEventType
from aiosonos.const import GroupEvent
from aiosonos.exceptions import FailedCommand
from aiosonos.group import SonosGroup as AiosonosGroup
from aiosonos.player import SonosPlayer as AiosonosPlayer
from music_assistant_models.player import OutputProtocol

from music_assistant.mass import MusicAssistant
from music_assistant.providers.sonos.player import SonosPlayer
from tests.common import virtual_loop_clock

HOUSEHOLD = "Sonos_household"
LEADER = "RINCON_F0F6C100000101400"
MEMBER = "RINCON_54200000000201400"
NAMES = {LEADER: "Kantoor", MEMBER: "Speelkamer"}
GROUP = f"{LEADER}:400"
MEMBER_GROUP = f"{MEMBER}:401"
AIRPLAY = OutputProtocol(output_protocol_id="ap_kantoor", name="AirPlay", protocol_domain="airplay")
RESTORE_TASK = f"restore_airplay_group_{LEADER}"


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
                "playbackState": "PLAYBACK_STATE_PLAYING",
            }
            for group_id, coordinator, members in groups
        ],
        "players": [
            {"_objectType": "player", "id": player_id, "name": NAMES[player_id]}
            for _, _, members in groups
            for player_id in members
        ],
    }


SYNCED = _groups((GROUP, LEADER, [LEADER, MEMBER]))
# Sonos took the member out of the group as AirPlay started on the leader
DROPPED = _groups((GROUP, LEADER, [LEADER]), (MEMBER_GROUP, MEMBER, [MEMBER]))
# the member Sonos moved the old group to restarts, and is missing from the household
GONE = _groups((GROUP, LEADER, [LEADER]))


class _Household:
    """A Sonos speaker that leads a group with a second one, around real aiosonos clients."""

    def __init__(self, mass: MusicAssistant) -> None:
        self.mass = mass
        self.apis = {player_id: AsyncMock() for player_id in NAMES}
        self.players = {player_id: self._player(player_id) for player_id in NAMES}
        # the household as the speakers know it right now
        self.current = SYNCED
        # whether the leader's household lists the member, so that a command may name it
        self.member_known = True
        # the loop times at which the leader was asked to add the member
        self.adds: list[float] = []

        async def _get_groups(*_args: object) -> dict[str, Any]:
            return copy.deepcopy(self.current)

        for api in self.apis.values():
            api.groups.get_groups.side_effect = _get_groups
        self.apis[LEADER].groups.modify_group_members.side_effect = self._modify_group_members

    @property
    def leader(self) -> SonosPlayer:
        """The speaker AirPlay plays on."""
        return self.players[LEADER]

    @property
    def member(self) -> SonosPlayer:
        """The speaker Sonos takes out of the group."""
        return self.players[MEMBER]

    @property
    def grouped(self) -> list[str]:
        """The members of the leader's group, as the leader reports them."""
        return list(self.leader.client.player.group_members)

    async def start_airplay(
        self, during_start: Callable[[], Awaitable[None]] | None = None
    ) -> None:
        """
        Start AirPlay playback on the leader the way the players controller does.

        :param during_start: What happens while AirPlay connects and anchors its stream.
        """
        async with self.leader.prepare_play_media():
            if during_start is not None:
                await during_start()
            await self.leader.on_protocol_playback(AIRPLAY)

    def report(self, groups: dict[str, Any]) -> None:
        """Change the household, and deliver the groups event of the leader about it."""
        self.current = groups
        self.leader.client._handle_groups_event(cast("Any", copy.deepcopy(groups)))

    def take_member_offline(self) -> None:
        """Let the member drop its connection and leave the household, as it restarts."""
        self.member.connected = False
        self.member_known = False
        self.report(GONE)

    def reconnect_member(self) -> None:
        """Let the connection of the member come back, and its client report its group."""
        self.member.connected = True
        group = next(iter(self.member.client._groups.values()))
        self.member.client.signal_event(
            GroupEvent(SonosEventType.GROUP_ADDED, group.id, group),
        )

    async def _modify_group_members(
        self, group_id: str, player_ids_to_add: list[str], _player_ids_to_remove: list[str]
    ) -> dict[str, Any]:
        """Answer a group command the way the leader does: with the resulting group."""
        assert group_id == GROUP
        if MEMBER in player_ids_to_add:
            self.adds.append(asyncio.get_running_loop().time())
            if not self.member_known:
                raise FailedCommand("playerIdsToAdd contains invalid player ids")
            self.current = SYNCED
        group = next(x for x in self.current["groups"] if x["coordinatorId"] == LEADER)
        return {"_objectType": "groupInfo", "group": copy.deepcopy(group)}

    def _player(self, player_id: str) -> SonosPlayer:
        """Create a connected SonosPlayer around a real aiosonos client."""
        player = SonosPlayer.__new__(SonosPlayer)
        player.mass = self.mass
        player._provider = MagicMock(get_protocol=lambda protocol_id: self.players.get(protocol_id))
        player.logger = logging.getLogger("test.sonos.player")
        player._protocol_id = player_id
        player._attr_name = NAMES[player_id]
        player._player = None
        player.connected = True
        player._members_before_play = set()
        player._airplay_rejoin_members = set()
        player.client = self._client(player_id)
        player.update_attributes = MagicMock()  # type: ignore[method-assign]
        player.update_state = MagicMock()  # type: ignore[method-assign, misc]
        return player

    def _client(self, player_id: str) -> SonosLocalApiClient:
        """Create the aiosonos client of one speaker, set up from the synced household."""
        client = SonosLocalApiClient("192.168.1.10", MagicMock())
        client._loop = cast("Any", asyncio.get_running_loop())
        client._player_id = player_id
        client._household_id = HOUSEHOLD
        client.api = self.apis[player_id]
        for group_data in SYNCED["groups"]:
            client._groups[group_data["id"]] = AiosonosGroup(
                client, cast("Any", copy.deepcopy(group_data))
            )
        player_data = next(x for x in SYNCED["players"] if x["id"] == player_id)
        client._player = AiosonosPlayer(client, cast("Any", dict(player_data)))
        return client


@pytest.mark.asyncio
@pytest.mark.parametrize("drop_reported_during_start", [True, False])
async def test_a_member_that_stays_online_is_restored_right_away(
    timer_mass: MusicAssistant, drop_reported_during_start: bool
) -> None:
    """A member Sonos drops from the group is added back as soon as the drop is reported."""
    home = _Household(timer_mass)
    loop = asyncio.get_running_loop()
    dropped_at = 0.0

    async def _drop_member() -> None:
        nonlocal dropped_at
        # Sonos drops the member about 0.9 s after AirPlay starts on the leader
        await asyncio.sleep(0.9)
        dropped_at = loop.time()
        home.report(DROPPED)

    async def _start_takes_a_while() -> None:
        await _drop_member()
        await asyncio.sleep(0.6)

    with virtual_loop_clock():
        if drop_reported_during_start:
            await home.start_airplay(_start_takes_a_while)
        else:
            await home.start_airplay()
            await _drop_member()
        await asyncio.sleep(10)

    assert home.grouped == [LEADER, MEMBER]
    assert len(home.adds) == 1
    assert home.adds[0] - dropped_at < 1
    assert RESTORE_TASK not in timer_mass._tracked_tasks


@pytest.mark.asyncio
async def test_a_member_that_returns_after_eight_seconds_is_restored(
    timer_mass: MusicAssistant,
) -> None:
    """A member that is offline for seconds is added back once it is back, refused or not."""
    home = _Household(timer_mass)

    with virtual_loop_clock():
        # the leader still reports its member as AirPlay starts: its groups event lags behind
        await home.start_airplay()
        await asyncio.sleep(0.9)
        home.take_member_offline()
        await asyncio.sleep(7.1)
        # the member is reachable again, while the leader's household does not list it yet
        home.reconnect_member()
        await asyncio.sleep(1)
        home.member_known = True
        home.report(DROPPED)
        await asyncio.sleep(1)

    assert home.grouped == [LEADER, MEMBER]
    # refused as an invalid player once, then accepted
    assert len(home.adds) == 2
    assert RESTORE_TASK not in timer_mass._tracked_tasks


@pytest.mark.asyncio
async def test_a_member_that_never_returns_is_reported_once(
    timer_mass: MusicAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """A member that stays offline gets no command and one warning, and the restore ends."""
    home = _Household(timer_mass)

    with virtual_loop_clock(), caplog.at_level(logging.WARNING):
        await home.start_airplay()
        await asyncio.sleep(0.9)
        home.take_member_offline()
        await asyncio.sleep(60)
        restore_running = RESTORE_TASK in timer_mass._tracked_tasks
        # a member that comes back after the restore gave up is left alone
        home.member_known = True
        home.reconnect_member()
        home.report(DROPPED)
        await asyncio.sleep(10)

    warnings = [record.getMessage() for record in caplog.records]
    assert len(warnings) == 1
    assert "Speelkamer did not return to the group of Kantoor" in warnings[0]
    assert home.adds == []
    assert not restore_running
    assert home.grouped == [LEADER]


@pytest.mark.asyncio
async def test_a_member_removed_meanwhile_is_not_restored(timer_mass: MusicAssistant) -> None:
    """A member removed from the group on purpose while it was away stays out of it."""
    home = _Household(timer_mass)

    with virtual_loop_clock():
        await home.start_airplay()
        await asyncio.sleep(0.9)
        home.take_member_offline()
        await home.leader.set_members(player_ids_to_remove=[MEMBER])
        await asyncio.sleep(5)
        home.member_known = True
        home.reconnect_member()
        home.report(DROPPED)
        await asyncio.sleep(5)

    assert home.adds == []
    assert home.grouped == [LEADER]


@pytest.mark.asyncio
async def test_a_member_grouped_on_the_airplay_route_meanwhile_is_left_there(
    timer_mass: MusicAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """A member Music Assistant added to the AirPlay session while it was away keeps playing there."""
    home = _Household(timer_mass)
    airplay_group = ["ap_kantoor"]
    leader_player = MagicMock(
        active_output_protocol="ap_kantoor",
        attached_protocols={"ap_kantoor": MagicMock(group_members=airplay_group)},
        own_protocol=home.leader,
    )
    leader_player.name = "Kantoor"
    member_player = MagicMock(own_protocol=home.member)
    home.leader._player = leader_player
    timer_mass.players = MagicMock()
    timer_mass.players.get_player_by_protocol.side_effect = {
        "ap_kantoor": leader_player,
        "ap_speelkamer": member_player,
    }.get

    with virtual_loop_clock(), caplog.at_level(logging.WARNING):
        await home.start_airplay()
        await asyncio.sleep(0.9)
        home.take_member_offline()
        await asyncio.sleep(7.1)
        # a sync group re-adds the member, on the AirPlay route the leader plays on
        airplay_group.append("ap_speelkamer")
        home.member_known = True
        home.reconnect_member()
        home.report(DROPPED)
        await asyncio.sleep(60)

    assert home.adds == []
    assert home.grouped == [LEADER]
    assert caplog.records == []
