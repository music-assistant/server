"""Tests for a Sonos speaker that is removed from, or added back to, the group of another one."""

import asyncio
import logging
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiosonos.client import SonosLocalApiClient
from aiosonos.const import EventType as SonosEventType
from aiosonos.group import SonosGroup as AiosonosGroup
from aiosonos.player import SonosPlayer as AiosonosPlayer

from music_assistant.providers.sonos import player as player_module
from music_assistant.providers.sonos.player import SonosPlayer

HOUSEHOLD = "Sonos_household"
LEADER = "RINCON_F0F6C100000101400"
MEMBER = "RINCON_38420B00000201400"
OTHER = "RINCON_48A6B800000301400"
SHARED_GROUP = f"{LEADER}:400"
OWN_GROUP = f"{MEMBER}:401"
OTHER_GROUP = f"{OTHER}:402"


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
            for player_id in (LEADER, MEMBER, OTHER)
        ],
    }


SYNCED = _groups((SHARED_GROUP, LEADER, [LEADER, MEMBER, OTHER]))
LEFT = _groups((SHARED_GROUP, LEADER, [LEADER, OTHER]), (OWN_GROUP, MEMBER, [MEMBER]))
BOTH_LEFT = _groups(
    (SHARED_GROUP, LEADER, [LEADER]),
    (OWN_GROUP, MEMBER, [MEMBER]),
    (OTHER_GROUP, OTHER, [OTHER]),
)


class _Household:
    """Three connected Sonos speakers synced to one leader, around real aiosonos clients."""

    def __init__(self) -> None:
        self.mass = MagicMock()
        self.apis = {player_id: AsyncMock() for player_id in (LEADER, MEMBER, OTHER)}
        self.players = {player_id: self._player(player_id) for player_id in self.apis}
        # what the household looks like after the next change on the leader
        self.next_groups = LEFT

        async def _modify_group_members(*_args: object) -> dict[str, Any]:
            # the leader answers with the resulting group; its own groups event follows later
            return self.group_info(self.next_groups)

        self.apis[LEADER].groups.modify_group_members.side_effect = _modify_group_members

    @property
    def leader(self) -> SonosPlayer:
        """The speaker that leads the group."""
        return self.players[LEADER]

    @property
    def member(self) -> SonosPlayer:
        """The speaker that is taken out of the group."""
        return self.players[MEMBER]

    @staticmethod
    def group_info(groups: dict[str, Any]) -> dict[str, Any]:
        """Build the groupInfo the leader answers a group command with."""
        group = next(x for x in groups["groups"] if x["coordinatorId"] == LEADER)
        return {
            "_objectType": "groupInfo",
            "group": {**group, "playerIds": list(group["playerIds"])},
        }

    def answer_groups(self, player_id: str, groups: dict[str, Any]) -> None:
        """Let a speaker answer getGroups with the given household."""
        self.apis[player_id].groups.get_groups.side_effect = lambda *_args: groups

    def deliver_own_event(self, player_id: str, groups: dict[str, Any]) -> None:
        """Deliver the groups event a speaker sends seconds after the change."""
        self.players[player_id].client._handle_groups_event(cast("Any", groups))

    def _player(self, player_id: str) -> SonosPlayer:
        """Create a connected SonosPlayer around a real aiosonos client."""
        player = SonosPlayer.__new__(SonosPlayer)
        player.mass = self.mass
        player._provider = MagicMock(get_protocol=lambda protocol_id: self.players.get(protocol_id))
        player.logger = logging.getLogger("test.sonos.player")
        player._protocol_id = player_id
        player._attr_name = player_id
        player.connected = True
        player._airplay_rejoin_members = set()
        player._stepping_out = False
        player._taken_over_item_id = None
        player.client = self._client(player_id)
        player.update_attributes = MagicMock()  # type: ignore[method-assign]
        player.update_state = MagicMock()  # type: ignore[method-assign, misc]
        player.client.subscribe(
            player.on_player_event,
            (SonosEventType.GROUP_UPDATED, SonosEventType.PLAYER_UPDATED),
        )
        return player

    def _client(self, player_id: str) -> SonosLocalApiClient:
        """Create an aiosonos client of one speaker, set up from the synced household."""
        client = SonosLocalApiClient("192.168.1.10", MagicMock())
        client._loop = cast("Any", asyncio.get_running_loop())
        client._player_id = player_id
        client._household_id = HOUSEHOLD
        client.api = self.apis[player_id]
        for group_data in SYNCED["groups"]:
            client._groups[group_data["id"]] = AiosonosGroup(client, cast("Any", dict(group_data)))
        player_data = next(x for x in SYNCED["players"] if x["id"] == player_id)
        client._player = AiosonosPlayer(client, cast("Any", dict(player_data)))
        return client


