"""
The plan of a group change and the decisions of its join, one rule per test, on snapshots built by hand.

A test that pins a known shortcoming says so in its docstring. How the join carries its
decisions out, call by call, is tested in test_grouping_join.py.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from typing import Any

import pytest
from music_assistant_models.enums import PlaybackState, PlayerFeature, PlayerType, ProtocolRole
from music_assistant_models.errors import (
    PlayerCommandFailed,
    PlayerUnavailableError,
    UnsupportedFeaturedException,
)

from music_assistant.controllers.players.grouping_plan import (
    ChangeKind,
    Detach,
    GroupChange,
    GroupCommand,
    GroupingSnapshot,
    GroupPlan,
    GroupProgress,
    GroupRules,
    GroupStep,
    Join,
    JoinLists,
    MemberChange,
    MemberLists,
    MemberStage,
    Note,
    OldProtocol,
    OutputView,
    PlayerView,
    PrepareForPlayback,
    ProtocolLeg,
    ProtocolView,
    QueueView,
    Refuse,
    Regroup,
    ReleaseSession,
    SetMembers,
    TransferLeadership,
    Ungroup,
    elect_ad_hoc_leader,
    plan_group_change,
    set_members_group,
)

GROUPS = frozenset({PlayerFeature.SET_MEMBERS, PlayerFeature.PLAY_MEDIA})
PLAYS = frozenset({PlayerFeature.PLAY_MEDIA})
PLAYING = PlaybackState.PLAYING


@dataclass
class _World:
    """Players and protocols built by hand, turned into a snapshot."""

    players: dict[str, PlayerView] = field(default_factory=dict)
    protocols: dict[str, ProtocolView] = field(default_factory=dict)
    aliases: dict[str, str] = field(default_factory=dict)

    def protocol(
        self, protocol_id: str, player_id: str, domain: str, **values: Any
    ) -> ProtocolView:
        """
        Add a protocol of a player: an audio protocol that groups, unless the values say else.

        :param values: ``role``, ``features``, ``members`` (its group), ``synced_to``, ``can``
            (what it can group with), ``peers`` (its native peers), ``own_stream``,
            ``prefers_native``, ``available``.
        """
        protocol = ProtocolView(
            protocol_id=protocol_id,
            player_id=player_id,
            domain=domain,
            provider_id=f"{domain}_instance",
            provider_name=f"{domain.title()} provider",
            role=values.get("role", ProtocolRole.AUDIO),
            features=values.get("features", GROUPS),
            available_for_playback=values.get("available", True),
            group_members=values.get("members", ()),
            synced_to=values.get("synced_to"),
            can_group_with=frozenset(values.get("can", ())),
            native_peers=frozenset(values.get("peers", ())),
            needs_own_stream=values.get("own_stream", False),
            prefers_native=values.get("prefers_native", False),
        )
        self.protocols[protocol_id] = protocol
        return protocol

    def player(self, player_id: str, *protocol_ids: str, **values: Any) -> PlayerView:
        """Add a player made of the given protocols (the first native one is its own)."""
        protocols = [self.protocols[protocol_id] for protocol_id in protocol_ids]
        own = next((p for p in protocols if p.role != ProtocolRole.AUDIO), None)
        audio = tuple(p.protocol_id for p in protocols if p.role == ProtocolRole.AUDIO)
        outputs: list[OutputView] = []
        if own is not None and PlayerFeature.PLAY_MEDIA in own.features:
            outputs.append(
                OutputView(
                    output_id="native",
                    protocol_id=own.protocol_id,
                    domain=own.domain,
                    available=own.available_for_playback,
                    is_native=True,
                )
            )
        outputs += [
            OutputView(
                output_id=protocol_id,
                protocol_id=protocol_id,
                domain=self.protocols[protocol_id].domain,
                available=self.protocols[protocol_id].available_for_playback,
                is_native=False,
            )
            for protocol_id in audio
        ]
        view = PlayerView(
            player_id=player_id,
            name=f"Player {player_id}",
            type=values.pop("type", PlayerType.PLAYER),
            features=values.pop("features", frozenset({PlayerFeature.SET_MEMBERS})),
            playback_owner=values.pop("playback_owner", player_id),
            own_protocol=own.protocol_id if own else None,
            audio_protocols=audio,
            outputs=tuple(outputs),
            **values,
        )
        self.players[player_id] = view
        return view

    def snapshot(self) -> GroupingSnapshot:
        """Return the snapshot of what was added."""
        return GroupingSnapshot(
            players=dict(self.players), protocols=dict(self.protocols), aliases=self.aliases
        )


def _plan(
    world: _World | GroupingSnapshot,
    kind: ChangeKind,
    target: str,
    add: tuple[str, ...] | None = None,
    remove: tuple[str, ...] | None = None,
    *,
    new_content: bool = False,
    progress: GroupProgress | None = None,
) -> GroupPlan:
    """Plan a change on the world."""
    snapshot = world.snapshot() if isinstance(world, _World) else world
    change = GroupChange(
        kind=kind, target_id=target, add=add, remove=remove, new_content=new_content
    )
    return plan_group_change(snapshot, change, progress)


def _calls(plan: GroupPlan) -> list[Any]:
    """Return the steps of a plan that act, without its log lines."""
    return [step for step in plan.steps if not isinstance(step, Note)]


def _all_plans(
    world: _World, kind: ChangeKind, target: str, add: tuple[str, ...] | None = None, **values: Any
) -> GroupPlan:
    """Plan a change to its end on a world that does not change: all its steps, and its join."""
    plan = _plan(world, kind, target, add, values.pop("remove", None), **values)
    steps = list(plan.steps)
    while plan.progress is not None:
        plan = _plan(world, kind, target, add, values.get("remove"), progress=plan.progress)
        steps += plan.steps
    return GroupPlan(tuple(steps), join=plan.join, regroup=plan.regroup)


type _Call = tuple[str, tuple[str, ...], tuple[str, ...]]


def _decided(
    world: _World,
    target: str,
    add: tuple[str, ...] | None = None,
    remove: tuple[str, ...] | None = None,
) -> tuple[list[_Call], list[GroupStep]]:
    """
    Return the set_members calls a member change decides on a world that does not change.

    The admission is planned to its end first. The calls are the join call of the group's
    protocol and the call of the leader's own protocol, as (protocol id, add, remove). The log
    lines of the admission and of the decisions come with them.
    """
    plan = _all_plans(world, ChangeKind.MEMBERS, target, add, remove=remove)
    notes = list(plan.steps)
    calls: list[_Call] = []
    if plan.join is None:
        return calls, notes
    rules = GroupRules(world.snapshot(), notes.append)
    leader = rules.held_player(target)
    lists = rules.translate(leader, plan.join.add, plan.join.remove)
    leg = rules.protocol_leg(leader, lists)
    assert not isinstance(leg, Refuse)
    if leg is not None:
        calls.append((leg.protocol_id, leg.add, leg.remove))
    if native := rules.native_leg(leader, lists.native_add, lists.native_remove):
        calls.append((native.protocol_id, native.add, native.remove))
    return calls, notes


def _notes(plan: GroupPlan | list[GroupStep], level: int = logging.INFO) -> list[str]:
    """Return the log lines of a plan, or of steps, at the level or above."""
    steps = plan.steps if isinstance(plan, GroupPlan) else plan
    return [
        step.message % step.args for step in steps if isinstance(step, Note) and step.level >= level
    ]


def _sonos(world: _World, *names: str, can: bool = True, **values: Any) -> None:
    """Add native players of one provider that group natively."""
    ids = [f"sonos_{name}" for name in names]
    for name, protocol_id in zip(names, ids, strict=True):
        world.protocol(
            protocol_id,
            name,
            "sonos",
            role=ProtocolRole.NATIVE,
            can=tuple(other for other in ids if other != protocol_id) if can else (),
        )
        world.player(name, protocol_id, can_group_with=frozenset(names) - {name}, **values)


def _members(calls: list[_Call], protocol_id: str) -> list[tuple[tuple[str, ...], tuple[str, ...]]]:
    """Return the member lists of the calls on a protocol."""
    return [(add, remove) for called, add, remove in calls if called == protocol_id]


# -- routing --


def test_a_target_without_grouping_is_refused() -> None:
    """A target without SET_MEMBERS in its state refuses the change."""
    world = _World()
    _sonos(world, "a", "b", features=frozenset())
    plan = _plan(world, ChangeKind.ROUTE, "a", ("b",))
    assert isinstance(plan.steps[-1], Refuse)
    assert plan.steps[-1].error is UnsupportedFeaturedException


def test_a_member_change_on_a_held_target_goes_to_its_group() -> None:
    """A target held by a group player with SET_MEMBERS passes the change on to it."""
    world = _World()
    _sonos(world, "a", "b", active_group="g", playback_owner="g")
    world.protocol("group_g", "g", "sync_group", role=ProtocolRole.VIRTUAL)
    world.player("g", "group_g", type=PlayerType.GROUP)
    plan = _plan(world, ChangeKind.ROUTE, "a", ("b",), None)
    assert plan.passed_on
    assert _calls(plan) == [GroupCommand(target_id="g", add=("b",), remove=None)]
    assert set_members_group(world.snapshot(), "a") == "g"


def test_a_target_held_by_a_group_without_grouping_keeps_the_change() -> None:
    """A group player without SET_MEMBERS is not passed the change."""
    world = _World()
    _sonos(world, "a", "b", active_group="g", playback_owner="g")
    world.protocol("group_g", "g", "sync_group", role=ProtocolRole.VIRTUAL, features=PLAYS)
    world.player("g", "group_g", type=PlayerType.GROUP, features=frozenset())
    plan = _plan(world, ChangeKind.ROUTE, "a", ("b",))
    assert not plan.passed_on
    assert _calls(plan) == []


def test_a_synced_target_is_released_first() -> None:
    """A synced target leaves its leader first, waited for, a failure only warned."""
    world = _World()
    _sonos(world, "a", "b", synced_to="c")
    plan = _plan(world, ChangeKind.ROUTE, "a", ("b",))
    assert _calls(plan) == [
        Detach(
            member_id="a",
            wait_for_update=True,
            failure=_fail("warn"),
            message="Failed to auto-ungroup %s from %s, proceeding anyway",
            args=("Player a", "c"),
        )
    ]
    assert _notes(plan) == [
        "Player Player a is already synced to c, ungrouping it first before setting members"
    ]


def _fail(name: str) -> Any:
    """Return a failure policy by name."""
    from music_assistant.controllers.players.grouping_plan import Failure  # noqa: PLC0415

    return Failure(name)


def test_a_legacy_id_names_its_player_unless_a_player_has_that_id() -> None:
    """An alias resolves to its player; a live player's own id wins over it."""
    world = _World()
    _sonos(world, "a", "b")
    world.aliases["old_b"] = "b"
    snapshot = world.snapshot()
    assert snapshot.player("old_b") is snapshot.players["b"]
    assert snapshot.player("a") is snapshot.players["a"]
    assert snapshot.player_of("sonos_b") is snapshot.players["b"]
    assert snapshot.player("unknown") is None


