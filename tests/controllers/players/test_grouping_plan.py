"""
The plan of a group change, one rule per test, on snapshots built by hand.

The row ids name the rows of the behaviour table of the player split's grouping design
(9a-design.md); rules marked as kept order dependences or kept defects say so.
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
    Hold,
    JoinPhase,
    MemberChange,
    MemberLists,
    MemberStage,
    Note,
    OpenTakeover,
    OutputView,
    PlayerView,
    PrepareForPlayback,
    ProtocolView,
    QueueView,
    Refuse,
    ReleaseSession,
    Resume,
    SetMembers,
    SetOutput,
    StopLeader,
    StopProtocol,
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
            state_name=f"State {player_id}",
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
    """Return the steps of a plan that act, without its log lines and holds."""
    return [step for step in plan.steps if not isinstance(step, (Note, Hold))]


def _device_calls(plan: GroupPlan) -> list[Any]:
    """Return the steps of a plan that act, without the admission's preparation."""
    return [step for step in _calls(plan) if not isinstance(step, PrepareForPlayback)]


def _all_plans(
    world: _World, kind: ChangeKind, target: str, add: tuple[str, ...] | None = None, **values: Any
) -> GroupPlan:
    """Plan a change to its end on a world that does not change, and return all its steps."""
    plan = _plan(world, kind, target, add, values.pop("remove", None), **values)
    steps = list(plan.steps)
    while plan.progress is not None:
        plan = _plan(world, kind, target, add, values.get("remove"), progress=plan.progress)
        steps += plan.steps
    return GroupPlan(tuple(steps))


