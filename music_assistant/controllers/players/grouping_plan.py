"""
Decide how a group change reaches the devices, from a snapshot of the players.

Nothing here reads anything but the snapshot it is given: no server, no config, no provider and
no clock. ``plan_group_change`` returns the steps of a change up to the next point where the
outcome of a step decides what follows; the executor runs them, takes a new snapshot and plans
again from the progress the plan handed back. The join that completes a member change asks
``GroupRules`` for each of its decisions, on the snapshot taken after its last device call.
"""

from __future__ import annotations

import contextlib
import logging
from dataclasses import dataclass, field, replace
from enum import IntEnum, StrEnum
from typing import TYPE_CHECKING, Protocol

from music_assistant_models.constants import PLAYER_CONTROL_NONE
from music_assistant_models.enums import PlaybackState, PlayerFeature, PlayerType, ProtocolRole
from music_assistant_models.errors import (
    PlayerCommandFailed,
    PlayerUnavailableError,
    UnsupportedFeaturedException,
)

from music_assistant.helpers.config_entries import PLAYBACK_TARGET_TYPES

if TYPE_CHECKING:
    from collections.abc import Callable, Container, Iterable, Mapping, Sequence

    from music_assistant_models.errors import MusicAssistantError

RENDERING = (PlaybackState.PLAYING, PlaybackState.PAUSED)


@dataclass(frozen=True, slots=True, kw_only=True)
class OutputView:
    """
    One output of a player, as the player lists its outputs.

    :param output_id: "native" or the id of an audio protocol.
    :param protocol_id: The attached protocol behind it (the own protocol for "native"), None
        when it is not attached.
    :param domain: Protocol domain of the output.
    :param available: Whether it can play now.
    :param is_native: Whether it is the own protocol of the player.
    """

    output_id: str
    protocol_id: str | None
    domain: str
    available: bool
    is_native: bool


@dataclass(frozen=True, slots=True, kw_only=True)
class ProtocolView:
    """
    A protocol, as far as grouping depends on it.

    :param protocol_id: Id of the protocol.
    :param player_id: Id of the player it is attached to; None while it waits for one.
    :param domain: Domain of its provider.
    :param provider_id: Instance id of its provider.
    :param provider_name: Manifest name of its provider, as refusals name it.
    :param role: Role it is attached in.
    :param features: What it supports.
    :param available_for_playback: Whether it is enabled, reachable and set up.
    :param group_members: The ids it reports as its group, itself first.
    :param synced_to: The id of the protocol it reports to be synced to.
    :param can_group_with: The ids it reports it can group with: protocol ids or provider
        instance ids.
    :param native_peers: The own protocols of other players it groups with natively (asked by
        their id); only given for an own protocol with SET_MEMBERS.
    :param needs_own_stream: Its native grouping only works while it renders its own stream.
    :param prefers_native: It groups natively before any audio protocol.
    """

    protocol_id: str
    player_id: str | None
    domain: str
    provider_id: str
    provider_name: str
    role: ProtocolRole
    features: frozenset[PlayerFeature]
    available_for_playback: bool
    group_members: tuple[str, ...]
    synced_to: str | None
    can_group_with: frozenset[str]
    native_peers: Container[str] = frozenset()
    needs_own_stream: bool = False
    prefers_native: bool = False


@dataclass(frozen=True, slots=True, kw_only=True)
class QueueView:
    """
    The queue a player plays.

    :param queue_id: Id of the queue.
    :param state: Its playback state.
    """

    queue_id: str
    state: PlaybackState


@dataclass(frozen=True, slots=True, kw_only=True)
class PlayerView:
    """
    A player, as far as grouping depends on it.

    :param player_id: Id of the player.
    :param name: Its name.
    :param type: Its type.
    :param listed: Whether its state says available and enabled.
    :param iterated: Whether ``iter_players(return_unavailable=False)`` yields it.
    :param available_for_playback: Whether it can play now.
    :param switched_off: Whether its device is off the network because it is switched off at
        its external power control.
    :param powered: Its power state.
    :param power_control: The control its power goes through.
    :param playback_state: Its playback state.
    :param features: The features its state lists.
    :param synced_to: The player it is synced to.
    :param active_group: The group holding it.
    :param group_members: Its group members, as its state lists them.
    :param static_group_members: The members a group of it can not let go.
    :param can_group_with: The players it can group with, as its state lists them.
    :param playback_owner: The player that owns its playback.
    :param own_protocol: Id of its own protocol.
    :param audio_protocols: Ids of its attached audio protocols, most preferred first.
    :param outputs: Its outputs, in the order the player lists them.
    :param active_output: Its active output: "native", a protocol id, or None.
    :param preferred_output: The output its config prefers.
    :param active_queue: The queue it plays, if any.
    """

    player_id: str
    name: str
    type: PlayerType
    listed: bool = True
    iterated: bool = True
    available_for_playback: bool = True
    switched_off: bool = False
    powered: bool | None = None
    power_control: str = PLAYER_CONTROL_NONE
    playback_state: PlaybackState = PlaybackState.IDLE
    features: frozenset[PlayerFeature] = frozenset()
    synced_to: str | None = None
    active_group: str | None = None
    group_members: tuple[str, ...] = ()
    static_group_members: tuple[str, ...] = ()
    can_group_with: frozenset[str] = frozenset()
    playback_owner: str = ""
    own_protocol: str | None = None
    audio_protocols: tuple[str, ...] = ()
    outputs: tuple[OutputView, ...] = ()
    active_output: str | None = None
    preferred_output: str | None = None
    active_queue: QueueView | None = None

    def output_by_domain(self, domain: str) -> OutputView | None:
        """
        Return the first output of a protocol domain.

        :param domain: The protocol domain.
        """
        return next((output for output in self.outputs if output.domain == domain), None)

    @property
    def playback_domains(self) -> set[str]:
        """Return the domains of the outputs that can play now."""
        return {output.domain for output in self.outputs if output.available}


@dataclass(frozen=True, slots=True)
class GroupingSnapshot:
    """
    Everything a group change is decided on.

    :param players: The players by id, in registry order.
    :param protocols: The attached protocols by id.
    :param aliases: The legacy ids that resolve, to the id of their player; an id of a live or
        a stored player is no alias.
    :param held: The protocols the running change holds, also when detached meanwhile.
    :param held_players: The players the running change holds, also when removed meanwhile.
    """

    players: Mapping[str, PlayerView]
    protocols: Mapping[str, ProtocolView]
    aliases: Mapping[str, str] = field(default_factory=dict)
    held: Mapping[str, ProtocolView] = field(default_factory=dict)
    held_players: Mapping[str, PlayerView] = field(default_factory=dict)

    def player(self, player_id: str) -> PlayerView | None:
        """
        Return the player an id names, a legacy id included.

        :param player_id: Id of a player, or one of its legacy ids.
        """
        return self.players.get(self.aliases.get(player_id, player_id))

    def player_of(self, protocol_or_player_id: str) -> PlayerView | None:
        """
        Return the player an id names: the one a protocol is attached to, or the player.

        :param protocol_or_player_id: Id of an attached protocol or of a player.
        """
        protocol = self.protocols.get(protocol_or_player_id)
        if protocol is not None and protocol.player_id is not None:
            return self.players.get(protocol.player_id)
        return self.player(protocol_or_player_id)

    def held_protocol(self, protocol_id: str) -> ProtocolView | None:
        """
        Return a protocol the change holds, else the attached one.

        :param protocol_id: Id of the protocol.
        """
        return self.held.get(protocol_id) or self.protocols.get(protocol_id)

    def held_player(self, player_id: str) -> PlayerView | None:
        """
        Return a player the change holds, else the live one.

        :param player_id: Id of the player.
        """
        return self.held_players.get(player_id) or self.player(player_id)

    def attached(self, player: PlayerView, protocol_id: str | None) -> ProtocolView | None:
        """
        Return a protocol attached to a player.

        :param player: The player.
        :param protocol_id: Id of the protocol.
        """
        if protocol_id is None or (protocol := self.protocols.get(protocol_id)) is None:
            return None
        return protocol if protocol.player_id == player.player_id else None

    def own(self, player: PlayerView) -> ProtocolView | None:
        """
        Return the own protocol of a player.

        :param player: The player.
        """
        return self.attached(player, player.own_protocol)

    def output_protocol(self, player: PlayerView, output_id: str) -> ProtocolView | None:
        """
        Return the attached protocol behind an output of a player.

        :param player: The player.
        :param output_id: "native" or the id of a protocol.
        """
        if output_id == "native":
            own = self.own(player)
            return own if own and PlayerFeature.PLAY_MEDIA in own.features else None
        return self.attached(player, output_id)