# -- the leader removes itself --


def _leader_with_members(world: _World, queue: QueueView | None, **member_values: Any) -> None:
    """Add leader a holding b and c natively."""
    world.protocol(
        "sonos_a", "a", "sonos", role=ProtocolRole.NATIVE, members=("sonos_a", "sonos_b", "sonos_c")
    )
    world.player("a", "sonos_a", group_members=("a", "b", "c"), active_queue=queue)
    for name in ("b", "c"):
        world.protocol(f"sonos_{name}", name, "sonos", role=ProtocolRole.NATIVE)
        world.player(name, f"sonos_{name}", synced_to="a", **member_values)


def test_a_leader_playing_its_own_queue_hands_the_group_over() -> None:
    """The remaining members that can play take the group over, nothing else happens."""
    world = _World()
    _leader_with_members(world, QueueView(queue_id="a", state=PLAYING))
    plan = _plan(world, ChangeKind.MEMBERS, "a", ("x",), ("a",))
    assert plan.steps == (TransferLeadership(leader_id="a", remaining=("b", "c")),)


@pytest.mark.parametrize(
    "queue",
    [
        None,
        QueueView(queue_id="a", state=PlaybackState.IDLE),
        QueueView(queue_id="g", state=PLAYING),
    ],
)
def test_a_leader_without_a_playing_queue_of_its_own_dissolves(queue: QueueView | None) -> None:
    """The group is dissolved natively and the leader stopped afterwards."""
    world = _World()
    _leader_with_members(world, queue)
    plan = _plan(world, ChangeKind.MEMBERS, "a", ("x",), ("a",))
    assert plan.join == Join(target_id="a", remove=("b", "c"), stop_after=True)
    assert _decided(world, "a", ("x",), ("a",))[0] == [("sonos_a", (), ("sonos_b", "sonos_c"))]
    assert "Dissolving sync group of player Player a as it is being removed from itself" in _notes(
        plan
    )


def test_a_group_of_displays_only_has_no_heir() -> None:
    """Members that can not play audio never take the group over."""
    world = _World()
    _leader_with_members(world, QueueView(queue_id="a", state=PLAYING), type=PlayerType.VISUALIZER)
    plan = _plan(world, ChangeKind.MEMBERS, "a", None, ("a",))
    assert not any(isinstance(step, TransferLeadership) for step in plan.steps)


