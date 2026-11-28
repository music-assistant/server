"""Tests for bringing Sonos speakers back into their group after AirPlay playback started on it."""

import asyncio
import copy
import logging
from collections.abc import Awaitable, Callable
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiosonos.client import SonosLocalApiClient
from aiosonos.const import EventType as SonosEventType
from aiosonos.const import GroupEvent
from aiosonos.exceptions import FailedCommand
from aiosonos.group import SonosGroup as AiosonosGroup
from aiosonos.player import SonosPlayer as AiosonosPlayer
from music_assistant_models.enums import PlaybackState

from music_assistant.mass import MusicAssistant
from music_assistant.providers.sonos.player import SonosPlayer
from tests.common import virtual_loop_clock

HOUSEHOLD = "Sonos_household"
LEADER = "RINCON_F0F6C100000101400"
MEMBER = "RINCON_54200000000201400"
NAMES = {LEADER: "Kantoor", MEMBER: "Speelkamer"}
GROUP = f"{LEADER}:400"
MEMBER_GROUP = f"{MEMBER}:401"
RESTORE_TASK = f"restore_airplay_group_{LEADER}"
AIRPLAY_ID = "ap_kantoor"


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

    def __init__(self, mass: MusicAssistant, airplay_renders: bool = False) -> None:
        """
        Set up the household.

        :param mass: The MusicAssistant instance.
        :param airplay_renders: Whether an AirPlay session already renders on the leader, so
            that a start absorbs the new media; Sonos still drops the members on a cold
            restart of that session.
        """
        self.mass = mass
        self.apis = {player_id: AsyncMock() for player_id in NAMES}
        self.players = {player_id: self._player(player_id) for player_id in NAMES}
        # the household as the speakers know it right now
        self.current = SYNCED
        # whether the leader's household lists the member, so that a command may name it
        self.member_known = True
        # the loop times at which the leader was asked to add the member
        self.adds: list[float] = []
        # the loop times at which the leader was asked to remove the member
        self.removes: list[float] = []
        # the AirPlay protocol the leader plays over; IDLE until a session renders
        self.airplay: MagicMock | None = MagicMock(
            protocol_id=AIRPLAY_ID,
            playback_state=PlaybackState.PLAYING if airplay_renders else PlaybackState.IDLE,
        )
        self.leader._airplay_output = lambda: self.airplay  # type: ignore[method-assign]
        # the leader plays over the AirPlay protocol itself
        self.leader._player = MagicMock(active_output_protocol=AIRPLAY_ID, attached_protocols={})
        self.leader._player.name = NAMES[LEADER]

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

    def play_natively(self, state: str = "PLAYBACK_STATE_PLAYING", own: bool = True) -> None:
        """
        Let the native group report a playback state, of our queue or of another source.

        :param state: The playback state the group reports.
        :param own: Whether the group plays our queue.
        """
        self.leader.cloud_queue_id = "queue" if own else None
        group = self.leader.client.player.group
        assert group is not None
        group._playback_status_data["playbackState"] = cast("Any", state)

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
        self, group_id: str, player_ids_to_add: list[str], player_ids_to_remove: list[str]
    ) -> dict[str, Any]:
        """Answer a group command the way the leader does: with the resulting group."""
        assert group_id == GROUP
        if MEMBER in player_ids_to_remove:
            self.removes.append(asyncio.get_running_loop().time())
            self.current = DROPPED
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
        player._airplay_rejoin_members = set()
        player.cloud_queue_id = None
        player.group_queue_id = None
        player._stepping_out = False
        player._taken_over_item_id = None
        player._flow_queue_id = None
        player._flow_media = None
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
    home = _Household(timer_mass, airplay_renders=True)
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
    home = _Household(timer_mass, airplay_renders=True)

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
    home = _Household(timer_mass, airplay_renders=True)

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
    home = _Household(timer_mass, airplay_renders=True)

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
async def test_a_member_of_an_ungrouped_leader_is_not_restored(timer_mass: MusicAssistant) -> None:
    """A member stays out of the group when its leader was ungrouped while it was away."""
    home = _Household(timer_mass, airplay_renders=True)

    with virtual_loop_clock():
        await home.start_airplay()
        await asyncio.sleep(0.9)
        home.take_member_offline()
        await home.leader.ungroup()
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
    home = _Household(timer_mass, airplay_renders=True)
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