class ChangeKind(StrEnum):
    """What a group change is."""

    ROUTE = "route"
    MEMBERS = "members"
    UNGROUP = "ungroup"
    DETACH = "detach"
    LEAVE = "leave"
    LEAVE_FOR_REMOVAL = "leave_for_removal"
    REGROUP_FOR_START = "regroup_for_start"
    LEAVE_PROTOCOL = "leave_protocol"


@dataclass(frozen=True, slots=True, kw_only=True)
class GroupChange:
    """
    A group change as a command asks for it.

    :param kind: What the change is.
    :param target_id: The player it is about (the protocol, for LEAVE_PROTOCOL).
    :param add: Players to add, in the order asked; None when not given.
    :param remove: Players to remove; None when not given.
    :param new_content: The caller publishes new content once the members joined.
    """

    kind: ChangeKind
    target_id: str
    add: tuple[str, ...] | None = None
    remove: tuple[str, ...] | None = None
    new_content: bool = False


class MemberStage(IntEnum):
    """How far the member that is being admitted got."""

    RELEASED = 1
    DISSOLVED = 2


@dataclass(frozen=True, slots=True, kw_only=True)
class GroupProgress:
    """
    How far the admission of a change got between two plans.

    :param next_add: Index in GroupChange.add of the member being admitted.
    :param stage: How far that member got.
    :param admitted: The members that join, in order.
    """

    next_add: int = 0
    stage: MemberStage | None = None
    admitted: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True, kw_only=True)
class Join:
    """
    The join of a member change on a leader, once its members are admitted.

    :param target_id: The leader.
    :param add: The members that join.
    :param remove: The members that leave.
    :param stop_after: Stop the leader once its members left: its group dissolves.
    """

    target_id: str
    add: tuple[str, ...] = ()
    remove: tuple[str, ...] = ()
    stop_after: bool = False


@dataclass(frozen=True, slots=True, kw_only=True)
class Regroup:
    """
    The regroup of a leader at a fresh start, off a protocol none of its members needs anymore.

    :param leader_id: The leader.
    :param protocol_id: The protocol its group leaves.
    :param removed: The protocol ids removed from it; empty when it is not called.
    :param member_ids: The players that join the leader afresh.
    """

    leader_id: str
    protocol_id: str
    removed: tuple[str, ...]
    member_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True, kw_only=True)
class JoinLists:
    """
    The members of a join, per protocol that carries them.

    :param protocol_id: The protocol that carries the group, if any.
    :param protocol_add: Protocol ids that join it.
    :param protocol_remove: Protocol ids that leave it.
    :param native_add: Ids the leader's own protocol adds.
    :param native_remove: Ids the leader's own protocol removes.
    """

    protocol_id: str | None = None
    protocol_add: tuple[str, ...] = ()
    protocol_remove: tuple[str, ...] = ()
    native_add: tuple[str, ...] = ()
    native_remove: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True, kw_only=True)
class ProtocolLeg:
    """
    The join call of the protocol that carries a group.

    :param protocol_id: The protocol.
    :param add: Protocol ids that join it, the stranded members included.
    :param remove: Protocol ids that leave it.
    :param stranded: Members of the leader's own protocol that move along, to release from it
        once the leader renders on the protocol.
    """

    protocol_id: str
    add: tuple[str, ...]
    remove: tuple[str, ...]
    stranded: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True, kw_only=True)
class Handover:
    """
    How the leader's output follows its group onto the protocol that carries it.

    :param moves: Its output moves to the protocol.
    :param previous_output: Its output before.
    :param was_playing: It played, so it resumes once the old protocol let go.
    :param handing_over: It rendered on another output, which is handed over.
    :param note: The line for the log once its output moved.
    """

    moves: bool
    previous_output: str | None
    was_playing: bool
    handing_over: bool
    note: Note | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class OldProtocol:
    """
    The protocol a leader rendered on before its output was handed over.

    :param protocol_id: The protocol.
    :param members: Its members other than the leader, as (protocol id, player id).
    """

    protocol_id: str
    members: tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True, kw_only=True)
class NativeLeg:
    """
    The call that groups the members of the leader's own protocol.

    :param protocol_id: The own protocol.
    :param add: Ids it adds.
    :param remove: Ids it removes.
    """

    protocol_id: str
    add: tuple[str, ...]
    remove: tuple[str, ...]


class Failure(StrEnum):
    """What a failing step does to the change."""

    RAISE = "raise"
    WARN = "warn"
    LEAVE_OUT = "leave_out"
    UNAVAILABLE = "unavailable"
    DEBUG = "debug"


class MemberLists(StrEnum):
    """How a set_members call passes its lists."""

    AS_THEY_ARE = "as_they_are"
    REMOVE_ONLY = "remove_only"


@dataclass(frozen=True, slots=True, kw_only=True)
class SetMembers:
    """Call set_members on a protocol."""

    protocol_id: str
    add: tuple[str, ...] = ()
    remove: tuple[str, ...] = ()
    lists: MemberLists


@dataclass(frozen=True, slots=True, kw_only=True)
class Ungroup:
    """Let a protocol leave its group from its own side."""

    protocol_id: str
    failure: Failure = Failure.RAISE
    message: str = ""


@dataclass(frozen=True, slots=True, kw_only=True)
class PrepareForPlayback:
    """Power a member on for playback."""

    player_id: str


@dataclass(frozen=True, slots=True, kw_only=True)
class MemberChange:
    """A nested member change on a player, without its lock."""

    target_id: str
    add: tuple[str, ...] | None = None
    remove: tuple[str, ...] | None = None
    failure: Failure = Failure.RAISE
    message: str = ""
    args: tuple[object, ...] = ()


@dataclass(frozen=True, slots=True, kw_only=True)
class Detach:
    """A nested detach of a synced member from its leader."""

    member_id: str
    wait_for_update: bool = False
    failure: Failure = Failure.RAISE
    message: str = ""
    args: tuple[object, ...] = ()


@dataclass(frozen=True, slots=True, kw_only=True)
class GroupCommand:
    """A nested member change through cmd_set_members: its routing and its lock."""

    target_id: str
    add: tuple[str, ...] | None = None
    remove: tuple[str, ...] | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class ReleaseSession:
    """End the session of a group player: power it off, or stop it without a power control."""

    group_id: str


@dataclass(frozen=True, slots=True, kw_only=True)
class TransferLeadership:
    """Hand a group whose leader leaves it to one of its members."""

    leader_id: str
    remaining: tuple[str, ...]


@dataclass(frozen=True, slots=True, kw_only=True)
class Note:
    """A line for the log about a decision the plan took."""

    level: int
    message: str
    args: tuple[object, ...] = ()


@dataclass(frozen=True, slots=True, kw_only=True)
class Refuse:
    """End the change with an error."""

    error: type[MusicAssistantError]
    message: str
    translation_key: str | None = None
    translation_args: tuple[str, ...] = ()


type GroupStep = (
    SetMembers
    | Ungroup
    | PrepareForPlayback
    | MemberChange
    | Detach
    | GroupCommand
    | ReleaseSession
    | TransferLeadership
    | Note
    | Refuse
)