def test_a_group_player_removing_itself_runs_the_leader_path() -> None:
    """
    A group player removed from itself takes the path of a leader that leaves its group.

    It dissolves: its members are removed from it, without the stop that ends a leader's playback.
    This is a known shortcoming, kept as it was; this test changes when it is fixed.
    """
    world = _World()
    world.protocol("group_g", "g", "sync_group", role=ProtocolRole.VIRTUAL, members=("a", "b"))
    world.player("g", "group_g", type=PlayerType.GROUP, group_members=("a", "b"))
    _sonos(world, "a", "b")
    plan = _plan(world, ChangeKind.MEMBERS, "g", None, ("g",))
    assert _calls(plan) == [
        SetMembers(protocol_id="group_g", add=(), remove=("a", "b"), lists=MemberLists.AS_THEY_ARE)
    ]


@pytest.mark.parametrize(
    ("domain_of_b", "domain_of_c", "expected"),
    [("airplay", "airplay", "b"), ("dlna", "airplay", "c"), ("sendspin", "dlna", "b")],
)
def test_the_heir_can_be_reached_on_the_groups_protocol(
    domain_of_b: str, domain_of_c: str, expected: str
) -> None:
    """The heir is the first member on the domain the group plays on, else the first one."""
    world = _World()
    world.protocol("airplay_a", "a", "airplay")
    world.player("a", "airplay_a", active_output="airplay_a")
    world.protocol("x_b", "b", domain_of_b)
    world.player("b", "x_b")
    world.protocol("x_c", "c", domain_of_c)
    world.player("c", "x_c")
    assert elect_ad_hoc_leader(world.snapshot(), "a", ["b", "c"]) == expected


def test_a_player_that_can_not_play_audio_never_leads() -> None:
    """A visualizer is passed over even when it comes first."""
    world = _World()
    world.protocol("airplay_a", "a", "airplay")
    world.player("a", "airplay_a")
    world.protocol("airplay_v", "v", "airplay")
    world.player("v", "airplay_v", type=PlayerType.VISUALIZER)
    world.protocol("airplay_b", "b", "airplay")
    world.player("b", "airplay_b")
    assert elect_ad_hoc_leader(world.snapshot(), "a", ["v", "b"]) == "b"


# -- admission --


def test_the_target_and_a_repeated_member_are_skipped_silently() -> None:
    """The target itself and a member asked twice join once, without a log line."""
    world = _World()
    _sonos(world, "a", "b")
    plan = _plan(world, ChangeKind.MEMBERS, "a", ("a", "b", "b"))
    assert plan.join == Join(target_id="a", add=("b",))
    assert [step for step in plan.steps if isinstance(step, PrepareForPlayback)] == [
        PrepareForPlayback(player_id="b")
    ]
    assert _notes(plan, logging.WARNING) == []


def test_an_unknown_or_unavailable_member_is_left_out() -> None:
    """A member that is unknown or can not play now is left out with a warning."""
    world = _World()
    _sonos(world, "a", "b")
    world.players["b"] = replace(world.players["b"], available_for_playback=False)
    plan = _plan(world, ChangeKind.MEMBERS, "a", ("nobody", "b"))
    assert _notes(plan, logging.WARNING) == [
        "Player nobody is not available",
        "Player b is not available",
    ]
    assert plan.join == Join(target_id="a")


def test_a_switched_off_member_is_left_out_without_a_warning() -> None:
    """A member that is switched off at its power control is left out, which is no warning."""
    world = _World()
    _sonos(world, "a", "b", "c")
    world.players["b"] = replace(
        world.players["b"], available_for_playback=False, switched_off=True
    )
    plan = _plan(world, ChangeKind.MEMBERS, "a", ("b", "c"))
    assert _notes(plan, logging.WARNING) == []
    assert _notes(plan, logging.DEBUG) == [
        "Player Player b is switched off at its power control, it is left out for now"
    ]
    assert plan.join == Join(target_id="a", add=("c",))


def test_a_member_the_target_can_not_group_with_is_left_out() -> None:
    """A member missing from the target's can_group_with is left out with a warning."""
    world = _World()
    _sonos(world, "a", "b")
    world.players["a"] = replace(world.players["a"], can_group_with=frozenset())
    plan = _plan(world, ChangeKind.MEMBERS, "a", ("b",))
    assert _notes(plan, logging.WARNING) == ["Player Player b can not be grouped with Player a"]
    assert _calls(plan) == []
    assert plan.join == Join(target_id="a")


@pytest.mark.parametrize("held_as", ["synced_to", "active_group"])
def test_a_member_already_in_the_group_is_skipped(held_as: str) -> None:
    """A listed member synced to the target, or held by it, joins nothing again."""
    world = _World()
    _sonos(world, "a", "b")
    world.players["a"] = replace(world.players["a"], group_members=("a", "b"))
    member = world.players["b"]
    world.players["b"] = (
        replace(member, synced_to="a")
        if held_as == "synced_to"
        else replace(member, active_group="a")
    )
    plan = _plan(world, ChangeKind.MEMBERS, "a", ("b",))
    assert _calls(plan) == []
    assert plan.join == Join(target_id="a")


def test_a_member_synced_elsewhere_is_released_and_the_plan_stops() -> None:
    """The release ends the plan; the next one goes on with that member."""
    world = _World()
    _sonos(world, "a", "b", "c")
    world.players["b"] = replace(world.players["b"], synced_to="c")
    plan = _plan(world, ChangeKind.MEMBERS, "a", ("b",))
    assert isinstance(_calls(plan)[-1], Detach)
    assert plan.progress == GroupProgress(next_add=0, stage=MemberStage.RELEASED)
    # the release worked: the next plan, on the new state, admits the member without checks
    world.players["b"] = replace(world.players["b"], synced_to=None)
    next_plan = _plan(world, ChangeKind.MEMBERS, "a", ("b",), progress=plan.progress)
    assert next_plan.join == Join(target_id="a", add=("b",))
    assert next_plan.progress is None


def test_a_member_held_by_a_group_without_sync_is_not_released() -> None:
    """
    A member a group holds without being synced joins without being released from it.

    b's active group is g, so g still holds b after b joined a.
    This is a known shortcoming, kept as it was; this test changes when it is fixed.
    """
    world = _World()
    _sonos(world, "a", "b", active_group="g")
    world.players["a"] = replace(world.players["a"], active_group=None)
    plan = _plan(world, ChangeKind.MEMBERS, "a", ("b",))
    assert not any(isinstance(step, Detach) for step in plan.steps)
    assert plan.join == Join(target_id="a", add=("b",))


def _airplay_leaders(world: _World, state: PlaybackState = PlaybackState.IDLE) -> None:
    """Add AirPlay speakers a and b, with b leading c natively on its own stream."""
    for name in ("a", "b", "c"):
        world.protocol(
            f"airplay_{name}",
            name,
            "airplay",
            role=ProtocolRole.NATIVE,
            own_stream=True,
            members=("airplay_b", "airplay_c") if name == "b" else (),
        )
    world.player("a", "airplay_a", can_group_with=frozenset({"b"}))
    world.player("b", "airplay_b", group_members=("b", "c"), playback_state=state)
    world.player("c", "airplay_c", synced_to="b")