@pytest.mark.asyncio
async def test_members_are_taken_out_before_airplay_starts_and_added_back_after(
    timer_mass: MusicAssistant,
) -> None:
    """The members leave the group before a new AirPlay session starts, and rejoin once it plays."""
    home = _Household(timer_mass)
    home.play_natively()
    loop = asyncio.get_running_loop()
    during_start: list[str] = []

    async def _connect_airplay() -> None:
        during_start.extend(home.grouped)
        await asyncio.sleep(4)

    with virtual_loop_clock(), patch.object(AiosonosGroup, "stop", new_callable=AsyncMock) as stop:
        await home.start_airplay(_connect_airplay)
        started_at = loop.time()
        await asyncio.sleep(1)

    assert during_start == [LEADER]
    stop.assert_awaited_once()
    assert home.leader.cloud_queue_id is None
    assert home.grouped == [LEADER, MEMBER]
    assert len(home.removes) == 1
    assert len(home.adds) == 1
    assert home.adds[0] - started_at < 0.1
    assert RESTORE_TASK not in timer_mass._tracked_tasks


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("own_queue", "state", "left"),
    [
        (True, "PLAYBACK_STATE_PLAYING", True),
        (True, "PLAYBACK_STATE_PAUSED", True),
        (True, "PLAYBACK_STATE_IDLE", False),
        (False, "PLAYBACK_STATE_PLAYING", False),
    ],
)
async def test_the_native_session_is_left_only_when_it_is_ours_and_live(
    timer_mass: MusicAssistant, own_queue: bool, state: str, left: bool
) -> None:
    """Only our own playing or paused session is left; the members are taken out either way."""
    home = _Household(timer_mass)
    home.play_natively(state, own=own_queue)

    with virtual_loop_clock(), patch.object(AiosonosGroup, "stop", new_callable=AsyncMock) as stop:
        await home.start_airplay()
        await asyncio.sleep(1)

    assert stop.await_count == (1 if left else 0)
    assert len(home.removes) == 1
    assert home.grouped == [LEADER, MEMBER]


@pytest.mark.asyncio
async def test_members_stay_when_the_airplay_session_already_renders(
    timer_mass: MusicAssistant,
) -> None:
    """A start that an AirPlay session already rendering absorbs leaves the group as it is."""
    home = _Household(timer_mass, airplay_renders=True)
    home.play_natively()

    with virtual_loop_clock(), patch.object(AiosonosGroup, "stop", new_callable=AsyncMock) as stop:
        await home.start_airplay()
        await asyncio.sleep(60)

    stop.assert_not_awaited()
    assert home.removes == []
    assert home.adds == []
    assert home.grouped == [LEADER, MEMBER]


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["native", "no_members", "not_coordinator"])
async def test_nothing_happens_without_native_members_to_keep(
    timer_mass: MusicAssistant, case: str
) -> None:
    """Native playback, a speaker on its own and a group member are left alone."""
    home = _Household(timer_mass)
    speaker = home.leader
    if case == "native":
        home.airplay = None
    elif case == "no_members":
        home.report(DROPPED)
    else:
        speaker = home.member
        speaker._airplay_output = lambda: home.airplay  # type: ignore[method-assign]

    with virtual_loop_clock(), patch.object(AiosonosGroup, "stop", new_callable=AsyncMock) as stop:
        async with speaker.prepare_play_media():
            assert f"restore_airplay_group_{speaker.protocol_id}" not in timer_mass._tracked_tasks
        restoring = f"restore_airplay_group_{speaker.protocol_id}" in timer_mass._tracked_tasks

    stop.assert_not_awaited()
    assert not restoring
    for api in home.apis.values():
        api.groups.modify_group_members.assert_not_called()