@dataclass(frozen=True, slots=True)
class GroupPlan:
    """
    What to do next for a change.

    :param steps: The steps, in order.
    :param progress: Where the change is once the steps ran; None when the plan is complete.
    :param progress_left_out: Where it is when the last step failed in a way that leaves the
        member being admitted out.
    :param passed_on: ROUTE only: the change went to another player.
    :param join: The join that follows the steps of a complete plan, if any.
    :param regroup: The regroup that follows the steps of a complete plan, if any.
    """

    steps: tuple[GroupStep, ...]
    progress: GroupProgress | None = None
    progress_left_out: GroupProgress | None = None
    passed_on: bool = False
    join: Join | None = None
    regroup: Regroup | None = None


def plan_group_change(
    snapshot: GroupingSnapshot, change: GroupChange, progress: GroupProgress | None = None
) -> GroupPlan:
    """
    Return the steps of a group change, up to the next point that needs a new look.

    A complete plan of a member change, or of a regroup at a fresh start, names the join or
    the regroup that follows its steps.

    :param snapshot: The players as they are now.
    :param change: The change a command asks for.
    :param progress: Where the change got to; None for a change that starts.
    """
    return _Planner(snapshot, change, progress or GroupProgress()).plan()


def set_members_group(snapshot: GroupingSnapshot, target_id: str) -> str | None:
    """
    Return the group player a member change on the target is passed on to, if any.

    :param snapshot: The players as they are now.
    :param target_id: The target of the member change.
    """
    target = snapshot.player(target_id)
    if target is None or target.type == PlayerType.GROUP or not target.active_group:
        return None
    owner = snapshot.player(target.playback_owner)
    if (
        owner is not None
        and owner.type == PlayerType.GROUP
        and PlayerFeature.SET_MEMBERS in owner.features
    ):
        return owner.player_id
    return None


def elect_ad_hoc_leader(
    snapshot: GroupingSnapshot, leader_id: str, remaining: Sequence[str]
) -> str:
    """
    Return the member that takes over a group whose leader leaves it.

    The first member that plays audio and can be reached on the protocol the group plays on,
    else the first member that plays audio.

    :param snapshot: The players as they are now.
    :param leader_id: The leader that leaves.
    :param remaining: The members that stay, at least one of them a player that plays audio.
    """
    candidates = [
        member_id
        for member_id in remaining
        if (member := snapshot.player(member_id)) and member.type in PLAYBACK_TARGET_TYPES
    ]
    leader = snapshot.player(leader_id)
    active_domain: str | None = None
    if leader and leader.active_output and leader.active_output != "native":
        if protocol := snapshot.attached(leader, leader.active_output):
            active_domain = protocol.domain
    if active_domain:
        for member_id in candidates:
            member = snapshot.player(member_id)
            if member is not None and active_domain in member.playback_domains:
                return member_id
    return candidates[0]


class GroupedProtocol(Protocol):
    """What tells whether a protocol is grouped: a live protocol, or its view."""

    @property
    def available_for_playback(self) -> bool:
        """Return whether it can play now."""
        ...

    @property
    def synced_to(self) -> str | None:
        """Return the id of the protocol it is synced to."""
        ...

    @property
    def group_members(self) -> Sequence[str]:
        """Return the ids it reports as its group, itself first."""
        ...


def first_grouped_protocol[T: GroupedProtocol](protocols: Iterable[T]) -> T | None:
    """
    Return the first protocol that can play and is grouped: synced, or leading members.

    :param protocols: Audio protocols of a player, most preferred first.
    """
    for protocol in protocols:
        if protocol.available_for_playback and (
            protocol.synced_to or len(protocol.group_members) > 1
        ):
            return protocol
    return None


class _Boundary(Exception):
    """The plan reached a step whose outcome decides what follows."""


class _Refused(Exception):
    """The plan ends with a refusal."""