def test_a_member_whose_group_rides_its_own_stream_dissolves_it_first() -> None:
    """Its members are removed and the plan ends there; a failure leaves the member out."""
    world = _World()
    _airplay_leaders(world)
    plan = _plan(world, ChangeKind.MEMBERS, "a", ("b",))
    assert _calls(plan)[-1] == MemberChange(
        target_id="b",
        remove=("c",),
        failure=_fail("leave_out"),
        message="Could not dissolve the group of %s, leaving it out of %s",
        args=("Player b", "Player a"),
    )
    assert plan.progress == GroupProgress(next_add=0, stage=MemberStage.DISSOLVED)
    assert plan.progress_left_out == GroupProgress(next_add=1)


def test_a_member_still_serving_its_own_group_is_left_out() -> None:
    """A member that plays for its own group is left out with a warning."""
    world = _World()
    _airplay_leaders(world, state=PLAYING)
    plan = _plan(world, ChangeKind.MEMBERS, "a", ("b",))
    assert _notes(plan, logging.WARNING) == [
        "Player Player b is still serving its own group, leaving it out of Player a"
    ]
    assert _calls(plan) == []
    assert plan.join == Join(target_id="a")


def test_a_member_whose_group_survives_joins_as_it_is() -> None:
    """Without the own-stream rule, or without followers, there is nothing to dissolve."""
    world = _World()
    _sonos(world, "a", "b", "c")
    world.players["b"] = replace(world.players["b"], group_members=("b", "c"))
    plan = _plan(world, ChangeKind.MEMBERS, "a", ("b",))
    assert not any(isinstance(step, MemberChange) for step in plan.steps)
    assert plan.join == Join(target_id="a", add=("b",))


def test_a_member_that_powers_on_ends_the_plan() -> None:
    """
    Powering a member on ends the plan; the next plan groups it with the others.

    The power on runs the member's auto play, so it may start playing before it joins.
    This is a known shortcoming, kept as it was; this test changes when it is fixed.
    """
    world = _World()
    _sonos(world, "a", "b", "c")
    world.players["b"] = replace(world.players["b"], powered=False, power_control="fake")
    plan = _plan(world, ChangeKind.MEMBERS, "a", ("b", "c"))
    assert _calls(plan) == [PrepareForPlayback(player_id="b")]
    assert plan.progress == GroupProgress(next_add=1, admitted=("b",))
    next_plan = _plan(world, ChangeKind.MEMBERS, "a", ("b", "c"), progress=plan.progress)
    assert next_plan.join == Join(target_id="a", add=("b", "c"))


def test_a_later_member_is_checked_on_the_state_after_the_earlier_release() -> None:
    """
    A later member is checked on the state after an earlier member's release.

    j leads i, so the target offers j only once i left it: listed after i, j joins; listed
    before i, j is left out, so the outcome depends on the order of the request.
    This is a known shortcoming, kept as it was; this test changes when it is fixed.
    """
    world = _World()
    _sonos(world, "a", "i", "j")
    world.players["i"] = replace(world.players["i"], synced_to="j")
    world.players["a"] = replace(world.players["a"], can_group_with=frozenset({"i"}))
    first = _plan(world, ChangeKind.MEMBERS, "a", ("i", "j"))
    assert first.progress is not None
    # i left j, which is no leader any more and is now offered by the target
    world.players["i"] = replace(world.players["i"], synced_to=None)
    world.players["a"] = replace(world.players["a"], can_group_with=frozenset({"i", "j"}))
    second = _plan(world, ChangeKind.MEMBERS, "a", ("i", "j"), progress=first.progress)
    assert second.join == Join(target_id="a", add=("i", "j"))


# -- removals --


def test_only_members_of_the_group_are_removed() -> None:
    """A listed member, or one that reports being synced to the target, is removed."""
    world = _World()
    _sonos(world, "a", "b", "c", "d")
    world.players["a"] = replace(world.players["a"], group_members=("a", "b"))
    world.players["c"] = replace(world.players["c"], synced_to="a")
    plan = _plan(world, ChangeKind.MEMBERS, "a", None, ("b", "c", "d"))
    assert plan.join == Join(target_id="a", remove=("b", "c"))
    assert _decided(world, "a", None, ("b", "c", "d"))[0] == [
        ("sonos_a", (), ("sonos_b", "sonos_c"))
    ]


# -- a group player as target --


def test_a_group_player_takes_its_member_lists_as_they_are() -> None:
    """Its own protocol is called with both lists, empty ones included."""
    world = _World()
    world.protocol("group_g", "g", "sync_group", role=ProtocolRole.VIRTUAL)
    world.player("g", "group_g", type=PlayerType.GROUP, can_group_with=frozenset({"a"}))
    _sonos(world, "a")
    plan = _plan(world, ChangeKind.MEMBERS, "g", ("a",), ("nobody",))
    assert _calls(plan)[-1] == SetMembers(
        protocol_id="group_g", add=("a",), remove=(), lists=MemberLists.AS_THEY_ARE
    )


def test_a_group_without_grouping_reached_internally_calls_nothing() -> None:
    """Without SET_MEMBERS the removal goes through the protocol path, which ends empty."""
    world = _World()
    world.protocol("group_g", "g", "sync_group", role=ProtocolRole.VIRTUAL, features=PLAYS)
    world.player("g", "group_g", type=PlayerType.GROUP, features=frozenset(), group_members=("a",))
    _sonos(world, "a")
    plan = _plan(world, ChangeKind.MEMBERS, "g", None, ("a",))
    assert _calls(plan) == []


# -- which protocol carries a member --


def _mixed(
    world: _World, *, leader_state: PlaybackState = PlaybackState.IDLE, **leader: Any
) -> None:
    """Add Sonos leader s (AirPlay too), Sonos b (AirPlay too) and WiiM w (AirPlay only)."""
    world.protocol("sonos_s", "s", "sonos", role=ProtocolRole.NATIVE, can=("sonos_b",))
    world.protocol("airplay_s", "s", "airplay", can=("airplay_b", "airplay_w"))
    world.player(
        "s",
        "sonos_s",
        "airplay_s",
        can_group_with=frozenset({"b", "w"}),
        playback_state=leader_state,
        **leader,
    )
    world.protocol("sonos_b", "b", "sonos", role=ProtocolRole.NATIVE)
    world.protocol("airplay_b", "b", "airplay")
    world.player("b", "sonos_b", "airplay_b")
    world.protocol("wiim_w", "w", "wiim", role=ProtocolRole.NATIVE, features=PLAYS)
    world.protocol("airplay_w", "w", "airplay")
    world.player("w", "wiim_w", "airplay_w")


def test_a_native_peer_groups_on_the_own_protocol() -> None:
    """A member of the same provider groups natively, by its own protocol id."""
    world = _World()
    _mixed(world)
    assert _decided(world, "s", ("b",))[0] == [("sonos_s", ("sonos_b",), ())]