def _notes(plan: GroupPlan, level: int = logging.INFO) -> list[str]:
    """Return the log lines of a plan at the level or above."""
    return [
        step.message % step.args
        for step in plan.steps
        if isinstance(step, Note) and step.level >= level
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


def _members(plan: GroupPlan, protocol_id: str) -> list[tuple[tuple[str, ...], tuple[str, ...]]]:
    """Return the member lists of the set_members steps on a protocol."""
    return [
        (step.add, step.remove)
        for step in plan.steps
        if isinstance(step, SetMembers) and step.protocol_id == protocol_id
    ]


# -- E: routing --


def test_e3_a_target_without_grouping_is_refused() -> None:
    """E3: a target without SET_MEMBERS in its state refuses the change."""
    world = _World()
    _sonos(world, "a", "b", features=frozenset())
    plan = _plan(world, ChangeKind.ROUTE, "a", ("b",))
    assert isinstance(plan.steps[-1], Refuse)
    assert plan.steps[-1].error is UnsupportedFeaturedException


def test_e4_a_member_change_on_a_held_target_goes_to_its_group() -> None:
    """E4: a target held by a group player with SET_MEMBERS passes the change on to it."""
    world = _World()
    _sonos(world, "a", "b", active_group="g", playback_owner="g")
    world.protocol("group_g", "g", "sync_group", role=ProtocolRole.VIRTUAL)
    world.player("g", "group_g", type=PlayerType.GROUP)
    plan = _plan(world, ChangeKind.ROUTE, "a", ("b",), None)
    assert plan.passed_on
    assert _calls(plan) == [GroupCommand(target_id="g", add=("b",), remove=None)]
    assert set_members_group(world.snapshot(), "a") == "g"


def test_e5_a_target_held_by_a_group_without_grouping_keeps_the_change() -> None:
    """E5: a group player without SET_MEMBERS is not passed the change."""
    world = _World()
    _sonos(world, "a", "b", active_group="g", playback_owner="g")
    world.protocol("group_g", "g", "sync_group", role=ProtocolRole.VIRTUAL, features=PLAYS)
    world.player("g", "group_g", type=PlayerType.GROUP, features=frozenset())
    plan = _plan(world, ChangeKind.ROUTE, "a", ("b",))
    assert not plan.passed_on
    assert _calls(plan) == []


def test_e6_a_synced_target_is_released_first() -> None:
    """E6: a synced target leaves its leader first, waited for, a failure only warned."""
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


def test_e14_a_legacy_id_names_its_player_unless_a_player_has_that_id() -> None:
    """E14: an alias resolves to its player; a live player's own id wins over it."""
    world = _World()
    _sonos(world, "a", "b")
    world.aliases["old_b"] = "b"
    snapshot = world.snapshot()
    assert snapshot.player("old_b") is snapshot.players["b"]
    assert snapshot.player("a") is snapshot.players["a"]
    assert snapshot.player_of("sonos_b") is snapshot.players["b"]
    assert snapshot.player("unknown") is None


# -- L: the leader removes itself --


def _leader_with_members(world: _World, queue: QueueView | None, **member_values: Any) -> None:
    """Add leader a holding b and c natively."""
    world.protocol(
        "sonos_a", "a", "sonos", role=ProtocolRole.NATIVE, members=("sonos_a", "sonos_b", "sonos_c")
    )
    world.player("a", "sonos_a", group_members=("a", "b", "c"), active_queue=queue)
    for name in ("b", "c"):
        world.protocol(f"sonos_{name}", name, "sonos", role=ProtocolRole.NATIVE)
        world.player(name, f"sonos_{name}", synced_to="a", **member_values)


def test_l3_a_leader_playing_its_own_queue_hands_the_group_over() -> None:
    """L1-L3: the remaining members that can play take the group over, nothing else happens."""
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
def test_l6_a_leader_without_a_playing_queue_of_its_own_dissolves(queue: QueueView | None) -> None:
    """L6, L7: the group is dissolved natively and the leader stopped afterwards."""
    world = _World()
    _leader_with_members(world, queue)
    plan = _plan(world, ChangeKind.MEMBERS, "a", ("x",), ("a",))
    assert _calls(plan) == [
        SetMembers(protocol_id="sonos_a", remove=("sonos_b", "sonos_c")),
        StopLeader(player_id="a"),
    ]
    assert "Dissolving sync group of player Player a as it is being removed from itself" in _notes(
        plan
    )


def test_l2_a_group_of_displays_only_has_no_heir() -> None:
    """L2: members that can not play audio never take the group over."""
    world = _World()
    _leader_with_members(world, QueueView(queue_id="a", state=PLAYING), type=PlayerType.VISUALIZER)
    plan = _plan(world, ChangeKind.MEMBERS, "a", None, ("a",))
    assert not any(isinstance(step, TransferLeadership) for step in plan.steps)


def test_l8_a_group_player_removing_itself_runs_the_leader_path() -> None:
    """L8, D5 (kept): a group player removing itself loses its members, without a stop."""
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
def test_l4_the_heir_can_be_reached_on_the_groups_protocol(
    domain_of_b: str, domain_of_c: str, expected: str
) -> None:
    """L4, O2: the first member on the domain the group plays on, else the first one."""
    world = _World()
    world.protocol("airplay_a", "a", "airplay")
    world.player("a", "airplay_a", active_output="airplay_a")
    world.protocol("x_b", "b", domain_of_b)
    world.player("b", "x_b")
    world.protocol("x_c", "c", domain_of_c)
    world.player("c", "x_c")
    assert elect_ad_hoc_leader(world.snapshot(), "a", ["b", "c"]) == expected


def test_l4_a_player_that_can_not_play_audio_never_leads() -> None:
    """L4: a visualizer is passed over even when it comes first."""
    world = _World()
    world.protocol("airplay_a", "a", "airplay")
    world.player("a", "airplay_a")
    world.protocol("airplay_v", "v", "airplay")
    world.player("v", "airplay_v", type=PlayerType.VISUALIZER)
    world.protocol("airplay_b", "b", "airplay")
    world.player("b", "airplay_b")
    assert elect_ad_hoc_leader(world.snapshot(), "a", ["v", "b"]) == "b"


# -- M: admission --


def test_m1_m2_the_target_and_a_repeated_member_are_skipped_silently() -> None:
    """M1, M2: the target itself and a member asked twice join once, without a log line."""
    world = _World()
    _sonos(world, "a", "b")
    plan = _plan(world, ChangeKind.MEMBERS, "a", ("a", "b", "b"))
    assert _members(plan, "sonos_a") == [(("sonos_b",), ())]
    assert [step for step in plan.steps if isinstance(step, PrepareForPlayback)] == [
        PrepareForPlayback(player_id="b")
    ]
    assert _notes(plan, logging.WARNING) == []


def test_m3_an_unknown_or_unavailable_member_is_left_out() -> None:
    """M3: a member that is unknown or can not play now is left out with a warning."""
    world = _World()
    _sonos(world, "a", "b")
    world.players["b"] = replace(world.players["b"], available_for_playback=False)
    plan = _plan(world, ChangeKind.MEMBERS, "a", ("nobody", "b"))
    assert _notes(plan, logging.WARNING) == [
        "Player nobody is not available",
        "Player b is not available",
    ]
    assert _members(plan, "sonos_a") == []


def test_m4_a_member_the_target_can_not_group_with_is_left_out() -> None:
    """M4: a member missing from the target's can_group_with is left out with a warning."""
    world = _World()
    _sonos(world, "a", "b")
    world.players["a"] = replace(world.players["a"], can_group_with=frozenset())
    plan = _plan(world, ChangeKind.MEMBERS, "a", ("b",))
    assert _notes(plan, logging.WARNING) == ["Player Player b can not be grouped with Player a"]
    assert _calls(plan) == []


@pytest.mark.parametrize("held_as", ["synced_to", "active_group"])
def test_m5_m6_a_member_already_in_the_group_is_skipped(held_as: str) -> None:
    """M5, M6: a listed member synced to the target, or held by it, joins nothing again."""
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


def test_m7_a_member_synced_elsewhere_is_released_and_the_plan_stops() -> None:
    """M7, RP1: the release ends the plan; the next one goes on with that member."""
    world = _World()
    _sonos(world, "a", "b", "c")
    world.players["b"] = replace(world.players["b"], synced_to="c")
    plan = _plan(world, ChangeKind.MEMBERS, "a", ("b",))
    assert isinstance(_calls(plan)[-1], Detach)
    assert plan.progress == GroupProgress(next_add=0, stage=MemberStage.RELEASED)
    # the release worked: the next plan, on the new state, admits the member without checks
    world.players["b"] = replace(world.players["b"], synced_to=None)
    next_plan = _plan(world, ChangeKind.MEMBERS, "a", ("b",), progress=plan.progress)
    assert _members(next_plan, "sonos_a") == [(("sonos_b",), ())]
    assert next_plan.progress is None


def test_m8_a_member_held_by_a_group_without_sync_is_not_released() -> None:
    """M8, D3 (kept): a member a group holds without being synced joins without a release."""
    world = _World()
    _sonos(world, "a", "b", active_group="g")
    world.players["a"] = replace(world.players["a"], active_group=None)
    plan = _plan(world, ChangeKind.MEMBERS, "a", ("b",))
    assert not any(isinstance(step, Detach) for step in plan.steps)
    assert _members(plan, "sonos_a") == [(("sonos_b",), ())]


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


def test_m9_a_member_whose_group_rides_its_own_stream_dissolves_it_first() -> None:
    """M9, RP2: its members are removed, the plan ends; a failure leaves the member out."""
    world = _World()
    _airplay_leaders(world)
    plan = _plan(world, ChangeKind.MEMBERS, "a", ("b",))
    assert _calls(plan)[-1] == MemberChange(
        target_id="b",
        remove=("c",),
        failure=_fail("leave_out"),
        message="Could not dissolve the group of %s, leaving it out of %s",
        args=("Player b", "State a"),
    )
    assert plan.progress == GroupProgress(next_add=0, stage=MemberStage.DISSOLVED)
    assert plan.progress_left_out == GroupProgress(next_add=1)


def test_m10_a_member_still_serving_its_own_group_is_left_out() -> None:
    """M10: a member that plays for its own group is left out with a warning."""
    world = _World()
    _airplay_leaders(world, state=PLAYING)
    plan = _plan(world, ChangeKind.MEMBERS, "a", ("b",))
    assert _notes(plan, logging.WARNING) == [
        "Player Player b is still serving its own group, leaving it out of State a"
    ]
    assert _calls(plan) == []


def test_m11_m14_a_member_whose_group_survives_joins_as_it_is() -> None:
    """M11, M14: no own-stream rule, or no followers: nothing to dissolve."""
    world = _World()
    _sonos(world, "a", "b", "c")
    world.players["b"] = replace(world.players["b"], group_members=("b", "c"))
    plan = _plan(world, ChangeKind.MEMBERS, "a", ("b",))
    assert not any(isinstance(step, MemberChange) for step in plan.steps)
    assert _members(plan, "sonos_a") == [(("sonos_b",), ())]


def test_m12_a_member_that_powers_on_ends_the_plan() -> None:
    """M12, RP3, D11 (kept): powering a member on ends the plan, auto play is the handler's."""
    world = _World()
    _sonos(world, "a", "b", "c")
    world.players["b"] = replace(world.players["b"], powered=False, power_control="fake")
    plan = _plan(world, ChangeKind.MEMBERS, "a", ("b", "c"))
    assert _calls(plan) == [PrepareForPlayback(player_id="b")]
    assert plan.progress == GroupProgress(next_add=1, admitted=("b",))
    next_plan = _plan(world, ChangeKind.MEMBERS, "a", ("b", "c"), progress=plan.progress)
    assert _members(next_plan, "sonos_a") == [(("sonos_b", "sonos_c"), ())]


def test_m13_o9_a_later_member_is_checked_on_the_state_after_the_earlier_release() -> None:
    """M13, O9 (kept): the playing leader j is admitted once its member i left it."""
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
    assert _members(second, "sonos_a") == [(("sonos_i", "sonos_j"), ())]


# -- R: removals --


def test_r1_r3_only_members_of_the_group_are_removed() -> None:
    """R1-R3: a listed member, or one that reports being synced to the target, is removed."""
    world = _World()
    _sonos(world, "a", "b", "c", "d")
    world.players["a"] = replace(world.players["a"], group_members=("a", "b"))
    world.players["c"] = replace(world.players["c"], synced_to="a")
    plan = _plan(world, ChangeKind.MEMBERS, "a", None, ("b", "c", "d"))
    assert _members(plan, "sonos_a") == [((), ("sonos_b", "sonos_c"))]


# -- G: a group player as target --


def test_g1_a_group_player_takes_its_member_lists_as_they_are() -> None:
    """G1: its own protocol is called with both lists, empty ones included."""
    world = _World()
    world.protocol("group_g", "g", "sync_group", role=ProtocolRole.VIRTUAL)
    world.player("g", "group_g", type=PlayerType.GROUP, can_group_with=frozenset({"a"}))
    _sonos(world, "a")
    plan = _plan(world, ChangeKind.MEMBERS, "g", ("a",), ("nobody",))
    assert _calls(plan)[-1] == SetMembers(
        protocol_id="group_g", add=("a",), remove=(), lists=MemberLists.AS_THEY_ARE
    )


def test_g5_a_group_without_grouping_reached_internally_calls_nothing() -> None:
    """G5: without SET_MEMBERS the removal goes through the protocol path, which ends empty."""
    world = _World()
    world.protocol("group_g", "g", "sync_group", role=ProtocolRole.VIRTUAL, features=PLAYS)
    world.player("g", "group_g", type=PlayerType.GROUP, features=frozenset(), group_members=("a",))
    _sonos(world, "a")
    plan = _plan(world, ChangeKind.MEMBERS, "g", None, ("a",))
    assert _calls(plan) == []


# -- T: which protocol carries a member --


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


def test_t8_a_native_peer_groups_on_the_own_protocol() -> None:
    """T8: a member of the same provider groups natively, by its own protocol id."""
    world = _World()
    _mixed(world)
    plan = _plan(world, ChangeKind.MEMBERS, "s", ("b",))
    assert _members(plan, "sonos_s") == [(("sonos_b",), ())]


def test_t9_a_member_of_another_make_groups_over_a_shared_protocol() -> None:
    """T9, F6, F7: over AirPlay, its output activated first, then the join, then the leader."""
    world = _World()
    _mixed(world)
    plan = _plan(world, ChangeKind.MEMBERS, "s", ("w",))
    assert _device_calls(plan) == [
        SetOutput(player_id="w", output_id="airplay_w"),
        SetMembers(protocol_id="airplay_s", add=("airplay_w",)),
    ]
    assert plan.progress is not None
    assert plan.progress.join is not None
    assert plan.progress.join.phase == JoinPhase.JOINED


@pytest.mark.parametrize(("order", "native"), [(("w", "b"), ()), (("b", "w"), ("sonos_b",))])
def test_t13_o1_an_idle_mixed_batch_depends_on_its_order(
    order: tuple[str, str], native: tuple[str, ...]
) -> None:
    """T13, O1 (kept): on an idle leader the request order decides who groups natively."""
    world = _World()
    _mixed(world)
    plan = _all_plans(world, ChangeKind.MEMBERS, "s", order)
    assert _members(plan, "sonos_s") == ([(native, ())] if native else [])


@pytest.mark.parametrize("order", [("w", "b"), ("b", "w")])
def test_t2_a_live_native_session_keeps_a_mixed_batch_on_one_protocol(
    order: tuple[str, str],
) -> None:
    """T2, T3: with a live native session, members that need a protocol are taken first."""
    world = _World()
    _mixed(world, leader_state=PLAYING, active_output="native")
    plan = _all_plans(world, ChangeKind.MEMBERS, "s", order)
    assert _members(plan, "sonos_s") == []
    assert _members(plan, "airplay_s") == [(("airplay_w", "airplay_b"), ())]


def test_t4_a_member_that_prefers_native_grouping_groups_natively() -> None:
    """T4: a member whose own protocol prefers native grouping skips its preferred output."""
    world = _World()
    _mixed(world)
    world.protocols["sonos_b"] = replace(world.protocols["sonos_b"], prefers_native=True)
    world.players["b"] = replace(world.players["b"], preferred_output="airplay_b")
    plan = _plan(world, ChangeKind.MEMBERS, "s", ("b",))
    assert _members(plan, "sonos_s") == [(("sonos_b",), ())]


def test_t5_a_members_preferred_output_carries_it() -> None:
    """T5: the member's preferred output wins over native grouping."""
    world = _World()
    _mixed(world)
    world.players["b"] = replace(world.players["b"], preferred_output="airplay_b")
    plan = _plan(world, ChangeKind.MEMBERS, "s", ("b",))
    assert SetMembers(protocol_id="airplay_s", add=("airplay_b",)) in plan.steps


def test_t6_t7_a_group_on_a_protocol_takes_members_on_it() -> None:
    """T6, T7: the group's protocol carries a member that has it; without SET_MEMBERS it drops."""
    world = _World()
    _mixed(world, active_output="airplay_s")
    plan = _plan(world, ChangeKind.MEMBERS, "s", ("b",))
    assert SetMembers(protocol_id="airplay_s", add=("airplay_b",)) in plan.steps
    world.protocols["airplay_s"] = replace(world.protocols["airplay_s"], features=PLAYS)
    plan = _plan(world, ChangeKind.MEMBERS, "s", ("b",))
    assert _members(plan, "sonos_s") == [(("sonos_b",), ())]


def test_t10_a_member_without_any_way_is_dropped_with_a_warning() -> None:
    """T10, D7 (kept): no grouping method: a warning, after it was already prepared."""
    world = _World()
    _mixed(world)
    world.protocol("dlna_x", "x", "dlna", features=PLAYS)
    world.player("x", "dlna_x")
    world.players["s"] = replace(world.players["s"], can_group_with=frozenset({"x"}))
    plan = _plan(world, ChangeKind.MEMBERS, "s", ("x",))
    assert PrepareForPlayback(player_id="x") in plan.steps
    assert any(
        "no compatible grouping method found" in note for note in _notes(plan, logging.WARNING)
    )


def test_t11_d6_a_later_switch_drops_an_earlier_member_silently() -> None:
    """T11, D6 (kept): X joins over AirPlay, Y moves the group to Sendspin, X is dropped."""
    world = _World()
    world.protocol("sonos_p", "p", "sonos", role=ProtocolRole.NATIVE)
    world.protocol("airplay_p", "p", "airplay")
    world.protocol("sendspin_p", "p", "sendspin")
    world.player("p", "sonos_p", "airplay_p", "sendspin_p", can_group_with=frozenset({"x", "y"}))
    world.protocol("airplay_x", "x", "airplay")
    world.player("x", "airplay_x")
    world.protocol("sendspin_y", "y", "sendspin")
    world.player("y", "sendspin_y")
    plan = _plan(world, ChangeKind.MEMBERS, "p", ("x", "y"))
    assert _members(plan, "sendspin_p") == [(("sendspin_y",), ())]
    assert _members(plan, "airplay_p") == []
    assert _notes(plan, logging.WARNING) == []


def test_t12_native_members_follow_a_group_that_left_its_own_stream() -> None:
    """T12: on an own-stream leader, native members move to the group's protocol or drop."""
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
    plan = _plan(world, ChangeKind.MEMBERS, "l", ("m", "s"))
    assert _members(plan, "sendspin_l") == [(("sendspin_s",), ())]
    assert _members(plan, "airplay_l") == []
    assert any(
        "the group plays through the sendspin protocol" in n for n in _notes(plan, logging.WARNING)
    )


def test_t14_an_unknown_id_is_skipped_silently_in_a_translation() -> None:
    """T14: an id that names no player is skipped while translating removals."""
    world = _World()
    _sonos(world, "a", "b")
    world.players["a"] = replace(world.players["a"], group_members=("a", "ghost"))
    plan = _plan(world, ChangeKind.MEMBERS, "a", None, ("ghost",))
    assert _calls(plan) == []


# -- X: removals per protocol --


def test_x1_a_member_on_the_groups_protocol_leaves_it_there() -> None:
    """X1, F8: removed from the protocol, its output cleared after the call."""
    world = _World()
    _mixed(world, active_output="airplay_s")
    world.protocols["airplay_s"] = replace(
        world.protocols["airplay_s"], group_members=("airplay_s", "airplay_w")
    )
    world.players["s"] = replace(world.players["s"], group_members=("s", "w"))
    world.players["w"] = replace(world.players["w"], synced_to="s", active_output="airplay_w")
    plan = _plan(world, ChangeKind.MEMBERS, "s", None, ("w",))
    assert _calls(plan) == [SetMembers(protocol_id="airplay_s", remove=("airplay_w",))]
    assert plan.progress is not None
    after = _plan(world, ChangeKind.MEMBERS, "s", None, ("w",), progress=plan.progress)
    assert _calls(after) == [SetOutput(player_id="w", output_id=None)]


def test_x4_d2_a_member_whose_output_went_away_is_removed_natively() -> None:
    """X3, X4, D2 (kept): an unavailable output never matches; the removal goes native."""
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
    plan = _plan(world, ChangeKind.MEMBERS, "s", None, ("w",))
    assert _members(plan, "airplay_s") == []
    assert _members(plan, "sonos_s") == [((), ("wiim_w",))]


def test_x2_a_native_airplay_leader_removes_the_audio_protocol_it_holds() -> None:
    """X2, O4: the member's first audio protocol in the leader's native group is removed."""
    world = _World()
    world.protocol(
        "airplay_t", "t", "airplay", role=ProtocolRole.NATIVE, members=("airplay_t", "airplay_b")
    )
    world.player("t", "airplay_t", group_members=("t", "b"))
    world.protocol("sonos_b", "b", "sonos", role=ProtocolRole.NATIVE)
    world.protocol("airplay_b", "b", "airplay")
    world.player("b", "sonos_b", "airplay_b", synced_to="t")
    plan = _plan(world, ChangeKind.MEMBERS, "t", None, ("b",))
    assert _members(plan, "airplay_t") == [((), ("airplay_b",))]


# -- F and H: the forward and the handover --


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


def test_f4_a_switch_that_would_silence_a_member_is_refused() -> None:
    """F4: x can not follow to Sendspin while the leader plays, so y is refused."""
    world = _World()
    _switching(world, PLAYING)
    world.protocols["airplay_l"] = replace(world.protocols["airplay_l"], features=GROUPS)
    plan = _plan(world, ChangeKind.MEMBERS, "l", ("y",))
    refusal = plan.steps[-1]
    assert isinstance(refusal, Refuse)
    assert refusal.error is PlayerCommandFailed
    assert refusal.translation_key == "member_cannot_follow_protocol"
    assert refusal.message == (
        "Cannot add Player y to State l: Player x cannot play over Sendspin provider"
    )


def test_h_an_idle_switch_hands_nothing_over_and_keeps_x() -> None:
    """F9, H8: idle, the switch only moves the leader's output after the join call."""
    world = _World()
    _switching(world, PlaybackState.IDLE)
    plan = _plan(world, ChangeKind.MEMBERS, "l", ("y",))
    assert SetMembers(protocol_id="sendspin_l", add=("sendspin_y",)) in plan.steps
    assert plan.progress is not None
    assert plan.progress.join is not None
    after = _plan(world, ChangeKind.MEMBERS, "l", ("y",), progress=plan.progress)
    assert _calls(after) == [SetOutput(player_id="l", output_id="sendspin_l")]


def _joined(state: PlaybackState) -> tuple[_World, GroupProgress]:
    """Return a leader on AirPlay with x, after y joined Sendspin, and the progress there."""
    world = _World()
    _switching(world, state)
    world.players["x"] = replace(world.players["x"], outputs=(*world.players["x"].outputs,))
    world.protocol("sendspin_x", "x", "sendspin")
    world.players["x"] = world.player("x", "airplay_x", "sendspin_x", synced_to="l")
    plan = _plan(world, ChangeKind.MEMBERS, "l", ("y",))
    assert plan.progress is not None
    return world, plan.progress


def test_h2_h4_h5_h6_a_playing_switch_stops_the_old_protocol_resumes_and_regroups() -> None:
    """H2-H6, RP12-RP18: the phases of a hand-over, each after the device call before it."""
    world, progress = _joined(PLAYING)
    leader = _plan(world, ChangeKind.MEMBERS, "l", ("y",), progress=progress)
    assert _calls(leader) == [SetOutput(player_id="l", output_id="sendspin_l")]
    assert (
        "Handing the output of State l over to the sendspin protocol and resuming playback"
        in _notes(leader)
    )
    assert leader.progress is not None
    assert leader.progress.join is not None
    assert leader.progress.join.was_playing
    # the leader's output moved: the old protocol is looked up now, its members frozen
    world.players["l"] = replace(world.players["l"], active_output="sendspin_l")
    stop = _plan(world, ChangeKind.MEMBERS, "l", ("y",), progress=leader.progress)
    assert _calls(stop) == [StopProtocol(protocol_id="airplay_l")]
    assert stop.progress is not None
    assert stop.progress.join is not None
    assert stop.progress.join.old_members == (("airplay_x", "x"),)
    removal = _plan(world, ChangeKind.MEMBERS, "l", ("y",), progress=stop.progress)
    assert _calls(removal) == [SetMembers(protocol_id="airplay_l", remove=("airplay_x",))]
    tail = _plan(world, ChangeKind.MEMBERS, "l", ("y",), progress=removal.progress)
    assert _calls(tail) == [Resume(player_id="l"), MemberChange(target_id="l", add=("x",))]


def test_h7_a_pause_during_the_join_call_means_no_resume() -> None:
    """H7, RP13: a leader that paused while the join call ran hands over without resuming."""
    world, progress = _joined(PLAYING)
    world.players["l"] = replace(world.players["l"], playback_state=PlaybackState.PAUSED)
    leader = _plan(world, ChangeKind.MEMBERS, "l", ("y",), progress=progress)
    assert leader.progress is not None
    assert leader.progress.join is not None
    assert leader.progress.join.handing_over
    assert not leader.progress.join.was_playing


def test_h_was_playing_is_carried_across_the_leaders_activation() -> None:
    """RP13, RP18: a pause after the leader's output moved still resumes, as today."""
    world, progress = _joined(PLAYING)
    leader = _plan(world, ChangeKind.MEMBERS, "l", ("y",), progress=progress)
    world.players["l"] = replace(
        world.players["l"], active_output="sendspin_l", playback_state=PlaybackState.PAUSED
    )
    plan = leader
    steps: list[Any] = []
    while plan.progress is not None:
        plan = _plan(world, ChangeKind.MEMBERS, "l", ("y",), progress=plan.progress)
        steps += _calls(plan)
    assert Resume(player_id="l") in steps


def test_h3_h10_stranded_members_are_released_from_the_own_protocol() -> None:
    """F5, H3, H10: own-stream leader: natives move along and are released, unless own is gone."""
    world = _World()
    world.protocol(
        "airplay_l",
        "l",
        "airplay",
        role=ProtocolRole.NATIVE,
        own_stream=True,
        members=("airplay_l", "airplay_n"),
    )
    world.protocol("sendspin_l", "l", "sendspin")
    world.player(
        "l", "airplay_l", "sendspin_l", can_group_with=frozenset({"s"}), group_members=("l", "n")
    )
    world.protocol("airplay_n", "n", "airplay", role=ProtocolRole.NATIVE)
    world.protocol("sendspin_n", "n", "sendspin")
    world.player("n", "airplay_n", "sendspin_n", synced_to="l")
    world.protocol("sendspin_s", "s", "sendspin")
    world.player("s", "sendspin_s")
    everything = _all_plans(world, ChangeKind.MEMBERS, "l", ("s",))
    assert (
        SetMembers(protocol_id="sendspin_l", add=("sendspin_s", "sendspin_n")) in everything.steps
    )
    plan = _plan(world, ChangeKind.MEMBERS, "l", ("s",))
    while plan.progress is not None and (
        plan.progress.join is None or plan.progress.join.phase != JoinPhase.JOINED
    ):
        plan = _plan(world, ChangeKind.MEMBERS, "l", ("s",), progress=plan.progress)
    joined = _plan(world, ChangeKind.MEMBERS, "l", ("s",), progress=plan.progress)
    active = _plan(world, ChangeKind.MEMBERS, "l", ("s",), progress=joined.progress)
    assert _calls(active) == [
        SetMembers(protocol_id="airplay_l", remove=("airplay_n",), lists=MemberLists.REMOVE_ONLY)
    ]
    # H10: the own protocol is gone by then: nothing is released
    gone = GroupingSnapshot(
        players={**world.players, "l": replace(world.players["l"], own_protocol=None)},
        protocols=world.protocols,
    )
    assert _calls(_plan(gone, ChangeKind.MEMBERS, "l", ("s",), progress=joined.progress)) == []


def test_f2_f3_f10_the_forward_filters_and_checks_the_protocol() -> None:
    """F1-F3, F10: other domains and unknown ids drop; a protocol without SET_MEMBERS notes it."""
    world = _World()
    _mixed(world, active_output="airplay_s")
    world.protocols["airplay_s"] = replace(
        world.protocols["airplay_s"], group_members=("airplay_s", "airplay_w")
    )
    world.players["s"] = replace(world.players["s"], group_members=("s", "w"))
    snapshot = world.snapshot()
    progress = GroupProgress(
        join=_join(
            JoinPhase.FORWARD, "airplay_s", protocol_remove=("airplay_w", "sonos_b", "ghost")
        )
    )
    plan = plan_group_change(
        snapshot, GroupChange(kind=ChangeKind.MEMBERS, target_id="s"), progress
    )
    assert _calls(plan) == [SetMembers(protocol_id="airplay_s", remove=("airplay_w",))]
    world.protocols["airplay_s"] = replace(world.protocols["airplay_s"], features=PLAYS)
    plan = plan_group_change(
        world.snapshot(), GroupChange(kind=ChangeKind.MEMBERS, target_id="s"), progress
    )
    assert _calls(plan) == []
    assert any("does not support SET_MEMBERS" in note for note in _notes(plan, logging.ERROR))


def _join(phase: JoinPhase, protocol_id: str | None, **values: Any) -> Any:
    """Return a join progress."""
    from music_assistant.controllers.players.grouping_plan import JoinProgress  # noqa: PLC0415

    return JoinProgress(phase=phase, protocol_id=protocol_id, **values)


def test_f6_rp10_each_further_activation_is_planned_after_the_one_before() -> None:
    """F6, RP10: one member's activation ends the plan when another one follows."""
    world = _World()
    _mixed(world)
    world.protocol("airplay_v", "v", "airplay")
    world.player("v", "airplay_v")
    world.players["s"] = replace(world.players["s"], can_group_with=frozenset({"w", "v"}))
    world.protocols["airplay_s"] = replace(
        world.protocols["airplay_s"], can_group_with=frozenset({"airplay_w", "airplay_v"})
    )
    plan = _plan(world, ChangeKind.MEMBERS, "s", ("w", "v"))
    assert _calls(plan)[-1] == SetOutput(player_id="w", output_id="airplay_w")
    # v got the protocol as output by itself meanwhile: no activation, straight to the join call
    world.players["v"] = replace(world.players["v"], active_output="airplay_v")
    after = _plan(world, ChangeKind.MEMBERS, "s", ("w", "v"), progress=plan.progress)
    assert _calls(after) == [SetMembers(protocol_id="airplay_s", add=("airplay_w", "airplay_v"))]


def test_n1_rp19_the_native_forward_reads_the_own_protocol_after_the_protocol_leg() -> None:
    """N1, N2, RP19: native members are grouped after the protocol leg, if the feature stays."""
    world = _World()
    _mixed(world)
    progress = GroupProgress(join=_join(JoinPhase.NATIVE, None, native_add=("sonos_b",)))
    change = GroupChange(kind=ChangeKind.MEMBERS, target_id="s")
    assert _calls(plan_group_change(world.snapshot(), change, progress)) == [
        SetMembers(protocol_id="sonos_s", add=("sonos_b",))
    ]
    world.protocols["sonos_s"] = replace(world.protocols["sonos_s"], features=PLAYS)
    assert _calls(plan_group_change(world.snapshot(), change, progress)) == []


def test_k1_k3_a_takeover_is_opened_for_new_content_over_a_protocol_only() -> None:
    """K1, K3, RP6: the hook is called before the forward and ends the plan; never natively."""
    world = _World()
    _mixed(world)
    plan = _plan(world, ChangeKind.MEMBERS, "s", ("w",), new_content=True)
    assert _device_calls(plan) == [OpenTakeover(protocol_id="airplay_s")]
    assert plan.progress is not None
    native = _plan(world, ChangeKind.MEMBERS, "s", ("b",), new_content=True)
    assert not any(isinstance(step, OpenTakeover) for step in native.steps)


# -- U, I, J: ungroup, detach and the triggers --


def test_u3_u4_a_group_player_or_its_static_member_ends_the_session() -> None:
    """U3, U4: ungrouping a group player, or its static member, ends the group's session."""
    world = _World()
    world.protocol("group_g", "g", "sync_group", role=ProtocolRole.VIRTUAL)
    world.player("g", "group_g", type=PlayerType.GROUP, static_group_members=("a",))
    _sonos(world, "a", "b", active_group="g")
    assert _calls(_plan(world, ChangeKind.UNGROUP, "g")) == [ReleaseSession(group_id="g")]
    assert _calls(_plan(world, ChangeKind.UNGROUP, "a")) == [ReleaseSession(group_id="g")]
    assert _calls(_plan(world, ChangeKind.UNGROUP, "b")) == [
        GroupCommand(target_id="g", remove=("b",))
    ]


def test_u6_u7_u8_a_synced_member_or_a_leader_leaves() -> None:
    """U6-U8: through its reachable leader, on its own side, or the leader removes itself."""
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


def test_u9_u12_o5_d9_the_scan_takes_the_first_initialised_leader_listing_it() -> None:
    """U9, U12, O5, D9 (kept on purpose): registry order, only what iter_players yields."""
    world = _World()
    _sonos(world, "x", "y", "free")
    world.players["x"] = replace(world.players["x"], group_members=("x", "free"), iterated=False)
    world.players["y"] = replace(world.players["y"], group_members=("y", "free"))
    assert _calls(_plan(world, ChangeKind.UNGROUP, "free")) == [
        GroupCommand(target_id="y", remove=("free",))
    ]


def test_i1_i2_a_member_leaves_on_its_own_side_when_its_leader_is_away() -> None:
    """I1, I2: through the leader, else the protocol that reports the sync, else refused."""
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


def test_j8_j22_a_player_that_went_away_leaves_its_group_or_nothing_happens() -> None:
    """J8, J22: removed from its group or leader; gone, free, or a gone parent: nothing."""
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


def test_j12_j13_a_removed_player_leaves_through_its_group_or_its_leader() -> None:
    """J12, J13: straight to the group's own protocol, else a detach from its leader."""
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


def test_j17_a_protocol_taken_over_by_an_external_source_leaves_its_group() -> None:
    """J17: the protocol leaves its group from its own side."""
    world = _World()
    _sonos(world, "a")
    assert _calls(_plan(world, ChangeKind.LEAVE_PROTOCOL, "sonos_a")) == [
        Ungroup(protocol_id="sonos_a")
    ]


# -- S: regroup at a fresh start --


def _left_on_bridge(world: _World, preferred: str | None = None) -> None:
    """Add Sonos leader a and Sonos b, left grouped on AirPlay although they group natively."""
    world.protocol("sonos_a", "a", "sonos", role=ProtocolRole.NATIVE, can=("sonos_b",))
    world.protocol("airplay_a", "a", "airplay", members=("airplay_a", "airplay_b"))
    world.player("a", "sonos_a", "airplay_a", preferred_output=preferred, active_output="airplay_a")
    world.protocol("sonos_b", "b", "sonos", role=ProtocolRole.NATIVE)
    world.protocol("airplay_b", "b", "airplay")
    world.player("b", "sonos_b", "airplay_b", active_output="airplay_b")


def test_s4_a_group_left_on_a_protocol_regroups_natively_at_a_fresh_start() -> None:
    """S1-S4, RP22, RP23: removed from the protocol, then joined afresh after that call."""
    world = _World()
    _left_on_bridge(world)
    plan = _plan(world, ChangeKind.REGROUP_FOR_START, "a")
    assert _calls(plan) == [SetMembers(protocol_id="airplay_a", remove=("airplay_b",))]
    assert plan.progress is not None
    assert plan.progress.regroup is not None
    after = _plan(world, ChangeKind.REGROUP_FOR_START, "a", progress=plan.progress)
    assert _calls(after) == [
        SetOutput(player_id="b", output_id=None),
        SetOutput(player_id="a", output_id=None),
        SetMembers(protocol_id="sonos_a", add=("sonos_b",)),
    ]


@pytest.mark.parametrize("why", ["preferred", "unavailable", "synced"])
def test_s1_s3_a_group_that_still_needs_its_protocol_stays(why: str) -> None:
    """S1-S3: a preferred bridge, a member away, or a synced protocol leave the group alone."""
    world = _World()
    _left_on_bridge(world, preferred="airplay_a" if why == "preferred" else None)
    if why == "unavailable":
        world.players["b"] = replace(world.players["b"], available=False)
    if why == "synced":
        world.protocols["airplay_a"] = replace(world.protocols["airplay_a"], synced_to="airplay_z")
    assert _calls(_plan(world, ChangeKind.REGROUP_FOR_START, "a")) == []


def test_t9_d10_the_common_output_can_be_the_leaders_own_protocol() -> None:
    """T9, D10 (kept): P4 matches the leader's native output for a member P3 ruled out."""
    world = _World()
    world.protocol("sonos_a", "a", "sonos", role=ProtocolRole.NATIVE)
    world.player("a", "sonos_a", can_group_with=frozenset({"b"}))
    # a speaker of another household of the same make: no native peer of the leader
    world.protocols["sonos_b"] = replace(
        world.protocol("sonos_b", "b", "sonos", role=ProtocolRole.NATIVE),
        provider_id="sonos_other_household",
    )
    world.player("b", "sonos_b")
    plan = _all_plans(world, ChangeKind.MEMBERS, "a", ("b",))
    assert _members(plan, "sonos_a") == [(("sonos_b",), ())]