class GroupRules:
    """
    The decisions of a group change, on one snapshot of the players.

    A join asks a new instance after each device call, as the call may have changed what the
    next decision reads. The lines for the log go to the callback as they are decided.
    """

    def __init__(self, snapshot: GroupingSnapshot, note: Callable[[Note], object]) -> None:
        """
        Initialize the decisions on a snapshot.

        :param snapshot: The players as they are now.
        :param note: Takes each line for the log.
        """
        self.snapshot = snapshot
        self._note_to = note
        # the active outputs set on the strength of these decisions, by player id
        self.outputs: dict[str, str | None] = {}

    def held_player(self, player_id: str) -> PlayerView:
        """
        Return a player the change holds, else the live one.

        :param player_id: Id of the player.
        """
        player = self.snapshot.held_player(player_id)
        assert player is not None  # the entries pass a player that exists
        return player

    def active_output(self, player: PlayerView) -> str | None:
        """
        Return the active output of a player, as these decisions left it.

        :param player: The player.
        """
        if player.player_id in self.outputs:
            return self.outputs[player.player_id]
        return player.active_output

    def translate(self, leader: PlayerView, add: Sequence[str], remove: Sequence[str]) -> JoinLists:
        """
        Translate the members of a join to the protocols that carry them.

        :param leader: The leader.
        :param add: The members that join.
        :param remove: The members that leave.
        """
        protocol: ProtocolView | None = None
        domain: str | None = None
        active = self.active_output(leader)
        if active and active != "native" and (protocol := self.snapshot.attached(leader, active)):
            domain = protocol.domain
        protocol_add, native_add, protocol, domain = self._translate_adds(
            leader, add, protocol, domain
        )
        protocol_remove, native_remove = self._translate_removals(leader, remove, protocol, domain)
        return JoinLists(
            protocol_id=protocol.protocol_id if protocol else None,
            protocol_add=tuple(protocol_add),
            protocol_remove=tuple(protocol_remove),
            native_add=tuple(native_add),
            native_remove=tuple(native_remove),
        )

    def protocol_leg(self, leader: PlayerView, lists: JoinLists) -> ProtocolLeg | Refuse | None:
        """
        Return the join call of the group's protocol, or why the join is refused.

        Members of another domain are left out. A join that switches protocols moves every
        member along, silencing those without it: it is refused while the leader renders.
        Members that ride the leader's own stream are stranded by the switch, so they join the
        protocol group in the same call and are released from the own protocol afterwards.

        :param leader: The leader.
        :param lists: The members per protocol.
        :return: None when the protocol has no call to make.
        """
        protocol = self.snapshot.held_protocol(lists.protocol_id) if lists.protocol_id else None
        if protocol is None or not (lists.protocol_add or lists.protocol_remove):
            return None
        add = self._of_domain(lists.protocol_add, protocol)
        remove = self._of_domain(lists.protocol_remove, protocol)
        if not add and not remove:
            return None
        if PlayerFeature.SET_MEMBERS not in protocol.features:
            self._note_unsupported(protocol)
            return None
        if add and (refusal := self._stranding_refusal(leader, protocol, add)):
            return refusal
        stranded = self._migrate_stranded(leader, protocol, add) if add else []
        return ProtocolLeg(
            protocol_id=protocol.protocol_id,
            add=tuple(add),
            remove=tuple(remove),
            stranded=tuple(stranded),
        )

    def member_output(self, protocol_id: str) -> str | None:
        """
        Return the player of a joining protocol when its output has to move to that protocol.

        :param protocol_id: The protocol that joins.
        """
        if (protocol := self.snapshot.protocols.get(protocol_id)) is None:
            return None
        if (
            protocol.player_id is None
            or (player := self.snapshot.players.get(protocol.player_id)) is None
        ):
            return None
        if protocol_id == player.own_protocol or self.active_output(player) == protocol_id:
            return None
        self.outputs[player.player_id] = protocol_id
        return player.player_id

    def leaving_outputs(self, protocol_ids: Iterable[str]) -> list[str]:
        """
        Return the players whose active output is cleared, as their protocol left the group.

        :param protocol_ids: The protocols that left.
        """
        cleared: list[str] = []
        for protocol_id in protocol_ids:
            if (protocol := self.snapshot.protocols.get(protocol_id)) is None:
                continue
            if (
                protocol.player_id is None
                or (player := self.snapshot.players.get(protocol.player_id)) is None
            ):
                continue
            if protocol_id == player.own_protocol:
                continue
            if self.active_output(player) == protocol_id:
                cleared.append(player.player_id)
                self.outputs[player.player_id] = None
        return cleared

    def handover(self, leader: PlayerView, protocol_id: str) -> Handover:
        """
        Return how the leader's output follows its members onto the protocol they joined.

        :param leader: The leader.
        :param protocol_id: The protocol they joined.
        """
        previous = self.active_output(leader)
        was_playing = leader.playback_state == PlaybackState.PLAYING
        # A paused player still holds its output, so the handover has to run for it too.
        was_rendering = leader.playback_state in RENDERING
        # the group is on the parent's own protocol
        is_own = protocol_id == leader.own_protocol
        already_native = previous in (None, "native")
        already_this = previous == protocol_id
        switching = not ((is_own and already_native) or already_this)
        handing_over = was_rendering and switching
        moves = not (is_own and already_native)
        if moves:
            self.outputs[leader.player_id] = "native" if is_own else protocol_id
        note: Note | None = None
        if handing_over:
            protocol = self.snapshot.held_protocol(protocol_id)
            note = Note(
                level=logging.INFO,
                message="Handing the output of %s over to the %s protocol%s",
                args=(
                    leader.name,
                    protocol.domain if protocol else "",
                    " and resuming playback" if was_playing else "",
                ),
            )
        return Handover(
            moves=moves,
            previous_output=previous,
            was_playing=was_playing,
            handing_over=handing_over,
            note=note,
        )

    def own_protocol(self, leader: PlayerView) -> str | None:
        """
        Return the id of the leader's own protocol, while it is attached.

        :param leader: The leader.
        """
        own = self.snapshot.own(leader)
        return own.protocol_id if own is not None else None

    def old_protocol(
        self, leader: PlayerView, previous_output: str | None, protocol_id: str
    ) -> OldProtocol | None:
        """
        Return the audio protocol the leader rendered on before its output was handed over.

        :param leader: The leader.
        :param previous_output: Its active output before.
        :param protocol_id: The protocol that took the output over.
        """
        if (old := self._previous_protocol(leader, previous_output, protocol_id)) is None:
            return None
        return OldProtocol(
            protocol_id=old.protocol_id,
            members=tuple(self._protocol_group_members(leader, old)),
        )

    def removal(self, protocol_id: str, protocol_ids: Sequence[str]) -> list[str]:
        """
        Return the members a protocol group can be called to remove.

        :param protocol_id: The protocol.
        :param protocol_ids: The protocol members to remove.
        :return: The protocol ids to pass; empty when there is no call to make.
        """
        if (protocol := self.snapshot.held_protocol(protocol_id)) is None:
            return []
        return self._removal(protocol, protocol_ids)

    def native_leg(
        self, leader: PlayerView, add: Sequence[str], remove: Sequence[str]
    ) -> NativeLeg | None:
        """
        Return the call that groups the native members on the leader's own protocol.

        :param leader: The leader.
        :param add: The native members that join.
        :param remove: The native members that leave.
        :return: None when the own protocol has no call to make.
        """
        own = self.snapshot.own(leader)
        if not (add or remove) or own is None:
            return None
        native_add = self._native_additions(add, leader, own)
        # For removal, allow the audio protocol of a member only if it is actually in the
        # group_members of the parent's own protocol. This handles a native protocol of an
        # audio domain (e.g. native AirPlay) where group_members contains the audio
        # protocols of other players
        native_remove = [
            protocol_id
            for protocol_id in remove
            if (member := self.snapshot.player_of(protocol_id))
            and (
                protocol_id in own.group_members
                or protocol_id == self._native_member_id(leader, member)
            )
        ]
        if not (native_add or native_remove) or PlayerFeature.SET_MEMBERS not in own.features:
            return None
        self._note(
            logging.INFO,
            "Calling set_members on native player %s with add=%s, remove=%s",
            leader.name,
            native_add,
            native_remove,
        )
        return NativeLeg(
            protocol_id=own.protocol_id, add=tuple(native_add), remove=tuple(native_remove)
        )

    # -- translation --

    def _translate_adds(
        self,
        parent: PlayerView,
        player_ids: Sequence[str],
        protocol: ProtocolView | None,
        domain: str | None,
    ) -> tuple[list[str], list[str], ProtocolView | None, str | None]:
        """
        Translate the members that join to the protocols that carry them.

        :param parent: The leader.
        :param player_ids: The members that join.
        :param protocol: The protocol the group is on so far.
        :param domain: Its domain.
        :return: The protocol members, the native members, the group protocol and its domain.
        """
        protocol_members: list[str] = []
        native_members: list[str] = []
        parent_own = self.snapshot.own(parent)
        supports_native = (
            parent_own is not None and PlayerFeature.SET_MEMBERS in parent_own.features
        )
        ordered = list(player_ids)
        if self._live_native_session(parent):
            ordered.sort(
                key=lambda player_id: bool(
                    (child := self.snapshot.player(player_id))
                    and self._groups_natively(child, parent, supports_native)
                )
            )
        for child_id in ordered:
            if (child := self.snapshot.player(child_id)) is None:
                continue
            protocol, domain = self._select_grouping(
                child, parent, protocol, domain, supports_native, protocol_members, native_members
            )
        # the protocol selected for the group is only known once every child has been
        # processed, so the members picked for native grouping are corrected here
        if (
            native_members
            and domain
            and protocol
            and parent_own is not None
            and protocol.protocol_id != parent_own.protocol_id
            and parent_own.needs_own_stream
        ):
            protocol_members.extend(self._move_to_domain(parent, domain, native_members))
            native_members.clear()
        return protocol_members, native_members, protocol, domain

    def _select_grouping(
        self,
        child: PlayerView,
        parent: PlayerView,
        protocol: ProtocolView | None,
        domain: str | None,
        supports_native: bool,
        protocol_members: list[str],
        native_members: list[str],
    ) -> tuple[ProtocolView | None, str | None]:
        """
        Pick how one member joins, and add it to the matching list.

        :param child: The member.
        :param parent: The leader.
        :param protocol: The protocol the group is on so far.
        :param domain: Its domain.
        :param supports_native: Whether the leader groups natively.
        :param protocol_members: The protocol members so far.
        :param native_members: The native members so far.
        :return: The protocol the group is on, and its domain.
        """
        natively = self._groups_natively(child, parent, supports_native)
        # The parent is already playing natively and the child can join that native
        # session directly - adopt it before considering the child's own preferred output
        # protocol (a child joining an existing group must not force the whole group onto
        # it). Skipped once a protocol has been selected, so mixed batches stay cohesive.
        if self._live_native_session(parent) and not domain and natively:
            native_members.append(self._native_member_id(parent, child))
            return protocol, domain
        child_own = self.snapshot.own(child)
        # a player that runs its own multiroom (e.g. a LinkPlay control shell) keeps
        # grouping on its native path rather than routing it through an audio protocol that
        # is merely its preferred playback output. Native compatibility still decides
        # whether this is possible, so an incompatible/cross-backend pair falls through.
        if child_own is not None and child_own.prefers_native and natively:
            native_members.append(self._native_member_id(parent, child))
            return protocol, domain
        grouped, protocol, domain = self._via_preferred(
            child, parent, protocol, domain, protocol_members
        )
        if grouped:
            return protocol, domain
        # the protocol the group is already on; dropped when it can not group members
        # itself, so a later grouping method can select a new protocol
        if protocol is not None and domain and PlayerFeature.SET_MEMBERS not in protocol.features:
            protocol, domain = None, None
        elif protocol is not None and domain:
            output = child.output_by_domain(domain)
            if output and output.available:
                protocol_members.append(self._member_protocol_id(child, output))
                return protocol, domain
        if natively:
            native_members.append(self._native_member_id(parent, child))
            return protocol, domain
        common = self._common_output(child, parent)
        if common is not None:
            parent_output, child_output = common
            if protocol is None or domain != parent_output.domain:
                protocol = self.snapshot.output_protocol(parent, parent_output.output_id)
                if protocol is not None:
                    domain = protocol.domain
            protocol_members.append(self._member_protocol_id(child, child_output))
            return protocol, domain
        self._note(
            logging.WARNING,
            "Cannot group %s with %s: no compatible grouping method found "
            "(tried: child preferred protocol, parent active protocol, "
            "native grouping, common protocols)",
            child.name,
            parent.name,
        )
        return protocol, domain

    def _via_preferred(
        self,
        child: PlayerView,
        parent: PlayerView,
        protocol: ProtocolView | None,
        domain: str | None,
        protocol_members: list[str],
    ) -> tuple[bool, ProtocolView | None, str | None]:
        """
        Group a member through the output it prefers, when the group can be on it.

        :param child: The member.
        :param parent: The leader.
        :param protocol: The protocol the group is on so far.
        :param domain: Its domain.
        :param protocol_members: The protocol members so far.
        :return: Whether it was grouped, and the protocol the group is on with its domain.
        """
        preferred = child.preferred_output
        if not preferred or preferred in {"auto", "native"}:
            return False, protocol, domain
        child_output = next(
            (output for output in child.outputs if output.output_id == preferred), None
        )
        if not child_output or not child_output.available:
            return False, protocol, domain
        parent_output = parent.output_by_domain(child_output.domain)
        if not parent_output or not parent_output.available:
            return False, protocol, domain
        parent_protocol = self.snapshot.output_protocol(parent, parent_output.output_id)
        if not parent_protocol or PlayerFeature.SET_MEMBERS not in parent_protocol.features:
            return False, protocol, domain
        if domain and child_output.domain != domain:
            return False, protocol, domain
        if not protocol or domain != child_output.domain:
            protocol = parent_protocol
            domain = child_output.domain
        protocol_members.append(child_output.output_id)
        return True, protocol, domain

    def _translate_removals(
        self,
        parent: PlayerView,
        player_ids: Sequence[str],
        protocol: ProtocolView | None,
        domain: str | None,
    ) -> tuple[list[str], list[str]]:
        """
        Translate the members that leave to the protocols that hold them.

        :param parent: The leader.
        :param player_ids: The members that leave.
        :param protocol: The protocol the group is on.
        :param domain: Its domain.
        :return: The protocol members and the native members to remove.
        """
        protocol_members: list[str] = []
        native_members: list[str] = []
        parent_own = self.snapshot.own(parent)
        native_group_members = parent_own.group_members if parent_own else ()
        for child_id in player_ids:
            if (child := self.snapshot.player(child_id)) is None:
                continue
            if domain and protocol:
                output = child.output_by_domain(domain)
                if output and output.available:
                    member_id = self._member_protocol_id(child, output)
                    if member_id in protocol.group_members:
                        protocol_members.append(member_id)
                        continue
            # one of the child's audio protocols in the parent's native group_members: a
            # native protocol of an audio domain (e.g. a native AirPlay player like Apple TV)
            # groups the audio protocols of other players
            native_id = next(
                (pid for pid in child.audio_protocols if pid in native_group_members), None
            )
            native_members.append(native_id or self._native_member_id(parent, child))
        return protocol_members, native_members

    # -- the join's rules --

    def _stranding_refusal(
        self, parent: PlayerView, protocol: ProtocolView, add: Sequence[str]
    ) -> Refuse | None:
        """
        Return the refusal of a join that switches protocols while old members can not follow.

        :param parent: The leader.
        :param protocol: The protocol the members join.
        :param add: The protocol members that join.
        """
        if parent.playback_state not in RENDERING:
            return None
        old = self._previous_protocol(parent, self.active_output(parent), protocol.protocol_id)
        if old is None:
            return None
        unable: list[str] = []
        for _, member_id in self._protocol_group_members(parent, old):
            member = self.snapshot.player(member_id)
            output = member.output_by_domain(protocol.domain) if member else None
            if not output or not output.available:
                unable.append(member.name if member else member_id)
        if not unable:
            return None
        added_names = ", ".join(
            player.name if (player := self.snapshot.player_of(member_id)) else member_id
            for member_id in add
        )
        stranded_names = ", ".join(unable)
        return Refuse(
            error=PlayerCommandFailed,
            message=f"Cannot add {added_names} to {parent.name}: "
            f"{stranded_names} cannot play over {protocol.provider_name}",
            translation_key="member_cannot_follow_protocol",
            translation_args=(
                added_names,
                parent.name,
                stranded_names,
                protocol.provider_name,
            ),
        )

    def _migrate_stranded(
        self, parent: PlayerView, protocol: ProtocolView, add: list[str]
    ) -> list[str]:
        """
        Move the native members a switch to the protocol strands onto it.

        :param parent: The leader.
        :param protocol: The protocol the leader will render through.
        :param add: The protocol members that join, extended with the moved ones.
        :return: The native members to release from the own protocol.
        """
        own = self.snapshot.own(parent)
        if own is None or protocol.protocol_id == own.protocol_id or not own.needs_own_stream:
            return []
        if self.active_output(parent) not in (None, "native"):
            return []
        stranded = [member_id for member_id in own.group_members if member_id != own.protocol_id]
        for protocol_id in self._move_to_domain(parent, protocol.domain, stranded):
            if protocol_id not in add:
                add.append(protocol_id)
        return stranded

    def _removal(self, protocol: ProtocolView, protocol_ids: Sequence[str]) -> list[str]:
        """
        Return the members a protocol group can be called to remove, as the join's call does.

        :param protocol: The protocol.
        :param protocol_ids: The protocol members to remove.
        :return: The protocol ids to pass; empty when there is no call to make.
        """
        remove = self._of_domain(protocol_ids, protocol)
        if not remove:
            return []
        if PlayerFeature.SET_MEMBERS not in protocol.features:
            self._note_unsupported(protocol)
            return []
        return remove

    def _previous_protocol(
        self, parent: PlayerView, previous: str | None, protocol_id: str | None
    ) -> ProtocolView | None:
        """
        Return the audio protocol the leader renders through before switching to another one.

        :param parent: The leader.
        :param previous: Its active output before the switch.
        :param protocol_id: The protocol taking the output over.
        """
        if previous is None or previous == "native":
            return None
        old = self.snapshot.attached(parent, previous)
        if old is None or old.protocol_id == protocol_id:
            return None
        return old

    def _protocol_group_members(
        self, parent: PlayerView, protocol: ProtocolView
    ) -> list[tuple[str, str]]:
        """
        Return the members of a protocol group other than the leader, as (protocol id, player id).

        :param parent: The leader.
        :param protocol: The protocol that holds the group.
        """
        members: list[tuple[str, str]] = []
        for member_id in protocol.group_members:
            if member_id == protocol.protocol_id:
                continue
            if (member := self.snapshot.player_of(member_id)) is None:
                continue
            if member.player_id != parent.player_id:
                members.append((member_id, member.player_id))
        return members

    def _move_to_domain(
        self, parent: PlayerView, domain: str, member_ids: Sequence[str]
    ) -> list[str]:
        """
        Translate natively grouped members onto a protocol domain, leaving out who lacks it.

        :param parent: The leader.
        :param domain: The domain of the group's protocol.
        :param member_ids: The native member ids.
        """
        translated: list[str] = []
        for member_id in member_ids:
            if (member := self.snapshot.player_of(member_id)) is None:
                continue
            output = member.output_by_domain(domain)
            if not output or not output.available:
                self._note(
                    logging.WARNING,
                    "Cannot group %s with %s: the group plays through the %s protocol, "
                    "which %s does not support",
                    member.name,
                    parent.name,
                    domain,
                    member.name,
                )
                continue
            translated.append(self._member_protocol_id(member, output))
        return translated

    def _native_additions(
        self, member_ids: Sequence[str], parent: PlayerView, own: ProtocolView
    ) -> list[str]:
        """
        Return the native members the leader's own protocol can group.

        :param member_ids: The native member ids.
        :param parent: The leader.
        :param own: Its own protocol.
        """
        if own.role == ProtocolRole.VIRTUAL:
            return [member_id for member_id in member_ids if self.snapshot.player(member_id)]
        return [
            member_id
            for member_id in member_ids
            if (protocol := self.snapshot.protocols.get(member_id))
            and (
                protocol.provider_id == own.provider_id
                or member_id in own.can_group_with
                or protocol.provider_id in own.can_group_with
            )
        ]

    # -- small rules --

    def _groups_natively(
        self, child: PlayerView, parent: PlayerView, supports_native: bool
    ) -> bool:
        """
        Return whether a member can group natively with the leader.

        :param child: The member.
        :param parent: The leader.
        :param supports_native: Whether the leader groups natively.
        """
        parent_own = self.snapshot.own(parent)
        child_own = self.snapshot.own(child)
        if not supports_native or parent_own is None or child_own is None:
            return False
        return (
            child_own.protocol_id in parent_own.native_peers
            or child_own.protocol_id in parent_own.can_group_with
            or child_own.provider_id in parent_own.can_group_with
        )

    def _live_native_session(self, parent: PlayerView) -> bool:
        """
        Return whether the leader plays natively right now.

        :param parent: The leader.
        """
        return self.active_output(parent) == "native" and parent.playback_state in RENDERING

    def _common_output(
        self, child: PlayerView, parent: PlayerView
    ) -> tuple[OutputView, OutputView] | None:
        """
        Return the first output of the leader that the member shares and that groups.

        :param child: The member.
        :param parent: The leader.
        """
        for parent_output in parent.outputs:
            if not parent_output.available:
                continue
            child_output = child.output_by_domain(parent_output.domain)
            if not child_output or not child_output.available:
                continue
            protocol = self.snapshot.output_protocol(parent, parent_output.output_id)
            if protocol and PlayerFeature.SET_MEMBERS in protocol.features:
                return parent_output, child_output
        return None

    def _member_protocol_id(self, player: PlayerView, output: OutputView) -> str:
        """
        Return the id of the protocol behind an output of a player.

        :param player: The player.
        :param output: One of its outputs.
        """
        if output.is_native and player.own_protocol is not None and self.snapshot.own(player):
            return player.own_protocol
        return output.output_id

    def _native_member_id(self, parent: PlayerView, member: PlayerView) -> str:
        """
        Return the id the leader's own protocol knows a member by.

        :param parent: The leader.
        :param member: The member.
        """
        parent_own = self.snapshot.own(parent)
        member_own = self.snapshot.own(member)
        if parent_own is not None and parent_own.role != ProtocolRole.VIRTUAL and member_own:
            return member_own.protocol_id
        return member.player_id

    def _of_domain(self, protocol_ids: Iterable[str], protocol: ProtocolView | None) -> list[str]:
        """
        Return the protocol ids of attached protocols of the domain of a protocol.

        :param protocol_ids: The ids to filter.
        :param protocol: The protocol whose domain counts.
        """
        if protocol is None:
            return []
        return [
            protocol_id
            for protocol_id in protocol_ids
            if (member := self.snapshot.protocols.get(protocol_id))
            and member.domain == protocol.domain
        ]

    def _note(self, level: int, message: str, *args: object) -> None:
        """
        Add a line for the log.

        :param level: The log level.
        :param message: The message, with %s for the args.
        :param args: The args.
        """
        self._note_to(Note(level=level, message=message, args=args))

    def _note_unsupported(self, protocol: ProtocolView) -> None:
        """
        Note that a protocol picked for a group can not group members.

        :param protocol: The protocol.
        """
        self._note(
            logging.ERROR,
            "Protocol %s does not support SET_MEMBERS, cannot perform grouping. "
            "This should have been caught earlier in the flow.",
            protocol.protocol_id,
        )