def test_a_member_of_another_make_groups_over_a_shared_protocol() -> None:
    """A member without a native way joins the protocol both have, by its protocol id."""
    world = _World()
    _mixed(world)
    assert _decided(world, "s", ("w",))[0] == [("airplay_s", ("airplay_w",), ())]


@pytest.mark.parametrize(("order", "native"), [(("w", "b"), ()), (("b", "w"), ("sonos_b",))])
def test_an_idle_mixed_batch_depends_on_its_order(
    order: tuple[str, str], native: tuple[str, ...]
) -> None:
    """
    On an idle leader the order of a mixed request decides who groups natively.

    w only has AirPlay: asked first, it takes the group to AirPlay and b follows there;
    asked second, b has joined natively already.
    This is a known shortcoming, kept as it was; this test changes when it is fixed.
    """
    world = _World()
    _mixed(world)
    calls, _ = _decided(world, "s", order)
    assert _members(calls, "sonos_s") == ([(native, ())] if native else [])


@pytest.mark.parametrize("order", [("w", "b"), ("b", "w")])
def test_a_live_native_session_keeps_a_mixed_batch_on_one_protocol(
    order: tuple[str, str],
) -> None:
    """With a live native session, members that need a protocol are taken first."""
    world = _World()
    _mixed(world, leader_state=PLAYING, active_output="native")
    calls, _ = _decided(world, "s", order)
    assert _members(calls, "sonos_s") == []
    assert _members(calls, "airplay_s") == [(("airplay_w", "airplay_b"), ())]


def test_a_member_that_prefers_native_grouping_groups_natively() -> None:
    """A member whose own protocol prefers native grouping skips its preferred output."""
    world = _World()
    _mixed(world)
    world.protocols["sonos_b"] = replace(world.protocols["sonos_b"], prefers_native=True)
    world.players["b"] = replace(world.players["b"], preferred_output="airplay_b")
    assert _members(_decided(world, "s", ("b",))[0], "sonos_s") == [(("sonos_b",), ())]


def test_a_members_preferred_output_carries_it() -> None:
    """The member's preferred output wins over native grouping."""
    world = _World()
    _mixed(world)
    world.players["b"] = replace(world.players["b"], preferred_output="airplay_b")
    assert _decided(world, "s", ("b",))[0] == [("airplay_s", ("airplay_b",), ())]


def test_a_group_on_a_protocol_takes_members_on_it() -> None:
    """The group's protocol carries a member that has it; without SET_MEMBERS it is passed over."""
    world = _World()
    _mixed(world, active_output="airplay_s")
    assert _decided(world, "s", ("b",))[0] == [("airplay_s", ("airplay_b",), ())]
    world.protocols["airplay_s"] = replace(world.protocols["airplay_s"], features=PLAYS)
    assert _members(_decided(world, "s", ("b",))[0], "sonos_s") == [(("sonos_b",), ())]


def test_a_member_without_any_way_is_dropped_with_a_warning() -> None:
    """
    A member with no way into the group is left out with a warning.

    The change finds that out only after it prepared the member (released it from another
    leader, powered it on).
    This is a known shortcoming, kept as it was; this test changes when it is fixed.
    """
    world = _World()
    _mixed(world)
    world.protocol("dlna_x", "x", "dlna", features=PLAYS)
    world.player("x", "dlna_x")
    world.players["s"] = replace(world.players["s"], can_group_with=frozenset({"x"}))
    plan = _plan(world, ChangeKind.MEMBERS, "s", ("x",))
    assert PrepareForPlayback(player_id="x") in plan.steps
    calls, notes = _decided(world, "s", ("x",))
    assert calls == []
    assert any(
        "no compatible grouping method found" in note for note in _notes(notes, logging.WARNING)
    )


def test_a_later_switch_drops_an_earlier_member_silently() -> None:
    """
    A member that moves the group to another protocol drops an earlier one without a log line.

    x would join over AirPlay; y needs Sendspin and takes the group there, leaving x out.
    This is a known shortcoming, kept as it was; this test changes when it is fixed.
    """
    world = _World()
    world.protocol("sonos_p", "p", "sonos", role=ProtocolRole.NATIVE)
    world.protocol("airplay_p", "p", "airplay")
    world.protocol("sendspin_p", "p", "sendspin")
    world.player("p", "sonos_p", "airplay_p", "sendspin_p", can_group_with=frozenset({"x", "y"}))
    world.protocol("airplay_x", "x", "airplay")
    world.player("x", "airplay_x")
    world.protocol("sendspin_y", "y", "sendspin")
    world.player("y", "sendspin_y")
    calls, notes = _decided(world, "p", ("x", "y"))
    assert calls == [("sendspin_p", ("sendspin_y",), ())]
    assert _notes(notes, logging.WARNING) == []


def test_native_members_follow_a_group_that_left_its_own_stream() -> None:
    """On an own-stream leader, native members move to the group's protocol or drop."""
    world = _World()
    world.protocol(
        "airplay_l", "l", "airplay", role=ProtocolRole.NATIVE, own_stream=True, can=("airplay_m",)
    )
    world.protocol("sendspin_l", "l", "sendspin", can=("sendspin_s",))
    world.player("l", "airplay_l", "sendspin_l", can_group_with=frozenset({"m", "s"}))
    world.protocol("airplay_m", "m", "airplay", role=ProtocolRole.NATIVE)
    world.player("m", "airplay_m")
    world.protocol("sendspin_s", "s", "sendspin")
    world.player("s", "sendspin_s")
    calls, notes = _decided(world, "l", ("m", "s"))
    assert calls == [("sendspin_l", ("sendspin_s",), ())]
    assert any(
        "the group plays through the sendspin protocol" in n for n in _notes(notes, logging.WARNING)
    )


def test_an_unknown_id_is_skipped_silently_in_a_translation() -> None:
    """An id that names no player is skipped while translating removals."""
    world = _World()
    _sonos(world, "a", "b")
    world.players["a"] = replace(world.players["a"], group_members=("a", "ghost"))
    plan = _plan(world, ChangeKind.MEMBERS, "a", None, ("ghost",))
    assert plan.join == Join(target_id="a", remove=("ghost",))
    assert _decided(world, "a", None, ("ghost",)) == ([], [])


# -- removals per protocol --


def test_a_member_on_the_groups_protocol_leaves_it_there() -> None:
    """It is removed from the protocol the group is on."""
    world = _World()
    _mixed(world, active_output="airplay_s")
    world.protocols["airplay_s"] = replace(
        world.protocols["airplay_s"], group_members=("airplay_s", "airplay_w")
    )
    world.players["s"] = replace(world.players["s"], group_members=("s", "w"))
    world.players["w"] = replace(world.players["w"], synced_to="s", active_output="airplay_w")
    assert _decided(world, "s", None, ("w",))[0] == [("airplay_s", (), ("airplay_w",))]