@pytest.mark.asyncio
async def test_members_return_when_playback_fails_after_they_were_taken_out(
    timer_mass: MusicAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """The members rejoin the group when AirPlay playback fails to start."""
    home = _Household(timer_mass)

    async def _fail() -> None:
        await asyncio.sleep(2)
        raise RuntimeError("AirPlay did not start")

    with virtual_loop_clock(), caplog.at_level(logging.WARNING):
        with pytest.raises(RuntimeError):
            await home.start_airplay(_fail)
        await asyncio.sleep(1)

    assert len(home.removes) == 1
    assert len(home.adds) == 1
    assert home.grouped == [LEADER, MEMBER]
    assert caplog.records == []


@pytest.mark.asyncio
async def test_a_member_removed_during_the_start_is_not_added_back(
    timer_mass: MusicAssistant,
) -> None:
    """A member removed on purpose while AirPlay starts stays out of the group."""
    home = _Household(timer_mass)

    async def _remove_member() -> None:
        await home.leader.set_members(player_ids_to_remove=[MEMBER])

    with virtual_loop_clock():
        await home.start_airplay(_remove_member)
        await asyncio.sleep(60)

    assert home.adds == []
    assert home.grouped == [LEADER]


@pytest.mark.asyncio
async def test_members_a_running_restore_still_waits_for_are_carried_over(
    timer_mass: MusicAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """A new start takes over the members the previous restore did not bring back yet."""
    home = _Household(timer_mass, airplay_renders=True)

    with virtual_loop_clock(), caplog.at_level(logging.WARNING):
        await home.start_airplay()
        await asyncio.sleep(0.9)
        home.take_member_offline()
        await asyncio.sleep(20)
        # the next track starts a new AirPlay session while the member is still away
        assert home.airplay is not None
        home.airplay.playback_state = PlaybackState.IDLE
        await home.start_airplay()
        await asyncio.sleep(20)
        home.member_known = True
        home.reconnect_member()
        home.report(DROPPED)
        await asyncio.sleep(1)

    assert home.removes == []
    assert home.grouped == [LEADER, MEMBER]
    assert caplog.records == []


@pytest.mark.asyncio
async def test_a_derived_transport_leaves_the_session_and_keeps_the_members(
    timer_mass: MusicAssistant,
) -> None:
    """Over a derived transport the members stay in, and rejoin once Sonos dropped them."""
    home = _Household(timer_mass)
    home.play_natively()
    leader_player = home.leader.player
    assert leader_player is not None
    # a Sendspin bridge, which starts the AirPlay session from its first audio chunk
    leader_player.active_output_protocol = "sendspin_kantoor"  # type: ignore[misc]
    during_start: list[str] = []

    async def _start_bridge() -> None:
        during_start.extend(home.grouped)

    with virtual_loop_clock(), patch.object(AiosonosGroup, "stop", new_callable=AsyncMock) as stop:
        await home.start_airplay(_start_bridge)
        await asyncio.sleep(1)
        restoring = RESTORE_TASK in timer_mass._tracked_tasks
        # the bridge started AirPlay, and Sonos dropped the member
        home.report(DROPPED)
        await asyncio.sleep(1)

    stop.assert_awaited_once()
    assert during_start == [LEADER, MEMBER]
    assert home.removes == []
    assert restoring
    assert len(home.adds) == 1
    assert home.grouped == [LEADER, MEMBER]


@pytest.mark.asyncio
@pytest.mark.parametrize("failing", ["leave", "remove"])
async def test_a_failing_step_skips_neither_the_other_nor_the_start(
    timer_mass: MusicAssistant, failing: str
) -> None:
    """A failed session leave or member removal leaves the rest of the start as it is."""
    home = _Household(timer_mass)
    home.play_natively()
    modify = home.apis[LEADER].groups.modify_group_members
    if failing == "remove":
        modify.side_effect = FailedCommand("the speaker refused")
    started = False

    async def _start() -> None:
        nonlocal started
        started = True

    with (
        virtual_loop_clock(),
        patch.object(
            AiosonosGroup,
            "stop",
            new_callable=AsyncMock,
            side_effect=FailedCommand("no session") if failing == "leave" else None,
        ) as stop,
    ):
        await home.start_airplay(_start)

    assert started
    stop.assert_awaited_once()
    # the first group command is the removal, whatever the restore sends after it
    assert modify.call_args_list[0].args[2] == [MEMBER]
    assert home.leader.cloud_queue_id == (None if failing == "remove" else "queue")