class _Planner(GroupRules):
    """The planning of one group change from one snapshot."""

    def __init__(
        self, snapshot: GroupingSnapshot, change: GroupChange, progress: GroupProgress
    ) -> None:
        """
        Initialize the planning.

        :param snapshot: The players as they are now.
        :param change: The change.
        :param progress: Where the change got to.
        """
        self.steps: list[GroupStep] = []
        super().__init__(snapshot, self.steps.append)
        self.change = change
        self.progress = progress
        self.next_progress: GroupProgress | None = None
        self.left_out_progress: GroupProgress | None = None
        self.passed_on = False
        self.join: Join | None = None
        self.regroup: Regroup | None = None

    def plan(self) -> GroupPlan:
        """Return the plan of the change from the snapshot."""
        handlers = {
            ChangeKind.ROUTE: self._route,
            ChangeKind.MEMBERS: self._members,
            ChangeKind.UNGROUP: self._ungroup,
            ChangeKind.DETACH: self._detach,
            ChangeKind.LEAVE: self._leave,
            ChangeKind.LEAVE_FOR_REMOVAL: self._leave_for_removal,
            ChangeKind.REGROUP_FOR_START: self._regroup_for_start,
            ChangeKind.LEAVE_PROTOCOL: self._leave_protocol,
        }
        with contextlib.suppress(_Boundary, _Refused):
            handlers[self.change.kind]()
        return GroupPlan(
            tuple(self.steps),
            progress=self.next_progress,
            progress_left_out=self.left_out_progress,
            passed_on=self.passed_on,
            join=self.join,
            regroup=self.regroup,
        )

    # -- the kinds of change --

    def _route(self) -> None:
        """Route a member change: refuse it, pass it on to a group, or release the target first."""
        target = self._player(self.change.target_id)
        if PlayerFeature.SET_MEMBERS not in target.features:
            self._refuse(
                UnsupportedFeaturedException,
                f"Player {target.name} does not support group commands",
            )
        # if the target player is a member of an active group player (e.g. a syncgroup),
        # redirect the command to that group player so it can manage the member change
        if group_id := set_members_group(self.snapshot, target.player_id):
            group = self._player(group_id)
            self._note(
                logging.DEBUG,
                "Redirecting set_members from %s to its group player %s",
                target.name,
                group.name,
            )
            self.steps.append(
                GroupCommand(target_id=group_id, add=self.change.add, remove=self.change.remove)
            )
            self.passed_on = True
            return
        # target player is already synced itself to another player:
        # automatically ungroup it first and wait for state to propagate
        if target.synced_to:
            self._release(target, "setting members")

    def _members(self) -> None:
        """Plan a member change on a leader or a group player."""
        target = self._held_target()
        add = list(self.change.add or ())
        remove = list(self.change.remove or ())
        stop_after = False
        # handle the sync leader being removed from itself: either transfer leadership
        # to a remaining member (keeping playback alive) or dissolve the group entirely
        if remove and target.player_id in remove:
            if self._hands_over_leadership(target, remove):
                return
            self._note(
                logging.INFO,
                "Dissolving sync group of player %s as it is being removed from itself",
                target.name,
            )
            add = []
            remove = [member for member in target.group_members if member != target.player_id]
            stop_after = True
        admitted = self._admit(target, add)
        accepted = self._accept_removals(target, remove)
        # GROUP players (sync_group, universal_group) manage their own members internally
        # and don't need protocol translation - call their set_members directly
        if (
            target.type == PlayerType.GROUP
            and PlayerFeature.SET_MEMBERS in target.features
            and target.own_protocol
            and self.snapshot.own(target)
        ):
            self.steps.append(
                SetMembers(
                    protocol_id=target.own_protocol,
                    add=tuple(admitted),
                    remove=tuple(accepted),
                    lists=MemberLists.AS_THEY_ARE,
                )
            )
            return
        self.join = Join(
            target_id=target.player_id,
            add=tuple(admitted),
            remove=tuple(accepted),
            stop_after=stop_after,
        )

    def _ungroup(self) -> None:
        """Take a player out of whatever holds it."""
        player_id = self.change.target_id
        if (player := self.snapshot.player(player_id)) is None:
            self._note(logging.WARNING, "Player %s is not available", player_id)
            return
        # Ungroup on a group player is interpreted as 'release the captured
        # session entirely'. This avoids the "Cannot remove static member"
        # error path when transfer_queue or HA's unjoin asks us to release a
        # group that has static members.
        if player.type == PlayerType.GROUP:
            self.steps.append(ReleaseSession(group_id=player.player_id))
            return
        if player.active_group:
            group = self.snapshot.player(player.active_group)
            # Static members can't be released individually — release the
            # session of the group as a whole. Access to the member is enough.
            if group is not None and player_id in group.static_group_members:
                self.steps.append(ReleaseSession(group_id=group.player_id))
                return
            # dynamic or non-static member — remove just this player
            self.steps.append(GroupCommand(target_id=player.active_group, remove=(player_id,)))
            return
        if player.synced_to:
            parent = self.snapshot.player(player.synced_to)
            if parent and parent.available_for_playback:
                self.steps.append(GroupCommand(target_id=parent.player_id, remove=(player_id,)))
            else:
                self.steps.append(Detach(member_id=player.player_id))
            return
        # player is a sync leader (a non-group player with synced followers).
        # Remove only the leader itself: the member change will either transfer
        # leadership to a remaining member (keeping playback alive) or, when no
        # members remain / nothing is playing, dissolve the group and stop.
        if player.group_members:
            self.steps.append(GroupCommand(target_id=player.player_id, remove=(player.player_id,)))
            return
        # unjoin from any dynamic sync groups if we're currently in one (edge case)
        # this is in particular used for the Home Assistant integration which does
        # not have a set_members command and only supports a single unjoin command
        for leader in self.snapshot.players.values():
            if not leader.iterated or not leader.group_members or leader.synced_to:
                continue
            if PlayerFeature.SET_MEMBERS not in leader.features:
                continue
            if player_id in leader.static_group_members:
                continue
            if player_id in leader.group_members:
                self.steps.append(GroupCommand(target_id=leader.player_id, remove=(player_id,)))
                return

    def _detach(self) -> None:
        """Take a synced member out of its leader's group."""
        member = self._player(self.change.target_id)
        parent = self.snapshot.player(member.synced_to) if member.synced_to else None
        if parent and parent.available_for_playback:
            self._note(
                logging.DEBUG, "Removing %s from its sync leader %s", member.name, parent.name
            )
            self.steps.append(MemberChange(target_id=parent.player_id, remove=(member.player_id,)))
            return
        # a sync leader that is gone (or offline) can't release its members, so the
        # member has to leave the group from its own side: natively, or through the
        # audio protocol that holds the membership (e.g. an AirPlay group)
        own = self.snapshot.own(member)
        target = own
        if target is None or not target.synced_to:
            target = next(
                (
                    protocol
                    for protocol_id in member.audio_protocols
                    if (protocol := self.snapshot.protocols.get(protocol_id)) and protocol.synced_to
                ),
                target,
            )
        message = (
            f"{member.name} is synced to the unavailable player {member.synced_to} "
            "and could not leave the group on its own"
        )
        if target is None:
            self._refuse(PlayerUnavailableError, message)
        assert target is not None  # for type checking
        self._note(
            logging.DEBUG,
            "Sync leader %s of %s is unavailable, letting %s leave the group on its own",
            member.synced_to,
            member.name,
            target.protocol_id,
        )
        self.steps.append(
            Ungroup(protocol_id=target.protocol_id, failure=Failure.UNAVAILABLE, message=message)
        )

    def _leave(self) -> None:
        """Drop a player that can not take commands any more from its group or leader."""
        if (player := self.snapshot.player(self.change.target_id)) is None:
            return
        if not (parent_id := player.active_group or player.synced_to):
            return
        # the player is part of a (permanent) groupplayer or synced to a leader
        if (parent := self.snapshot.player(parent_id)) is None:
            return
        self.steps.append(
            MemberChange(
                target_id=parent.player_id,
                remove=(self.change.target_id,),
                failure=Failure.DEBUG,
                message="Could not detach player %s from its group: %s",
                args=(player.name,),
            )
        )

    def _leave_for_removal(self) -> None:
        """Take a player that is removed out of the group or leader it plays with."""
        player = self._player(self.change.target_id)
        if (
            player.active_group
            and (group := self.snapshot.player(player.active_group))
            and group.own_protocol
            and self.snapshot.own(group)
        ):
            # try to remove from the group: a static group refuses to let one member go
            self.steps.append(
                SetMembers(
                    protocol_id=group.own_protocol,
                    remove=(player.player_id,),
                    lists=MemberLists.REMOVE_ONLY,
                )
            )
        elif player.synced_to:
            self.steps.append(Detach(member_id=player.player_id))

    def _regroup_for_start(self) -> None:
        """Regroup the group a player leads off a protocol none of its members needs anymore."""
        player = self._held_target()
        protocol = self._grouped_output_protocol(player)
        if not protocol or protocol.synced_to or len(protocol.group_members) < 2:
            return
        protocol_member_ids = [
            member_id for member_id in protocol.group_members if member_id != protocol.protocol_id
        ]
        member_ids: list[str] = []
        for protocol_member_id in protocol_member_ids:
            member = self.snapshot.player_of(protocol_member_id)
            if not member or not member.available_for_playback:
                return
            member_ids.append(member.player_id)
        protocol_members, native_members, _, protocol_domain = self._translate_adds(
            player, member_ids, None, None
        )
        placed = len(protocol_members) + len(native_members)
        if protocol_domain == protocol.domain or placed != len(member_ids):
            return
        # the player plays through its preferred protocol, so the group may only move onto that one
        preferred = player.preferred_output
        if (
            preferred
            and preferred not in ("auto", "native")
            and (preferred_protocol := self._linked(player, str(preferred)))
            and preferred_protocol.domain != protocol_domain
        ):
            return
        self._note(
            logging.INFO,
            "Regrouping %s off the %s protocol, which none of its members need anymore",
            player.name,
            protocol.domain,
        )
        # Removed on the protocol itself: the player's active output protocol may
        # already be cleared after its last session, so a regular removal would not find them.
        removed = self._removal(protocol, protocol_member_ids)
        self.regroup = Regroup(
            leader_id=player.player_id,
            protocol_id=protocol.protocol_id,
            removed=tuple(removed),
            member_ids=tuple(member_ids),
        )

    def _leave_protocol(self) -> None:
        """Let a protocol an external source took over leave its group."""
        self.steps.append(Ungroup(protocol_id=self.change.target_id))

    # -- admission --

    def _hands_over_leadership(self, target: PlayerView, remove: list[str]) -> bool:
        """
        Hand the group of a leader that removes itself to a member, when it plays its own queue.

        :param target: The leader.
        :param remove: The players to remove, the leader among them.
        :return: Whether the leadership is handed over (nothing else happens then).
        """
        remaining = [
            member_id
            for member_id in target.group_members
            if member_id != target.player_id
            and member_id not in remove
            and (member := self.snapshot.player(member_id))
            and member.available_for_playback
        ]
        # a new leader must be able to render audio: a group with only display,
        # visualizer or lighting members left has no playback heir and dissolves
        has_playback_heir = any(
            (member := self.snapshot.player(member_id)) and member.type in PLAYBACK_TARGET_TYPES
            for member_id in remaining
        )
        # only a queue of the leader's own is handed over: the elected leader of a group
        # player plays the group's queue, and that group re-forms around another member
        # itself (its native followers may still linger on it after a dissolve)
        queue = target.active_queue
        if (
            has_playback_heir
            and queue
            and queue.queue_id == target.player_id
            and queue.state != PlaybackState.IDLE
        ):
            self.steps.append(
                TransferLeadership(leader_id=target.player_id, remaining=tuple(remaining))
            )
            return True
        return False

    def _admit(self, target: PlayerView, add: list[str]) -> list[str]:
        """
        Admit the members to add, one by one, up to a step whose outcome decides what follows.

        :param target: The leader or group player.
        :param add: The players to add, in the order asked.
        :return: The members that join.
        """
        admitted = list(self.progress.admitted)
        for index in range(self.progress.next_add, len(add)):
            child_id = add[index]
            stage = self.progress.stage if index == self.progress.next_add else None
            if stage is None and not self._accepts(target, child_id, admitted):
                continue
            child = (
                self.snapshot.player(child_id)
                if stage is None
                else self.snapshot.held_player(child_id)
            )
            if child is None:
                continue
            # a child synced to a different player is ungrouped first, and the state it then
            # reports decides what follows; not if it is part of this group via its sync leader
            if stage is None and self._needs_release(target, child):
                self._release(child, f"joining {target.name}")
                self._boundary(
                    replace(
                        self.progress,
                        next_add=index,
                        stage=MemberStage.RELEASED,
                        admitted=tuple(admitted),
                    )
                )
            # the child leads a native group of its own, which it cannot keep serving from
            # inside this one
            if stage != MemberStage.DISSOLVED:
                if not self._own_group_given_up(target, child, index, admitted):
                    continue
            self.steps.append(PrepareForPlayback(player_id=child.player_id))
            admitted.append(child_id)
            if not child.powered and child.power_control != PLAYER_CONTROL_NONE:
                self._boundary(
                    replace(self.progress, next_add=index + 1, stage=None, admitted=tuple(admitted))
                )
        return admitted

    def _accepts(self, target: PlayerView, child_id: str, admitted: list[str]) -> bool:
        """
        Return whether a member passes the checks of a join, noting why when it does not.

        :param target: The leader or group player.
        :param child_id: The player to add.
        :param admitted: The members admitted so far.
        """
        if child_id == target.player_id or child_id in admitted:
            return False
        child = self.snapshot.player(child_id)
        if child and child.listed and child.switched_off:
            # whoever takes it along powers it on, and it joins once its device is online
            self._note(
                logging.DEBUG,
                "Player %s is switched off at its power control, it is left out for now",
                child.name,
            )
            return False
        if not child or not child.available_for_playback:
            self._note(logging.WARNING, "Player %s is not available", child_id)
            return False
        # can_group_with already handles all expansion and translation
        if child_id not in target.can_group_with:
            self._note(
                logging.WARNING, "Player %s can not be grouped with %s", child.name, target.name
            )
            return False
        if (
            child.synced_to
            and child.synced_to == target.player_id
            and child_id in target.group_members
        ):
            return False
        # also skip if the child is part of this group via its sync leader
        # (e.g. synced to the sync leader of this syncgroup)
        return not (child.active_group == target.player_id and child_id in target.group_members)

    def _needs_release(self, target: PlayerView, child: PlayerView) -> bool:
        """
        Return whether a member is synced to another player, so it leaves that one first.

        :param target: The leader or group player.
        :param child: The member.
        """
        return bool(child.synced_to) and target.player_id not in {
            child.synced_to,
            child.active_group,
        }

    def _release(self, player: PlayerView, context: str) -> None:
        """
        Release a synced player from its leader, waiting for its update.

        :param player: The synced player.
        :param context: What it is released for, for the log.
        """
        self._note(
            logging.INFO,
            "Player %s is already synced to %s, ungrouping it first before %s",
            player.name,
            player.synced_to,
            context,
        )
        self.steps.append(
            Detach(
                member_id=player.player_id,
                wait_for_update=True,
                failure=Failure.WARN,
                message="Failed to auto-ungroup %s from %s, proceeding anyway",
                args=(player.name, player.synced_to),
            )
        )

    def _own_group_given_up(
        self, target: PlayerView, child: PlayerView, index: int, admitted: list[str]
    ) -> bool:
        """
        Dissolve the group a member leads when its members ride its own stream.

        :param target: The group the member joins.
        :param child: The member.
        :param index: Its index in the players to add.
        :param admitted: The members admitted so far.
        :return: False when the member stays out.
        """
        own = self.snapshot.own(child)
        if own is None or not own.needs_own_stream:
            return True
        members = [member for member in child.group_members if member != child.player_id]
        if not members:
            return True
        # Grouping never offers a rendering leader as a target, so reaching this means
        # the caller worked from a can_group_with snapshot taken before the playback
        # started. Tearing the group down now would cut its members off mid-track.
        if child.playback_state in RENDERING:
            self._note(
                logging.WARNING,
                "Player %s is still serving its own group, leaving it out of %s",
                child.name,
                target.name,
            )
            return False
        self._note(
            logging.INFO,
            "Player %s leads a group of its own, dissolving it before it joins %s",
            child.name,
            target.name,
        )
        # Removing the members rather than the leader keeps this out of the leadership
        # transfer path. Joining on top of a group it still leads is the silent-member
        # state this dissolve exists to prevent, so a failure leaves the player out.
        self.steps.append(
            MemberChange(
                target_id=child.player_id,
                remove=tuple(members),
                failure=Failure.LEAVE_OUT,
                message="Could not dissolve the group of %s, leaving it out of %s",
                args=(child.name, target.name),
            )
        )
        self.left_out_progress = replace(
            self.progress, next_add=index + 1, stage=None, admitted=tuple(admitted)
        )
        self._boundary(
            replace(
                self.progress, next_add=index, stage=MemberStage.DISSOLVED, admitted=tuple(admitted)
            )
        )
        return True  # not reached

    def _accept_removals(self, target: PlayerView, remove: list[str]) -> list[str]:
        """
        Return the players to remove that are in the target's group.

        :param target: The leader or group player.
        :param remove: The players to remove.
        """
        accepted: list[str] = []
        for child_id in remove:
            if child_id in target.group_members:
                accepted.append(child_id)
                continue
            # also accept the removal if the child player itself reports being synced to
            # this parent - handles race conditions where the parent's group_members state
            # is stale/not yet updated
            child = self.snapshot.player(child_id)
            if child and target.player_id == child.synced_to:
                accepted.append(child_id)
        return accepted

    # -- small rules --

    def _linked(self, player: PlayerView, protocol_id: str) -> ProtocolView | None:
        """
        Return an attached audio protocol of a player.

        :param player: The player.
        :param protocol_id: Id of the protocol.
        """
        if protocol_id not in player.audio_protocols:
            return None
        return self.snapshot.attached(player, protocol_id)

    def _grouped_output_protocol(self, player: PlayerView) -> ProtocolView | None:
        """
        Return the first audio protocol of a player that can play and is grouped.

        :param player: The player.
        """
        return first_grouped_protocol(
            protocol
            for protocol_id in player.audio_protocols
            if (protocol := self.snapshot.protocols.get(protocol_id)) is not None
        )

    def _held_target(self) -> PlayerView:
        """Return the player the change is about, held by the change."""
        target = self.snapshot.held_player(self.change.target_id)
        assert target is not None  # the entries pass a player that exists
        return target

    def _player(self, player_id: str) -> PlayerView:
        """
        Return a player the change is about.

        :param player_id: Id of the player.
        """
        player = self.snapshot.player(player_id)
        assert player is not None  # the entries pass a player that exists
        return player

    def _refuse(
        self,
        error: type[MusicAssistantError],
        message: str,
        translation_key: str | None = None,
        translation_args: tuple[str, ...] = (),
    ) -> None:
        """
        End the change with an error.

        :param error: The error type.
        :param message: Its message.
        :param translation_key: Its translation key.
        :param translation_args: Its translation args.
        """
        self.steps.append(
            Refuse(
                error=error,
                message=message,
                translation_key=translation_key,
                translation_args=translation_args,
            )
        )
        raise _Refused

    def _boundary(self, progress: GroupProgress) -> None:
        """
        End the plan here, to plan again from the progress once the steps ran.

        :param progress: Where the change is once the steps ran.
        """
        self.next_progress = progress
        raise _Boundary