async def _never_answers(*_args: object) -> None:
    """Stand in for a speaker that lost power without closing its connection."""
    await asyncio.Event().wait()


async def _settle() -> None:
    """Let everything that is ready to run, run."""
    for _ in range(20):
        await asyncio.sleep(0)


class _FrozenClock:
    """The clock of the event loop, standing still until the test moves it."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """
        Freeze the clock of the running event loop.

        :param monkeypatch: The fixture that restores the loop's clock after the test.
        """
        loop = asyncio.get_running_loop()
        self._now = loop.time()
        monkeypatch.setattr(loop, "time", lambda: self._now)

    async def advance(self, seconds: float) -> None:
        """
        Move the clock forward and run everything that falls due on the way.

        :param seconds: How far to move the clock.
        """
        await _settle()
        self._now += seconds
        await _settle()


def _remove(home: _Household, *player_ids: str) -> asyncio.Task[None]:
    """Start the removal of speakers from the group of the leader."""
    return asyncio.ensure_future(home.leader.set_members(player_ids_to_remove=list(player_ids)))


@pytest.mark.asyncio
async def test_a_removed_member_is_no_longer_synced_once_the_removal_returns() -> None:
    """A speaker that already knows it left is asked, ahead of its own late groups event."""
    home = _Household()
    home.answer_groups(MEMBER, LEFT)
    synced_to_before = home.member.synced_to

    await home.leader.set_members(player_ids_to_remove=[MEMBER])
    await asyncio.sleep(0)

    assert synced_to_before == LEADER
    assert home.member.synced_to is None
    assert not home.member.client.player.is_passive
    assert home.member.client.player.group is not None
    assert home.member.client.player.group.id == OWN_GROUP
    cast("MagicMock", home.member.update_state).assert_called()
    home.apis[MEMBER].groups.get_groups.assert_awaited_once_with(HOUSEHOLD)


@pytest.mark.asyncio
async def test_a_refresh_that_reads_the_old_grouping_leaves_it_to_the_late_event() -> None:
    """Asked too early, the speaker still reports the old group; its own event follows."""
    home = _Household()
    home.answer_groups(MEMBER, SYNCED)

    await home.leader.set_members(player_ids_to_remove=[MEMBER])
    await asyncio.sleep(0)
    synced_to_after_refresh = home.member.synced_to
    home.deliver_own_event(MEMBER, LEFT)
    await asyncio.sleep(0)

    assert synced_to_after_refresh == LEADER
    assert home.member.synced_to is None


@pytest.mark.asyncio
async def test_a_member_added_back_right_away_is_synced_again() -> None:
    """A speaker added back before it reported its leave sends no groups event at all."""
    home = _Household()
    home.answer_groups(MEMBER, LEFT)
    await home.leader.set_members(player_ids_to_remove=[MEMBER])
    synced_to_after_removal = home.member.synced_to

    # back in the group it last reported to be in, the member has nothing to report
    home.next_groups = SYNCED
    home.answer_groups(MEMBER, SYNCED)
    await home.leader.set_members(player_ids_to_add=[MEMBER])
    await asyncio.sleep(0)

    assert synced_to_after_removal is None
    assert home.member.synced_to == LEADER
    assert home.member.client.player.is_passive


@pytest.mark.asyncio
async def test_a_removal_returns_as_soon_as_the_leader_answers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The leader's answer to the command carries the new group, ahead of its late groups event."""
    home = _Household()
    home.answer_groups(MEMBER, LEFT)
    # the clock never moves: a removal that waited on a timer would not have returned
    _FrozenClock(monkeypatch)

    removal = _remove(home, MEMBER)
    await _settle()

    assert removal.done()
    await removal
    assert home.leader.client.player.group_members == [LEADER, OTHER]
    assert home.member.synced_to is None