def test_a_member_whose_output_went_away_is_removed_natively() -> None:
    """
    A member whose output went away is removed natively, not from the group that holds it.

    An output that can not play never matches, so w stays in s's AirPlay group.
    This is a known shortcoming, kept as it was; this test changes when it is fixed.
    """
    world = _World()
    _mixed(world, active_output="airplay_s")
    world.protocols["airplay_s"] = replace(
        world.protocols["airplay_s"], group_members=("airplay_s", "airplay_w")
    )
    world.protocols["airplay_w"] = replace(
        world.protocols["airplay_w"], available_for_playback=False
    )
    world.players["w"] = replace(
        world.players["w"],
        outputs=tuple(replace(o, available=False) for o in world.players["w"].outputs),
    )
    world.players["s"] = replace(world.players["s"], group_members=("s", "w"))
    calls, _ = _decided(world, "s", None, ("w",))
    assert _members(calls, "airplay_s") == []
    assert _members(calls, "sonos_s") == [((), ("wiim_w",))]


def test_a_native_airplay_leader_removes_the_audio_protocol_it_holds() -> None:
    """The member's first audio protocol in the leader's native group is removed."""
    world = _World()
    world.protocol(
        "airplay_t", "t", "airplay", role=ProtocolRole.NATIVE, members=("airplay_t", "airplay_b")
    )
    world.player("t", "airplay_t", group_members=("t", "b"))
    world.protocol("sonos_b", "b", "sonos", role=ProtocolRole.NATIVE)
    world.protocol("airplay_b", "b", "airplay")
    world.player("b", "sonos_b", "airplay_b", synced_to="t")
    assert _members(_decided(world, "t", None, ("b",))[0], "airplay_t") == [((), ("airplay_b",))]


# -- the join's protocol call and the hand-over --


def _switching(world: _World, state: PlaybackState) -> None:
    """Add leader l playing over AirPlay with member x, newcomer y only on Sendspin."""
    world.protocol("sonos_l", "l", "sonos", role=ProtocolRole.NATIVE)
    world.protocol("airplay_l", "l", "airplay", members=("airplay_l", "airplay_x"))
    world.protocol("sendspin_l", "l", "sendspin")
    world.player(
        "l",
        "sonos_l",
        "airplay_l",
        "sendspin_l",
        can_group_with=frozenset({"y"}),
        playback_state=state,
        active_output="airplay_l",
        group_members=("l", "x"),
    )
    world.protocol("airplay_x", "x", "airplay")
    world.player("x", "airplay_x", synced_to="l")
    world.protocol("sendspin_y", "y", "sendspin")
    world.player("y", "sendspin_y")


def _rules(world: _World) -> GroupRules:
    """Return the decisions on the world, their log lines dropped."""
    return GroupRules(world.snapshot(), lambda _note: None)


def test_a_switch_that_would_silence_a_member_is_refused() -> None:
    """Member x can not follow to Sendspin while the leader plays, so adding y is refused."""
    world = _World()
    _switching(world, PLAYING)
    rules = _rules(world)
    leader = world.players["l"]
    refusal = rules.protocol_leg(leader, rules.translate(leader, ("y",), ()))
    assert isinstance(refusal, Refuse)
    assert refusal.error is PlayerCommandFailed
    assert refusal.translation_key == "member_cannot_follow_protocol"
    assert refusal.message == (
        "Cannot add Player y to Player l: Player x cannot play over Sendspin provider"
    )
    # idle, nobody is silenced: the join goes on
    world.players["l"] = replace(leader, playback_state=PlaybackState.IDLE)
    rules = _rules(world)
    leader = world.players["l"]
    assert rules.protocol_leg(leader, rules.translate(leader, ("y",), ())) == ProtocolLeg(
        protocol_id="sendspin_l", add=("sendspin_y",), remove=()
    )


@pytest.mark.parametrize(
    ("state", "previous", "moves", "handing_over"),
    [
        (PlaybackState.IDLE, "airplay_l", True, False),
        (PLAYING, "airplay_l", True, True),
        (PlaybackState.PAUSED, "airplay_l", True, True),
        (PLAYING, "sendspin_l", True, False),
    ],
)
def test_the_leaders_output_follows_and_a_rendering_one_is_handed_over(
    state: PlaybackState, previous: str, moves: bool, handing_over: bool
) -> None:
    """
    The leader's output moves to the protocol; one that renders elsewhere is handed over.

    A paused leader still holds its output, so it is handed over too, without a resume.
    """
    world = _World()
    _switching(world, state)
    world.players["l"] = replace(world.players["l"], active_output=previous)
    handover = _rules(world).handover(world.players["l"], "sendspin_l")
    assert (handover.moves, handover.handing_over) == (moves, handing_over)
    assert handover.was_playing == (state == PLAYING)
    assert handover.previous_output == previous


@pytest.mark.parametrize("previous", [None, "native"])
def test_a_group_on_the_leaders_own_protocol_moves_no_output(previous: str | None) -> None:
    """The leader rendering natively, the group on its own protocol: its output stays."""
    world = _World()
    _sonos(world, "a", "b")
    world.players["a"] = replace(world.players["a"], active_output=previous, playback_state=PLAYING)
    handover = _rules(world).handover(world.players["a"], "sonos_a")
    assert not handover.moves
    assert not handover.handing_over


def test_the_forward_filters_and_checks_the_protocol() -> None:
    """Other domains and unknown ids drop; a protocol without SET_MEMBERS notes it."""
    world = _World()
    _mixed(world, active_output="airplay_s")
    world.protocols["airplay_s"] = replace(
        world.protocols["airplay_s"], group_members=("airplay_s", "airplay_w")
    )
    world.players["s"] = replace(world.players["s"], group_members=("s", "w"))
    lists = JoinLists(protocol_id="airplay_s", protocol_remove=("airplay_w", "sonos_b", "ghost"))
    assert _rules(world).protocol_leg(world.players["s"], lists) == ProtocolLeg(
        protocol_id="airplay_s", add=(), remove=("airplay_w",)
    )
    world.protocols["airplay_s"] = replace(world.protocols["airplay_s"], features=PLAYS)
    notes: list[GroupStep] = []
    assert (
        GroupRules(world.snapshot(), notes.append).protocol_leg(world.players["s"], lists) is None
    )
    assert any("does not support SET_MEMBERS" in note for note in _notes(notes, logging.ERROR))


def test_a_joining_member_is_pointed_at_the_protocol_unless_it_already_is() -> None:
    """Its player's output moves to the joining protocol, unless it is already that one."""
    world = _World()
    _mixed(world)
    rules = _rules(world)
    assert rules.member_output("airplay_w") == "w"
    assert rules.active_output(world.players["w"]) == "airplay_w"
    world.players["w"] = replace(world.players["w"], active_output="airplay_w")
    assert _rules(world).member_output("airplay_w") is None
    # an own protocol, or an id that names no protocol, has no output to move
    assert _rules(world).member_output("sonos_b") is None
    assert _rules(world).member_output("ghost") is None


# -- ungroup, detach and the triggers --


