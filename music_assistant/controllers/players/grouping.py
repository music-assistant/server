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
    GroupStep,
    Hold,
    MemberChange,
    MemberLists,
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
    plan_group_change,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from music_assistant_models.player import OutputProtocol
    from music_assistant_models.player_queue import PlayerQueue

    from music_assistant.models.player import Player
    from music_assistant.models.player_protocol import PlayerProtocol

    from .controller import PlayerController

type Takeover = tuple[PlayerProtocol, object]

# a member that is released from another leader is waited for this long to report it
RELEASE_UPDATE_TIMEOUT = 5


def grouping_snapshot(
    players: PlayerController,
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
    players: PlayerController,
    change: GroupChange,
    target: Player | None = None,
    protocol: PlayerProtocol | None = None,
) -> Takeover | None:
    """
    Carry out a group change: plan it, run the steps, plan again where the plan stopped.

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
            return run.takeover
        progress = (
            plan.progress_left_out
            if run.left_out and plan.progress_left_out is not None
            else plan.progress
        )
        run.left_out = False


async def route_group_change(players: PlayerController, change: GroupChange) -> bool:
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


class _GroupRun:
    """The run of one group change: the objects it holds and the takeover it opened."""

    def __init__(
        self, players: PlayerController, change: GroupChange, target: Player | None
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
            try:
                await self._run_step(step)
            except BaseException:
                if self.takeover is not None:
                    owner, token = self.takeover
                    self.takeover = None
                    await owner.on_group_content_takeover_aborted(token)
                raise

    async def _run_step(self, step: GroupStep) -> None:
        """
        Run one step.

        :param step: The step.
        """
        players = self.players
        match step:
            case Note():
                players.logger.log(step.level, step.message, *step.args)
            case Hold():
                if (protocol := players.get_protocol(step.protocol_id)) is not None:
                    self.held.setdefault(step.protocol_id, protocol)
            case SetMembers():
                await self._set_members(step)
            case SetOutput():
                await self._set_output(step)
            case PrepareForPlayback():
                await players._power_on_for_playback(self._player(step.player_id))
            case MemberChange():
                await self._member_change(step)
            case Detach():
                await self._detach(step)
            case GroupCommand():
                await players.cmd_set_members(step.target_id, **_lists(step.add, step.remove))
            case OpenTakeover():
                protocol = self._protocol(step.protocol_id)
                token = await protocol.on_group_content_takeover()
                if token is not None:
                    self.takeover = (protocol, token)
            case StopProtocol():
                await self._protocol(step.protocol_id).stop()
            case Ungroup():
                await self._ungroup(step)
            case ReleaseSession():
                await players._release_group_session(step.group_id)
            case StopLeader():
                await players._stop_player_or_its_queue(self._player(step.player_id))
            case Resume():
                await players.cmd_resume(step.player_id)
            case TransferLeadership():
                await players._transfer_ad_hoc_leadership(
                    self._player(step.leader_id), list(step.remaining)
                )
            case Refuse():
                if step.translation_key is not None:
                    raise step.error(
                        step.message,
                        translation_key=step.translation_key,
                        translation_owner=players.translation_owner,
                        translation_args=list(step.translation_args),
                    )
                raise step.error(step.message)

    async def _set_members(self, step: SetMembers) -> None:
        """
        Call set_members on a protocol, passing its lists the way the step says.

        :param step: The step.
        """
        protocol = self._protocol(step.protocol_id)
        if step.lists == MemberLists.REMOVE_ONLY:
            await protocol.set_members(player_ids_to_remove=list(step.remove))
        elif step.lists == MemberLists.AS_THEY_ARE:
            await protocol.set_members(
                player_ids_to_add=list(step.add), player_ids_to_remove=list(step.remove)
            )
        else:
            await protocol.set_members(
                player_ids_to_add=list(step.add) or None,
                player_ids_to_remove=list(step.remove) or None,
            )

    async def _set_output(self, step: SetOutput) -> None:
        """
        Activate or clear the output of a player.

        :param step: The step.
        """
        player = self._player(step.player_id)
        if step.output_id is None:
            player.set_active_output_protocol(None)
            return
        await self.players._activate_output_protocol(player, step.output_id)

    async def _member_change(self, step: MemberChange) -> None:
        """
        Run a nested member change, failing as the step says.

        :param step: The step.
        """
        call = self.players._handle_set_members(
            self._player(step.target_id), **_lists(step.add, step.remove)
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
        member = self._player(step.member_id)
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
        protocol = self._protocol(step.protocol_id)
        if step.failure != Failure.UNAVAILABLE:
            await protocol.ungroup()
            return
        try:
            await protocol.ungroup()
        except asyncio.CancelledError:
            raise
        except Exception as err:
            raise PlayerUnavailableError(step.message) from err

    def _protocol(self, protocol_id: str) -> PlayerProtocol:
        """
        Return the protocol object of an id: the one the change holds, else the attached one.

        :param protocol_id: Id of the protocol.
        """
        if (protocol := self.held.get(protocol_id)) is not None:
            return protocol
        protocol = self.players.get_protocol(protocol_id)
        assert protocol is not None  # a plan only names protocols the snapshot holds
        return protocol

    def _player(self, player_id: str) -> Player:
        """
        Return the player object of an id: the one the change holds, else the live one.

        :param player_id: Id of the player.
        """
        if (player := self.held_players.get(player_id)) is not None:
            return player
        player = self.players.get_player(player_id)
        assert player is not None  # a plan only names players the snapshot holds
        self.held_players[player_id] = player
        return player


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


def _player_view(players: PlayerController, player: Player) -> PlayerView:
    """
    Return what grouping reads of a player.

    :param players: The players controller.
    :param player: The player.
    """
    state = player.state
    own = player.own_protocol
    return PlayerView(
        player_id=player.player_id,
        name=player.display_name,
        state_name=state.name,
        type=player.type,
        listed=state.available and state.enabled,
        iterated=state.available and state.enabled and player.initialized.is_set(),
        available=player.available,
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
            output.output_protocol_id if output.output_protocol_id in player.protocols else None
        )
    return OutputView(
        output_id=output.output_protocol_id,
        protocol_id=protocol_id,
        domain=output.protocol_domain,
        available=output.available,
        is_native=output.is_native,
    )


def _protocol_view(players: PlayerController, protocol: PlayerProtocol) -> ProtocolView:
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


def _is_own_protocol(players: PlayerController, protocol: PlayerProtocol) -> bool:
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

    def __init__(self, players: PlayerController, protocol: PlayerProtocol) -> None:
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