@pytest.mark.asyncio
async def test_an_answer_without_the_group_waits_for_the_leaders_groups_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without the group in the answer, the change waits for the leader to report it."""
    home = _Household()
    home.apis[LEADER].groups.modify_group_members.side_effect = None
    home.apis[LEADER].groups.modify_group_members.return_value = {"_objectType": "groupInfo"}
    home.answer_groups(MEMBER, LEFT)
    clock = _FrozenClock(monkeypatch)

    removal = _remove(home, MEMBER)
    await clock.advance(0.5)
    returned_before_the_event = removal.done()
    home.deliver_own_event(LEADER, LEFT)
    await clock.advance(0.1)

    assert not returned_before_the_event
    assert removal.done()
    await removal
    assert home.leader.client.player.group_members == [LEADER, OTHER]
    assert home.member.synced_to is None


@pytest.mark.asyncio
async def test_a_member_that_never_answers_does_not_hold_up_the_removal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A speaker without power costs the removal no more than the bound, and no failure."""
    home = _Household()
    home.next_groups = BOTH_LEFT
    home.apis[MEMBER].groups.get_groups.side_effect = _never_answers
    home.answer_groups(OTHER, BOTH_LEFT)

    clock = _FrozenClock(monkeypatch)
    bound = player_module.GROUPS_REFRESH_TIMEOUT

    removal = _remove(home, MEMBER, OTHER)
    await clock.advance(bound - 0.5)
    waiting_for_the_member = not removal.done()
    await clock.advance(0.5)

    assert waiting_for_the_member
    assert removal.done()
    await removal
    assert home.leader.client.player.group_members == [LEADER]
    # the speaker that answers is refreshed alongside the one that does not
    assert home.players[OTHER].synced_to is None
    assert home.member.synced_to == LEADER


@pytest.mark.asyncio
async def test_a_connection_error_on_a_refresh_does_not_fail_the_removal() -> None:
    """A connection the speaker dropped is left to the speaker's own event."""
    home = _Household()
    home.next_groups = BOTH_LEFT
    home.apis[MEMBER].groups.get_groups.side_effect = ConnectionResetError(
        "Cannot write to closing transport"
    )
    home.answer_groups(OTHER, BOTH_LEFT)

    await home.leader.set_members(player_ids_to_remove=[MEMBER, OTHER])
    await asyncio.sleep(0)

    assert home.leader.client.player.group_members == [LEADER]
    assert home.players[OTHER].synced_to is None
    assert home.member.synced_to == LEADER