def test_a_group_player_or_its_static_member_ends_the_session() -> None:
    """Ungrouping a group player, or its static member, ends the group's session."""
    world = _World()
    world.protocol("group_g", "g", "sync_group", role=ProtocolRole.VIRTUAL)
    world.player("g", "group_g", type=PlayerType.GROUP, static_group_members=("a",))
    _sonos(world, "a", "b", active_group="g")
    assert _calls(_plan(world, ChangeKind.UNGROUP, "g")) == [ReleaseSession(group_id="g")]
    assert _calls(_plan(world, ChangeKind.UNGROUP, "a")) == [ReleaseSession(group_id="g")]
    assert _calls(_plan(world, ChangeKind.UNGROUP, "b")) == [
        GroupCommand(target_id="g", remove=("b",))
    ]


def test_a_synced_member_or_a_leader_leaves() -> None:
    """Through its reachable leader, on its own side, or the leader removes itself."""
    world = _World()
    _sonos(world, "a", "b", "c")
    world.players["b"] = replace(world.players["b"], synced_to="a")
    world.players["c"] = replace(world.players["c"], synced_to="gone")
    world.players["a"] = replace(world.players["a"], group_members=("a", "b"))
    assert _calls(_plan(world, ChangeKind.UNGROUP, "b")) == [
        GroupCommand(target_id="a", remove=("b",))
    ]
    assert _calls(_plan(world, ChangeKind.UNGROUP, "c")) == [Detach(member_id="c")]
    assert _calls(_plan(world, ChangeKind.UNGROUP, "a")) == [
        GroupCommand(target_id="a", remove=("a",))
    ]


def test_the_ungroup_scan_takes_the_first_initialised_leader_listing_it() -> None:
    """
    Ungrouping a free player asks the first leader that still lists it to let it go.

    The leaders are scanned in registry order, only those ``iter_players`` yields. This is
    on purpose: Home Assistant has a single unjoin command, which has to reach a dynamic
    group that still lists the player.
    """
    world = _World()
    _sonos(world, "x", "y", "free")
    world.players["x"] = replace(world.players["x"], group_members=("x", "free"), iterated=False)
    world.players["y"] = replace(world.players["y"], group_members=("y", "free"))
    assert _calls(_plan(world, ChangeKind.UNGROUP, "free")) == [
        GroupCommand(target_id="y", remove=("free",))
    ]


def test_a_member_leaves_on_its_own_side_when_its_leader_is_away() -> None:
    """Through the leader, else the protocol that reports the sync, else refused."""
    world = _World()
    _sonos(world, "a", "b")
    world.players["b"] = replace(world.players["b"], synced_to="a")
    assert _calls(_plan(world, ChangeKind.DETACH, "b")) == [
        MemberChange(target_id="a", remove=("b",))
    ]
    world.players["a"] = replace(world.players["a"], available_for_playback=False)
    world.protocols["sonos_b"] = replace(world.protocols["sonos_b"], synced_to="sonos_a")
    step = _calls(_plan(world, ChangeKind.DETACH, "b"))[0]
    assert isinstance(step, Ungroup)
    assert step.protocol_id == "sonos_b"
    no_protocol = GroupingSnapshot(
        players={**world.players, "b": replace(world.players["b"], own_protocol=None)},
        protocols=world.protocols,
    )
    refusal = _plan(no_protocol, ChangeKind.DETACH, "b").steps[-1]
    assert isinstance(refusal, Refuse)
    assert refusal.error is PlayerUnavailableError


def test_a_player_that_went_away_leaves_its_group_or_nothing_happens() -> None:
    """Removed from its group or leader; gone, free, or with a gone parent: nothing."""
    world = _World()
    _sonos(world, "a", "b", "c")
    world.players["b"] = replace(world.players["b"], synced_to="a")
    world.players["c"] = replace(world.players["c"], active_group="gone")
    step = _calls(_plan(world, ChangeKind.LEAVE, "b"))[0]
    assert isinstance(step, MemberChange)
    assert (step.target_id, step.remove) == ("a", ("b",))
    assert _calls(_plan(world, ChangeKind.LEAVE, "a")) == []
    assert _calls(_plan(world, ChangeKind.LEAVE, "c")) == []
    assert _calls(_plan(world, ChangeKind.LEAVE, "nobody")) == []


def test_a_removed_player_leaves_through_its_group_or_its_leader() -> None:
    """Straight to the group's own protocol, else a detach from its leader."""
    world = _World()
    world.protocol("group_g", "g", "sync_group", role=ProtocolRole.VIRTUAL)
    world.player("g", "group_g", type=PlayerType.GROUP)
    _sonos(world, "a", "b")
    world.players["a"] = replace(world.players["a"], active_group="g")
    world.players["b"] = replace(world.players["b"], synced_to="a")
    assert _calls(_plan(world, ChangeKind.LEAVE_FOR_REMOVAL, "a")) == [
        SetMembers(protocol_id="group_g", remove=("a",), lists=MemberLists.REMOVE_ONLY)
    ]
    assert _calls(_plan(world, ChangeKind.LEAVE_FOR_REMOVAL, "b")) == [Detach(member_id="b")]


def test_a_protocol_taken_over_by_an_external_source_leaves_its_group() -> None:
    """The protocol leaves its group from its own side."""
    world = _World()
    _sonos(world, "a")
    assert _calls(_plan(world, ChangeKind.LEAVE_PROTOCOL, "sonos_a")) == [
        Ungroup(protocol_id="sonos_a")
    ]


# -- regroup at a fresh start --


def _left_on_bridge(world: _World, preferred: str | None = None) -> None:
    """Add Sonos leader a and Sonos b, left grouped on AirPlay although they group natively."""
    world.protocol("sonos_a", "a", "sonos", role=ProtocolRole.NATIVE, can=("sonos_b",))
    world.protocol("airplay_a", "a", "airplay", members=("airplay_a", "airplay_b"))
    world.player("a", "sonos_a", "airplay_a", preferred_output=preferred, active_output="airplay_a")
    world.protocol("sonos_b", "b", "sonos", role=ProtocolRole.NATIVE)
    world.protocol("airplay_b", "b", "airplay")
    world.player("b", "sonos_b", "airplay_b", active_output="airplay_b")


def test_a_group_left_on_a_protocol_regroups_natively_at_a_fresh_start() -> None:
    """The members leave the protocol and join the leader afresh."""
    world = _World()
    _left_on_bridge(world)
    plan = _plan(world, ChangeKind.REGROUP_FOR_START, "a")
    assert _notes(plan) == [
        "Regrouping Player a off the airplay protocol, which none of its members need anymore"
    ]
    assert plan.regroup == Regroup(
        leader_id="a", protocol_id="airplay_a", removed=("airplay_b",), member_ids=("b",)
    )
    assert plan.progress is None


