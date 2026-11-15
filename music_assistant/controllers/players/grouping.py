"""
Carry out a group change: the snapshot of the players a plan is made from, and the executor.

These are functions of the players controller rather than its methods, so its internal entries
(``_handle_set_members``, ``_detach_member`` and the like) run them also where a test stands a
mock in for the controller.
"""

from __future__ import annotations

import asyncio
from collections.abc import Container, Mapping
from functools import partial
from typing import TYPE_CHECKING, Any, cast

from music_assistant_models.enums import PlayerFeature
from music_assistant_models.errors import (
    PlayerCommandFailed,
    PlayerUnavailableError,
    UnsupportedFeaturedException,
)

from music_assistant.constants import CONF_PLAYERS, CONF_PREFERRED_OUTPUT_PROTOCOL
from music_assistant.controllers.players.grouping_plan import (
    Detach,
    Failure,
    GroupChange,
    GroupCommand,
    GroupingSnapshot,
    GroupProgress,
    GroupRules,
    GroupStep,
    Handover,
    Join,
    MemberChange,
    MemberLists,
    Note,
    OutputView,
    PlayerView,
    PrepareForPlayback,
    ProtocolView,
    QueueView,
    Refuse,
    Regroup,
    ReleaseSession,
    SetMembers,
    TransferLeadership,
    Ungroup,
    plan_group_change,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Iterator

    from music_assistant_models.errors import MusicAssistantError
    from music_assistant_models.player import OutputProtocol
    from music_assistant_models.player_queue import PlayerQueue

    from music_assistant.models.player import Player
    from music_assistant.models.player_protocol import PlayerProtocol

    from .base import _PlayerControllerBase

type Takeover = tuple[PlayerProtocol, object]

# a member that is released from another leader is waited for this long to report it
RELEASE_UPDATE_TIMEOUT = 5


def grouping_snapshot(
    players: _PlayerControllerBase,
    held: Mapping[str, PlayerProtocol] | None = None,
    held_players: Mapping[str, Player] | None = None,
) -> GroupingSnapshot:
    """
    Return a snapshot of the players and protocols, as a group change is planned on.

    The view of a player or protocol is built when the plan first reads it, and kept for the
    snapshot; a plan made right after, with no await in between, reads the state as it is now.

    :param players: The players controller.
    :param held: The protocols a running change holds.
    :param held_players: The players a running change holds.
    """
    live = players._players
    stored = players.mass.config.get(CONF_PLAYERS, {})
    aliases = {
        legacy_id: player_id
        for legacy_id, player_id in players._legacy_ids.items()
        if legacy_id not in live and legacy_id not in stored
    }
    return GroupingSnapshot(
        players=_Views(dict(live), partial(_player_view, players)),
        protocols=_Views(dict(players._protocols), partial(_protocol_view, players)),
        aliases=aliases,
        held=_Views(dict(held or {}), partial(_protocol_view, players)),
        held_players=_Views(dict(held_players or {}), partial(_player_view, players)),
    )


async def apply_group_change(
    players: _PlayerControllerBase,
    change: GroupChange,
    target: Player | None = None,
    protocol: PlayerProtocol | None = None,
) -> Takeover | None:
    """
    Carry out a group change: plan it, run the steps, plan again where the plan stopped.

    A member change then joins its members, a regroup at a fresh start joins them afresh.

    :param players: The players controller.
    :param change: The change.
    :param target: The player object the change is about, kept for the whole change.
    :param protocol: The protocol object the change is about (LEAVE_PROTOCOL), kept for it.
    :return: The takeover the change opened, for the caller to finish or abort.
    """
    run = _GroupRun(players, change, target)
    if protocol is not None:
        run.held[protocol.protocol_id] = protocol
    progress = GroupProgress()
    while True:
        plan = plan_group_change(
            grouping_snapshot(players, run.held, run.held_players), change, progress
        )
        players.logger.debug("Group plan for %s: %s", change.target_id, plan.steps)
        await run.run(plan.steps)
        if plan.progress is None:
            break
        progress = (
            plan.progress_left_out
            if run.left_out and plan.progress_left_out is not None
            else plan.progress
        )
        run.left_out = False
    if plan.regroup is not None:
        await _regroup(run, plan.regroup)
    elif plan.join is not None:
        try:
            await _join(run, plan.join, new_content=change.new_content)
        except BaseException:
            await run.abort_takeover()
            raise
    return run.takeover


async def route_group_change(players: _PlayerControllerBase, change: GroupChange) -> bool:
    """
    Route a member change before its target is locked.

    :param players: The players controller.
    :param change: The change, of kind ROUTE.
    :return: Whether the change was passed on to another player.
    """
    plan = plan_group_change(grouping_snapshot(players), change)
    players.logger.debug("Group plan for %s: %s", change.target_id, plan.steps)
    await _GroupRun(players, change, None).run(plan.steps)
    return plan.passed_on


async def _join(run: _GroupRun, join: Join, *, new_content: bool) -> None:  # noqa: PLR0915
    """
    Join and drop the members of a leader on the protocols that carry them.

    The members join the protocol that carries the group first and the leader's output follows
    them; the members of the leader's own protocol join last.

    :param run: The run of the change.
    :param join: The members that join and leave.
    :param new_content: The caller publishes new content once the members joined.
    """
    players = run.players
    target_id = join.target_id
    # each decision is taken on the players as they are after the device call before it
    rules = run.read()
    lists = rules.translate(rules.held_player(target_id), join.add, join.remove)
    if lists.protocol_id is not None:
        run.hold(lists.protocol_id)
        if lists.protocol_add and new_content:
            await run.open_takeover(lists.protocol_id)
            rules = run.read()
    leg = rules.protocol_leg(rules.held_player(target_id), lists)
    if isinstance(leg, Refuse):
        raise run.error(leg)
    handover: Handover | None = None
    if leg is not None:
        # This runs before set_members because a member's own stream starts inside that
        # call and the provider resolves the member's volume control as it starts: unless
        # its player already points at this protocol, that resolution picks another
        # interface of the same device (e.g. its cast side) over the one carrying the audio.
        for index, member_protocol_id in enumerate(leg.add):
            if (player_id := rules.member_output(member_protocol_id)) is not None:
                await run.set_output(player_id, member_protocol_id)
                if index + 1 < len(leg.add):
                    rules = run.read()
        await run.protocol(leg.protocol_id).set_members(
            player_ids_to_add=list(leg.add) or None,
            player_ids_to_remove=list(leg.remove) or None,
        )
        rules = run.read()
        run.clear_outputs(rules.leaving_outputs(leg.remove))
        if leg.add:
            handover = rules.handover(rules.held_player(target_id), leg.protocol_id)
            if handover.moves:
                await run.set_output(target_id, leg.protocol_id)
                if handover.note is not None:
                    run.note(handover.note)
                rules = run.read()
            # The members already joined the protocol group, so the native session only has
            # to release them, and stop when it is live. Releasing them first also clears the
            # native group, which keeps a later native playback command from resurrecting it.
            # Both calls take the provider's own lock, so they run one after the other.
            if leg.stranded and (own_id := rules.own_protocol(rules.held_player(target_id))):
                run.hold(own_id)
                await run.protocol(own_id).set_members(player_ids_to_remove=list(leg.stranded))
                if handover.handing_over:
                    await run.protocol(own_id).stop()
                rules = run.read()
    if leg is not None and handover is not None and handover.handing_over:
        old = rules.old_protocol(
            rules.held_player(target_id), handover.previous_output, leg.protocol_id
        )
        removed: list[str] = []
        if old is not None:
            run.hold(old.protocol_id)
            # Stop the specific protocol itself, bypassing group/sync redirect and queue
            # redirect logic. A stopped protocol keeps its group, which would win the output
            # selection of the resume over the protocol that takes over, so its members are
            # removed from it next, as they were before its stop.
            await run.protocol(old.protocol_id).stop()
            rules = run.read()
            if removed := rules.removal(old.protocol_id, [member for member, _ in old.members]):
                await run.protocol(old.protocol_id).set_members(
                    player_ids_to_add=None, player_ids_to_remove=removed
                )
                rules = run.read()
        run.clear_outputs(rules.leaving_outputs(removed))
        old_players = [player_id for _, player_id in old.members] if old is not None else []
        if handover.was_playing:
            await players.cmd_resume(target_id)
        if old_players:
            await players._handle_set_members(run.player(target_id), player_ids_to_add=old_players)
        if handover.was_playing or old_players:
            rules = run.read()
    native = rules.native_leg(rules.held_player(target_id), lists.native_add, lists.native_remove)
    if native is not None:
        await run.protocol(native.protocol_id).set_members(
            player_ids_to_add=list(native.add) or None,
            player_ids_to_remove=list(native.remove) or None,
        )
    if join.stop_after:
        await players._stop_player_or_its_queue(run.player(target_id))


async def _regroup(run: _GroupRun, regroup: Regroup) -> None:
    """
    Take a group off a protocol none of its members needs anymore, and join it afresh.

    :param run: The run of the change.
    :param regroup: The regroup.
    """
    if regroup.removed:
        await run.protocol(regroup.protocol_id).set_members(
            player_ids_to_add=None, player_ids_to_remove=list(regroup.removed)
        )
    rules = run.read()
    run.clear_outputs(rules.leaving_outputs(regroup.removed))
    if rules.active_output(rules.held_player(regroup.leader_id)) == regroup.protocol_id:
        run.clear_outputs([regroup.leader_id])
    await _join(run, Join(target_id=regroup.leader_id, add=regroup.member_ids), new_content=False)


class _GroupRun:
    """The run of one group change: the objects it holds and the takeover it opened."""

    def __init__(
        self, players: _PlayerControllerBase, change: GroupChange, target: Player | None
    ) -> None:
        """
        Initialize the run.

        :param players: The players controller.
        :param change: The change.
        :param target: The player object the change is about.
        """
        self.players = players
        self.held: dict[str, PlayerProtocol] = {}
        self.held_players: dict[str, Player] = {}
        if target is not None:
            self.held_players[change.target_id] = target
        self.takeover: Takeover | None = None
        self.left_out = False

    async def run(self, steps: tuple[GroupStep, ...]) -> None:
        """
        Run the steps of a plan, in order.

        :param steps: The steps.
        """
        for step in steps:
            await self._run_step(step)

    def read(self) -> GroupRules:
        """Return the decisions on the players as they are now."""
        return GroupRules(grouping_snapshot(self.players, self.held, self.held_players), self.note)

    def note(self, note: Note) -> None:
        """
        Write a line for the log.

        :param note: The line.
        """
        self.players.logger.log(note.level, note.message, *note.args)

    def error(self, refusal: Refuse) -> MusicAssistantError:
        """
        Return the error a refusal ends the change with.

        :param refusal: The refusal.
        """
        if refusal.translation_key is not None:
            return refusal.error(
                refusal.message,
                translation_key=refusal.translation_key,
                translation_owner=self.players.translation_owner,
                translation_args=list(refusal.translation_args),
            )
        return refusal.error(refusal.message)

    def hold(self, protocol_id: str) -> None:
        """
        Keep the protocol object of an id for the rest of the change.

        :param protocol_id: Id of the protocol.
        """
        if (protocol := self.players.get_protocol(protocol_id)) is not None:
            self.held.setdefault(protocol_id, protocol)

    def protocol(self, protocol_id: str) -> PlayerProtocol:
        """
        Return the protocol object of an id: the one the change holds, else the attached one.

        :param protocol_id: Id of the protocol.
        """
        if (protocol := self.held.get(protocol_id)) is not None:
            return protocol
        protocol = self.players.get_protocol(protocol_id)
        assert protocol is not None  # a change only names protocols the snapshot holds
        return protocol

    def player(self, player_id: str) -> Player:
        """
        Return the player object of an id: the one the change holds, else the live one.

        :param player_id: Id of the player.
        """
        if (player := self.held_players.get(player_id)) is not None:
            return player
        player = self.players.get_player(player_id)
        assert player is not None  # a change only names players the snapshot holds
        self.held_players[player_id] = player
        return player

    async def set_output(self, player_id: str, output_id: str) -> None:
        """
        Make an output the active one of a player, its level handed over first.

        :param player_id: Id of the player.
        :param output_id: The protocol id of the output.
        """
        await self.players._activate_output_protocol(self.player(player_id), output_id)

    def clear_outputs(self, player_ids: Iterable[str]) -> None:
        """
        Clear the active output of players.

        :param player_ids: Ids of the players.
        """
        for player_id in player_ids:
            self.player(player_id).set_active_output_protocol(None)

    async def open_takeover(self, protocol_id: str) -> None:
        """
        Prepare a protocol group for a new content owner, keeping the token for the caller.

        :param protocol_id: Id of the protocol.
        """
        protocol = self.protocol(protocol_id)
        token = await protocol.on_group_content_takeover()
        if token is not None:
            self.takeover = (protocol, token)

    async def abort_takeover(self) -> None:
        """Abort the takeover the change opened, if any."""
        if self.takeover is not None:
            owner, token = self.takeover
            self.takeover = None
            await owner.on_group_content_takeover_aborted(token)

    async def _run_step(self, step: GroupStep) -> None:
        """
        Run one step.

        :param step: The step.
        """
        players = self.players
        match step:
            case Note():
                self.note(step)
            case SetMembers():
                await self._set_members(step)
            case PrepareForPlayback():
                await players._power_on_for_playback(self.player(step.player_id))
            case MemberChange():
                await self._member_change(step)
            case Detach():
                await self._detach(step)
            case GroupCommand():
                await players.cmd_set_members(step.target_id, **_lists(step.add, step.remove))
            case Ungroup():
                await self._ungroup(step)
            case ReleaseSession():
                await players._release_group_session(step.group_id)
            case TransferLeadership():
                await players._transfer_ad_hoc_leadership(
                    self.player(step.leader_id), list(step.remaining)
                )
            case Refuse():
                raise self.error(step)

    async def _set_members(self, step: SetMembers) -> None:
        """
        Call set_members on a protocol, passing its lists the way the step says.

        :param step: The step.
        """
        protocol = self.protocol(step.protocol_id)
        if step.lists == MemberLists.REMOVE_ONLY:
            await protocol.set_members(player_ids_to_remove=list(step.remove))
        else:
            await protocol.set_members(
                player_ids_to_add=list(step.add), player_ids_to_remove=list(step.remove)
            )

    async def _member_change(self, step: MemberChange) -> None:
        """
        Run a nested member change, failing as the step says.

        :param step: The step.
        """
        call = self.players._handle_set_members(
            self.player(step.target_id), **_lists(step.add, step.remove)
        )
        if step.failure == Failure.RAISE:
            await call
            return
        if step.failure == Failure.DEBUG:
            try:
                await call
            except (
                UnsupportedFeaturedException,
                PlayerCommandFailed,
                PlayerUnavailableError,
            ) as err:
                self.players.logger.debug(step.message, *step.args, err)
            return
        try:
            await call
        except Exception:
            self.players.logger.warning(step.message, *step.args)
            self.left_out = step.failure == Failure.LEAVE_OUT

    async def _detach(self, step: Detach) -> None:
        """
        Run a nested detach of a member from its leader, failing as the step says.

        :param step: The step.
        """
        member = self.player(step.member_id)
        if step.failure == Failure.RAISE and not step.wait_for_update:
            await self.players._detach_member(member)
            return
        try:
            async with self.players.wait_for_player_update(
                member.player_id, timeout=RELEASE_UPDATE_TIMEOUT
            ):
                await self.players._detach_member(member)
        except asyncio.CancelledError:
            raise
        except Exception:
            if step.failure != Failure.WARN:
                raise
            self.players.logger.warning(step.message, *step.args)

    async def _ungroup(self, step: Ungroup) -> None:
        """
        Let a protocol leave its group, failing as the step says.

        :param step: The step.
        """
        protocol = self.protocol(step.protocol_id)
        if step.failure != Failure.UNAVAILABLE:
            await protocol.ungroup()
            return
        try:
            await protocol.ungroup()
        except asyncio.CancelledError:
            raise
        except Exception as err:
            raise PlayerUnavailableError(step.message) from err


def _lists(add: tuple[str, ...] | None, remove: tuple[str, ...] | None) -> dict[str, Any]:
    """
    Return the member lists of a nested change as the keyword arguments it is called with.

    :param add: Players to add, None when not given.
    :param remove: Players to remove, None when not given.
    """
    lists: dict[str, Any] = {}
    if add is not None:
        lists["player_ids_to_add"] = list(add)
    if remove is not None:
        lists["player_ids_to_remove"] = list(remove)
    return lists


def _player_view(players: _PlayerControllerBase, player: Player) -> PlayerView:
    """
    Return what grouping reads of a player.

    :param players: The players controller.
    :param player: The player.
    """
    state = player.state
    own = player.own_protocol
    return PlayerView(
        player_id=player.player_id,
        name=player.name,
        type=player.type,
        listed=state.available and state.enabled,
        iterated=state.available and state.enabled and player.initialized.is_set(),
        available_for_playback=player.available_for_playback,
        powered=state.powered,
        power_control=state.power_control,
        playback_state=state.playback_state,
        features=frozenset(state.supported_features),
        synced_to=state.synced_to,
        active_group=state.active_group,
        group_members=tuple(state.group_members),
        static_group_members=tuple(state.static_group_members),
        can_group_with=frozenset(state.can_group_with),
        playback_owner=players._resolve_playback_owner(player).player_id,
        own_protocol=own.protocol_id if own is not None else None,
        audio_protocols=tuple(protocol.protocol_id for protocol in player.audio_protocols),
        outputs=tuple(_output_view(player, output) for output in player.output_protocols),
        active_output=player.active_output_protocol,
        preferred_output=cast(
            "str | None",
            players.mass.config.get_player_value(player.player_id, CONF_PREFERRED_OUTPUT_PROTOCOL),
        ),
        active_queue=_queue_view(players.get_active_queue(player)),
    )


def _output_view(player: Player, output: OutputProtocol) -> OutputView:
    """
    Return one output of a player as grouping reads it.

    :param player: The player.
    :param output: One of its outputs.
    """
    if output.output_protocol_id == "native":
        protocol_id = own.protocol_id if (own := player.own_protocol) is not None else None
    else:
        protocol_id = (
            output.output_protocol_id
            if output.output_protocol_id in player.attached_protocols
            else None
        )
    return OutputView(
        output_id=output.output_protocol_id,
        protocol_id=protocol_id,
        domain=output.protocol_domain,
        available=output.available,
        is_native=output.is_native,
    )


def _protocol_view(players: _PlayerControllerBase, protocol: PlayerProtocol) -> ProtocolView:
    """
    Return what grouping reads of a protocol.

    :param players: The players controller.
    :param protocol: The protocol.
    """
    features = frozenset(protocol.supported_features)
    native_peers: Container[str] = frozenset()
    if PlayerFeature.SET_MEMBERS in features and _is_own_protocol(players, protocol):
        native_peers = _NativePeers(players, protocol)
    return ProtocolView(
        protocol_id=protocol.protocol_id,
        player_id=protocol.player.player_id if protocol.player is not None else None,
        domain=protocol.provider.domain,
        provider_id=protocol.provider.instance_id,
        provider_name=protocol.provider.manifest.name,
        role=protocol.role,
        features=features,
        available_for_playback=protocol.available_for_playback,
        group_members=tuple(protocol.group_members),
        synced_to=protocol.synced_to,
        can_group_with=frozenset(protocol.can_group_with),
        native_peers=native_peers,
        needs_own_stream=protocol.native_grouping_requires_own_stream,
        prefers_native=protocol.prefer_native_grouping,
    )


def _is_own_protocol(players: _PlayerControllerBase, protocol: PlayerProtocol) -> bool:
    """
    Return whether a protocol is the own protocol of a live player.

    :param players: The players controller.
    :param protocol: The protocol.
    """
    player = protocol.player
    return (
        player is not None
        and players._players.get(player.player_id) is player
        and player.own_protocol is protocol
    )


class _Views[T, V](Mapping[str, V]):
    """The views of live objects by id, each built on its first read and kept."""

    def __init__(self, objects: dict[str, T], build: Callable[[T], V]) -> None:
        """
        Initialize the views.

        :param objects: The objects by id, in their order.
        :param build: Returns the view of an object.
        """
        self._objects = objects
        self._build = build
        self._views: dict[str, V] = {}

    def __getitem__(self, key: str) -> V:
        """Return the view of the object with the id, building it on its first read."""
        if (view := self._views.get(key)) is None:
            view = self._views[key] = self._build(self._objects[key])
        return view

    def __contains__(self, key: object) -> bool:
        """Return whether an object has the id."""
        return key in self._objects

    def __iter__(self) -> Iterator[str]:
        """Iterate over the ids, in their order."""
        return iter(self._objects)

    def __len__(self) -> int:
        """Return the number of objects."""
        return len(self._objects)


class _NativePeers(Container[str]):
    """The own protocols of other players a protocol groups with natively, asked per pair."""

    def __init__(self, players: _PlayerControllerBase, protocol: PlayerProtocol) -> None:
        """
        Initialize the peers of a protocol.

        :param players: The players controller.
        :param protocol: The own protocol whose peers these are.
        """
        self._players = players
        self._protocol = protocol
        self._answers: dict[object, bool] = {}

    def __contains__(self, protocol_id: object) -> bool:
        """Return whether the own protocol with the id is a native peer, asking it once."""
        if (answer := self._answers.get(protocol_id)) is None:
            other = (
                self._players._protocols.get(protocol_id) if isinstance(protocol_id, str) else None
            )
            answer = self._answers[protocol_id] = (
                other is not None
                and other is not self._protocol
                and _is_own_protocol(self._players, other)
                and self._protocol.is_native_group_compatible(other)
            )
        return answer


def _queue_view(queue: PlayerQueue | None) -> QueueView | None:
    """
    Return the queue a player plays, as grouping reads it.

    :param queue: The queue.
    """
    if queue is None:
        return None
    return QueueView(queue_id=queue.queue_id, state=queue.state)