@pytest.mark.asyncio
async def test_members_that_never_answer_cost_the_bound_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The members of one change are asked at the same time, not one after the other."""
    home = _Household()
    home.next_groups = BOTH_LEFT
    for player_id in (MEMBER, OTHER):
        home.apis[player_id].groups.get_groups.side_effect = _never_answers

    clock = _FrozenClock(monkeypatch)

    removal = _remove(home, MEMBER, OTHER)
    # asked one after the other, the second would still be waiting once the first reached the bound
    await clock.advance(player_module.GROUPS_REFRESH_TIMEOUT)

    assert removal.done()
    await removal


@pytest.mark.asyncio
async def test_a_reconnect_during_a_refresh_does_not_fail_the_removal() -> None:
    """A reconnect cancels the reply the refresh waits on; the removal still succeeds."""
    home = _Household()
    home.next_groups = BOTH_LEFT
    home.apis[MEMBER].groups.get_groups.side_effect = asyncio.CancelledError
    home.answer_groups(OTHER, BOTH_LEFT)

    await home.leader.set_members(player_ids_to_remove=[MEMBER, OTHER])
    await asyncio.sleep(0)

    assert home.leader.client.player.group_members == [LEADER]
    assert home.players[OTHER].synced_to is None
    assert home.member.synced_to == LEADER


def _leave_on_own(home: _Household, player_id: str, groups: dict[str, Any]) -> AsyncMock:
    """Let a speaker answer getGroups with the given household once it formed a group of its own."""

    async def _create_group(*_args: object) -> dict[str, Any]:
        home.answer_groups(player_id, groups)
        return {"_objectType": "groupInfo"}

    create_group = cast("AsyncMock", home.apis[player_id].groups.create_group)
    create_group.side_effect = _create_group
    return create_group


@pytest.mark.asyncio
async def test_a_member_the_leader_did_not_remove_leaves_through_its_own_connection() -> None:
    """A leader that has not registered a re-added member answers without removing it."""
    home = _Household()
    # the leader answers with a group without the member, the member still reports it
    home.answer_groups(MEMBER, SYNCED)
    create_group = _leave_on_own(home, MEMBER, LEFT)

    await home.leader.set_members(player_ids_to_remove=[MEMBER])
    await asyncio.sleep(0)

    create_group.assert_awaited_once_with(HOUSEHOLD, [MEMBER], None)
    assert home.member.synced_to is None
    assert home.leader.client.player.group_members == [LEADER, OTHER]


@pytest.mark.asyncio
async def test_a_member_that_reports_its_own_group_makes_no_extra_call() -> None:
    """A member that already left, as the leader answered, is not asked to leave again."""
    home = _Household()
    home.answer_groups(MEMBER, LEFT)

    await home.leader.set_members(player_ids_to_remove=[MEMBER])

    home.apis[MEMBER].groups.create_group.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["error", "no_answer", "connection_closed"])
async def test_a_member_that_cannot_leave_on_its_own_does_not_fail_the_removal(
    monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    """The member's own attempt costs the removal no more than the bound, and no failure."""
    home = _Household()
    home.answer_groups(MEMBER, SYNCED)
    create_group = cast("AsyncMock", home.apis[MEMBER].groups.create_group)
    create_group.side_effect = {
        "error": OSError("Cannot write to closing transport"),
        "no_answer": _never_answers,
        # a reconnect cancels the replies the connection still waited on
        "connection_closed": asyncio.CancelledError(),
    }[failure]
    clock = _FrozenClock(monkeypatch)

    removal = _remove(home, MEMBER)
    await clock.advance(player_module.GROUPS_REFRESH_TIMEOUT)

    assert removal.done()
    await removal
    create_group.assert_awaited_once()
    assert home.member.synced_to == LEADER


@pytest.mark.asyncio
async def test_each_removed_member_is_checked_on_its_own() -> None:
    """Only the member that still reports the group leaves through its own connection."""
    home = _Household()
    home.next_groups = BOTH_LEFT
    home.answer_groups(MEMBER, SYNCED)
    member_leaves = _leave_on_own(home, MEMBER, BOTH_LEFT)
    home.answer_groups(OTHER, BOTH_LEFT)

    await home.leader.set_members(player_ids_to_remove=[MEMBER, OTHER])
    await asyncio.sleep(0)

    member_leaves.assert_awaited_once_with(HOUSEHOLD, [MEMBER], None)
    home.apis[OTHER].groups.create_group.assert_not_awaited()
    assert home.member.synced_to is None
    assert home.players[OTHER].synced_to is None


@pytest.mark.asyncio
async def test_a_cancelled_refresh_is_still_cancelled() -> None:
    """Cancelling the refresh itself is not mistaken for a reconnect."""
    home = _Household()
    home.apis[MEMBER].groups.get_groups.side_effect = _never_answers
    refresh = asyncio.ensure_future(home.member._refresh_groups())
    await _settle()

    refresh.cancel()

    with pytest.raises(asyncio.CancelledError):
        await refresh