@pytest.mark.parametrize("why", ["preferred", "not_playable", "synced"])
def test_a_group_that_still_needs_its_protocol_stays(why: str) -> None:
    """A preferred bridge, a member that can not play, or a synced protocol leave the group alone."""
    world = _World()
    _left_on_bridge(world, preferred="airplay_a" if why == "preferred" else None)
    if why == "not_playable":
        # away, or still to be set up
        world.players["b"] = replace(world.players["b"], available_for_playback=False)
    if why == "synced":
        world.protocols["airplay_a"] = replace(world.protocols["airplay_a"], synced_to="airplay_z")
    plan = _plan(world, ChangeKind.REGROUP_FOR_START, "a")
    assert plan.steps == ()
    assert plan.regroup is None


def test_the_common_output_can_be_the_leaders_own_protocol() -> None:
    """
    A member native grouping rules out can still join the leader's own protocol.

    b is of the same make in another household, so it is no native peer of a; the search
    for a common output then matches a's native output by its domain. This may be a
    shortcoming, though no provider is known to reach it; it is kept as it was, and this
    test changes when it is fixed.
    """
    world = _World()
    world.protocol("sonos_a", "a", "sonos", role=ProtocolRole.NATIVE)
    world.player("a", "sonos_a", can_group_with=frozenset({"b"}))
    # a speaker of another household of the same make: no native peer of the leader
    world.protocols["sonos_b"] = replace(
        world.protocol("sonos_b", "b", "sonos", role=ProtocolRole.NATIVE),
        provider_id="sonos_other_household",
    )
    world.player("b", "sonos_b")
    assert _decided(world, "a", ("b",))[0] == [("sonos_a", ("sonos_b",), ())]


def test_the_old_members_leave_out_the_protocol_unknown_ids_and_the_leader() -> None:
    """The old members skip the protocol itself, unknown ids and the leader."""
    world = _World()
    _switching(world, PLAYING)
    world.protocols["airplay_l"] = replace(
        world.protocols["airplay_l"],
        group_members=("airplay_l", "ghost", "sendspin_l", "airplay_x"),
    )
    assert _rules(world).old_protocol(world.players["l"], "airplay_l", "sendspin_l") == (
        OldProtocol(protocol_id="airplay_l", members=(("airplay_x", "x"),))
    )


@pytest.mark.parametrize("previous", [None, "native", "sendspin_l"])
def test_there_is_no_old_protocol_without_another_audio_output(previous: str | None) -> None:
    """A leader that rendered natively, or on the protocol itself, leaves no protocol."""
    world = _World()
    _switching(world, PLAYING)
    assert _rules(world).old_protocol(world.players["l"], previous, "sendspin_l") is None


def test_the_native_add_keeps_only_what_the_own_protocol_can_group() -> None:
    """A native member the own protocol does not offer is left out of its call."""
    world = _World()
    _mixed(world)
    native = _rules(world).native_leg(world.players["s"], ("sonos_b", "wiim_w"), ())
    assert native is not None
    assert (native.protocol_id, native.add, native.remove) == ("sonos_s", ("sonos_b",), ())


def test_the_native_call_needs_an_own_protocol_that_groups() -> None:
    """Without SET_MEMBERS on the own protocol, or without an own protocol, nothing is called."""
    world = _World()
    _mixed(world)
    world.protocols["sonos_s"] = replace(world.protocols["sonos_s"], features=PLAYS)
    assert _rules(world).native_leg(world.players["s"], ("sonos_b",), ()) is None
    leaderless = replace(world.players["s"], own_protocol=None)
    assert _rules(world).native_leg(leaderless, ("sonos_b",), ()) is None


def test_a_regroup_that_can_not_place_every_member_leaves_the_group_alone() -> None:
    """A member the fresh grouping can not place keeps the group on its protocol."""
    world = _World()
    _left_on_bridge(world)
    # z rides the bridge too, but its AirPlay output went away and it has no native way
    world.protocol("wiim_z", "z", "wiim", role=ProtocolRole.NATIVE, features=PLAYS)
    world.protocol("airplay_z", "z", "airplay", available=False)
    world.player("z", "wiim_z", "airplay_z")
    world.protocols["airplay_a"] = replace(
        world.protocols["airplay_a"], group_members=("airplay_a", "airplay_b", "airplay_z")
    )
    assert _plan(world, ChangeKind.REGROUP_FOR_START, "a").regroup is None


def test_the_ungroup_scan_passes_over_a_leader_that_holds_the_player_as_static() -> None:
    """A leader that lists the player as a static member is not asked to let it go."""
    world = _World()
    _sonos(world, "x", "free")
    world.players["x"] = replace(
        world.players["x"], group_members=("x", "free"), static_group_members=("free",)
    )
    assert _calls(_plan(world, ChangeKind.UNGROUP, "free")) == []


def test_the_cleanup_of_a_player_that_went_away_only_logs_its_failure() -> None:
    """The removal from the group fails into a debug line, never an error."""
    world = _World()
    _sonos(world, "a", "b")
    world.players["b"] = replace(world.players["b"], synced_to="a")
    assert _calls(_plan(world, ChangeKind.LEAVE, "b")) == [
        MemberChange(
            target_id="a",
            remove=("b",),
            failure=_fail("debug"),
            message="Could not detach player %s from its group: %s",
            args=("Player b",),
        )
    ]


def test_a_dissolve_removes_the_members_that_are_away_too() -> None:
    """Every member but the leader is removed, also one that can not play now."""
    world = _World()
    _leader_with_members(world, None)
    world.players["c"] = replace(world.players["c"], available_for_playback=False)
    plan = _plan(world, ChangeKind.MEMBERS, "a", None, ("a",))
    assert plan.join == Join(target_id="a", remove=("b", "c"), stop_after=True)
    assert _decided(world, "a", None, ("a",))[0] == [("sonos_a", (), ("sonos_b", "sonos_c"))]


def test_only_members_that_can_play_now_take_the_group_over() -> None:
    """A member that can not play now is no heir and does not go along."""
    world = _World()
    _leader_with_members(world, QueueView(queue_id="a", state=PLAYING))
    world.players["b"] = replace(world.players["b"], available_for_playback=False)
    plan = _plan(world, ChangeKind.MEMBERS, "a", None, ("a",))
    assert plan.steps == (TransferLeadership(leader_id="a", remaining=("c",)),)


def test_an_output_of_the_leader_that_can_not_play_is_no_common_output() -> None:
    """The leader's AirPlay output is away, so a member only on AirPlay can not join."""
    world = _World()
    world.protocol("sonos_p", "p", "sonos", role=ProtocolRole.NATIVE)
    world.protocol("airplay_p", "p", "airplay", available=False)
    world.player("p", "sonos_p", "airplay_p", can_group_with=frozenset({"x"}))
    world.protocol("airplay_x", "x", "airplay")
    world.player("x", "airplay_x")
    calls, notes = _decided(world, "p", ("x",))
    assert calls == []
    assert any("no compatible grouping method found" in n for n in _notes(notes, logging.WARNING))
