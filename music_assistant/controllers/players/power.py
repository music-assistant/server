"""
Power Mixin for the Player Controller.

The power commands of a player, a player switched off at its external power control
(powered on for itself, or for the group or leader that takes it along), a power off that
happens outside of Music Assistant and the idle power off.

This module provides the PowerMixin class which is inherited by
PlayerController to add the power commands.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from typing import Any

from music_assistant_models.auth import Scope
from music_assistant_models.constants import (
    PLAYER_CONTROL_FAKE,
    PLAYER_CONTROL_NATIVE,
    PLAYER_CONTROL_NONE,
)
from music_assistant_models.enums import PlaybackState, PlayerType
from music_assistant_models.errors import (
    InsufficientPermissions,
    MusicAssistantError,
    PlayerUnavailableError,
    ResourceBusyError,
    UnsupportedFeaturedException,
)

from music_assistant.constants import (
    ATTR_ANNOUNCEMENT_IN_PROGRESS,
    ATTR_FAKE_POWER,
    ATTR_IDLE_SINCE,
    ATTR_POWERED,
    CONF_AUTO_PLAY,
    CONF_IDLE_POWER_OFF,
)
from music_assistant.controllers.webserver.helpers.auth_middleware import (
    get_current_user,
    has_player_access,
    system_auth_context,
)
from music_assistant.helpers.api import api_command
from music_assistant.helpers.config_entries import PLAYBACK_TARGET_TYPES
from music_assistant.models.player import Player

from .base import _PlayerControllerBase
from .constants import (
    CACHE_CATEGORY_PLAYER_POWER,
    OFFLINE_POWER_ON_TIMEOUT,
    UNGROUP_ON_POWER_OFF_TYPES,
    PlayerLockPurpose,
    SwitchedOffAction,
)
from .helpers import handle_player_command, wait_for_power_on

# How long a player must stay powered off before the queue it was playing is ended.
# Home Assistant reports an entity that is (briefly) unavailable or unknown as off, so an
# external power control can report a power off that comes straight back - which must not
# stop the music. Long enough to sit out a reloading integration or a device missing a
# poll, short enough that a real power off is not left streaming.
EXTERNAL_POWER_OFF_STOP_DELAY = 15


@dataclass
class MemberJoin:
    """
    A switched-off player that is powered on for the player that took it along.

    It lasts until the player joined its owner, and ends before that when the power on or
    the join fails, its owner calls it off or its owner is gone.

    :param owner_id: The group player or leader that took the player along.
    :param added: Whether the player was added to its owner, which adds it again once its
        device is online. Otherwise its owner started to play, and takes it into what it
        plays by then.
    :param online: Resolves to whether the device came online.
    """

    owner_id: str
    added: bool
    online: asyncio.Future[bool]


class PowerMixin(_PlayerControllerBase):
    """Mixin class providing the power commands for PlayerController."""

    @api_command("players/cmd/power", required_scope=Scope.PLAYERS_CONTROL)
    @handle_player_command(switched_off=SwitchedOffAction.RUN, accept_unavailable=True)
    async def cmd_power(self, player_id: str, powered: bool) -> None:
        """
        Send POWER command to given player.

        :param player_id: player_id of the player to handle the command.
        :param powered: bool if player should be powered on or off.
        """
        if powered and (player := self.get_player(player_id)):
            if player.offline_by_power_control:
                # no lock while the switched off device comes online: that takes far longer
                # than other commands wait for the lock, and it has no group to form
                await self._handle_cmd_power(player_id, True)
                return
            if player.type == PlayerType.GROUP and player.available and player.powered is False:
                # the group forms on the members that are on, so the others are switched on
                # before the lock as well
                await self._power_on_group_members(player)
        # Power is serialized with PLAYBACK because powering on a sync/group player
        # forms the group (and powering off dissolves it) - this must not race with
        # play_media / cmd_resume / cmd_set_members on the same player.
        # A power off also detaches this player from its (sync)group, which takes the
        # group's lock - so that one is taken first (see get_group_and_player_lock).
        lock = (
            self.get_player_lock(player_id, PlayerLockPurpose.PLAYBACK)
            if powered
            else self.get_group_and_player_lock(player_id)
        )
        async with lock:
            await self._handle_cmd_power(player_id, powered)

    @contextmanager
    def without_member_power_on(self) -> Iterator[None]:
        """
        Keep the playback that is started inside from powering on the members of a group player.

        For playback that the server continues or restarts by itself, such as the next items
        of a queue that played out or a restart that applies a changed setting. That is no
        command that starts the group, so its members that are switched off at their power
        control stay off.
        """
        task = asyncio.current_task()
        if task is None or task in self._continuing_tasks:
            yield
            return
        self._continuing_tasks.add(task)
        try:
            yield
        finally:
            self._continuing_tasks.discard(task)

    def awaits_member_join(self, owner_id: str, member_id: str) -> bool:
        """
        Return whether a player that was powered on for a group or leader still has to join it.

        For a group that lets such a member join later (see
        PlayerProtocol.on_member_powered_on): it only does so while this holds. A stop, a
        pause or a power off of the owner, or a removal of the member from it, ends it.

        :param owner_id: The group player or leader that took the player along.
        :param member_id: The player that was powered on for it.
        """
        join = self._member_joins.get(member_id)
        return join is not None and join.owner_id == owner_id

    def member_join_ended(self, owner_id: str, member_id: str) -> None:
        """
        Tell that a member a group let join later has joined, or that the group gave it up.

        From then on the member is powered off as idle like any other player. Does nothing
        for a member that does not wait to join the group.

        :param owner_id: The group player that took the player along.
        :param member_id: The player that was powered on for it.
        """
        if self.awaits_member_join(owner_id, member_id):
            del self._member_joins[member_id]

    def _handle_external_power_off(
        self, player: Player, changed_values: dict[str, tuple[Any, Any]]
    ) -> None:
        """End the queue of a player whose power was turned off outside of MA."""
        if (
            changed_values.get(ATTR_POWERED) != (True, False)
            or player.type not in PLAYBACK_TARGET_TYPES
        ):
            return
        if (
            player.synced_to
            or player.active_group
            or (player.group_members and player.type in UNGROUP_ON_POWER_OFF_TYPES)
        ):
            # a player that is a member of a group, or a sync leader handing its
            # leadership on, is detached instead and the group's own power off ends the
            # queue. a group player leads its own members, so its queue is ended here.
            return
        # a device that powers itself off may report its stop in this very update, so
        # judge on the playback state as it was before it - which is also the snapshot
        # an MA power off works from
        prev_playback_state = (
            changed_values["playback_state"][0]
            if "playback_state" in changed_values
            else player.playback_state
        )
        if prev_playback_state not in (PlaybackState.PLAYING, PlaybackState.PAUSED):
            return
        self.mass.call_later(
            EXTERNAL_POWER_OFF_STOP_DELAY,
            self._stop_queue_on_external_power_off,
            player.player_id,
            task_id=f"external_power_off_stop_{player.player_id}",
        )

    async def _stop_queue_on_external_power_off(self, player_id: str) -> None:
        """
        End the queue of a player that is still powered off.

        :param player_id: The player whose power was turned off outside of MA.
        """
        # hold the playback lock across the checks: a power on that overlaps the wait
        # holds it while it resumes the queue, and reading the power state before that
        # completes would stop the playback it just started (the lock is re-entrant per
        # task, so the stop below re-acquiring it is a no-op)
        async with self.get_player_lock(player_id, PlayerLockPurpose.PLAYBACK):
            if not (player := self.get_player(player_id)) or player.powered is not False:
                # the power came back on while we were waiting it out
                return
            # only the player's own queue: get_active_queue resolves to another player's
            # queue as soon as this one is hearing someone else's audio
            active_queue = self.get_active_queue(player)
            if active_queue is None or active_queue.queue_id != player_id:
                return
            # a player gone unavailable along with its power cannot be told to stop,
            # but the queue teardown that matters here runs regardless
            with suppress(PlayerUnavailableError):
                await self.mass.player_queues._handle_stop(player_id)

    def _check_idle_power_off(self, player: Player) -> None:
        """
        Power off a player once it has been idle for its configured idle power off time.

        :param player: The player to check.
        """
        if not self._get_idle_power_off_minutes(player):
            player.extra_data.pop(ATTR_IDLE_SINCE, None)
            return
        player.extra_data.setdefault(ATTR_IDLE_SINCE, time.monotonic())
        if self._is_idle_power_off_due(player):
            # the task id keeps a power off that waits for the player lock from starting twice
            self.mass.create_task(
                self._power_off_idle_player(player.player_id),
                task_id=f"idle_power_off_{player.player_id}",
            )

    def _restart_idle_count(
        self, player: Player, changed_values: dict[str, tuple[Any, Any]]
    ) -> None:
        """
        Start the idle power off count of a player over when its playback or power changed.

        :param player: The player whose state changed.
        :param changed_values: The changed state values.
        """
        # also catches playback that starts and ends between two checks of the poll loop
        if changed_values.keys() & {"playback_state", ATTR_POWERED}:
            player.extra_data.pop(ATTR_IDLE_SINCE, None)

    async def _power_off_idle_player(self, player_id: str) -> None:
        """
        Power off a player that has been idle for its configured idle power off time.

        :param player_id: The player to power off.
        """
        try:
            # a command that holds the player may play on it, which starts the count over.
            # So never run without the lock, and check again once it is ours.
            async with self.get_group_and_player_lock(player_id):
                player = self.get_player(player_id)
                if player is None or not self._is_idle_power_off_due(player):
                    return
                # starts the count over, so a power off that fails is retried a full idle
                # time later
                player.extra_data.pop(ATTR_IDLE_SINCE)
                self.logger.info("Powering off idle player %s", player.name)
                await self._handle_cmd_power(player_id, False)
        except ResourceBusyError:
            self.logger.debug("Player %s is busy, its idle power off is tried again", player_id)

    def _is_idle_power_off_due(self, player: Player) -> bool:
        """
        Return if the player has been idle for its configured idle power off time.

        :param player: The player to check.
        """
        minutes = self._get_idle_power_off_minutes(player)
        idle_since = player.extra_data.get(ATTR_IDLE_SINCE)
        return (
            bool(minutes)
            and idle_since is not None
            and time.monotonic() - idle_since >= minutes * 60
        )

    def _get_idle_power_off_minutes(self, player: Player) -> int:
        """
        Return after how many idle minutes the player is to be powered off.

        Returns 0 while that does not apply: the player has no idle power off configured, is
        not online, powered on and stopped, is about to play an announcement, or was powered
        on to join a group or leader and has not joined it yet.

        :param player: The player to get the idle power off time for.
        """
        minutes = player.config.get_value(CONF_IDLE_POWER_OFF)
        state = player.state
        if (
            not isinstance(minutes, int)
            or minutes <= 0
            or not state.available
            or state.powered is not True
            or state.playback_state != PlaybackState.IDLE
            or player.extra_data.get(ATTR_ANNOUNCEMENT_IN_PROGRESS)
            # reported as powered on and idle while it is still starting up
            or player.offline_by_power_control
            or player.player_id in self._member_joins
        ):
            return 0
        return minutes

    async def _handle_cmd_power(
        self, player_id: str, powered: bool, skip_auto_play: bool = False
    ) -> None:
        """
        Handle player power on/off command.

        Skips permission checks and locking (internal use only).

        :param player_id: The player ID to power on/off.
        :param powered: True to power on, False to power off.
        :param skip_auto_play: If True, skip auto-play on power on.
        """
        # power on is the one command a player takes while it is unavailable
        player = self.get_player(player_id, raise_unavailable=not powered)
        if player is None:
            msg = f"Player {player_id} is not available"
            raise PlayerUnavailableError(msg)
        player_state = player.state

        # a power on that still waits for the device is waited for (power on) or called off
        # (power off), whatever its control reports by now. So is that of the members the
        # player took along: a group that still waits for its first member is off itself
        called_off = not powered and self._cancel_power_on(player)
        if player_state.powered == powered and not (player.power_on_in_progress or called_off):
            self.logger.debug(
                "Ignoring power %s command for player %s: already in state %s",
                "ON" if powered else "OFF",
                player_state.name,
                "ON" if player_state.powered else "OFF",
            )
            return  # nothing to do

        if not powered:
            # the power off the player reports back must not ungroup it a second time: a
            # device that powers itself off when stopped reports it before the power
            # command below even reaches it
            player.set_power_off_issued_at(time.monotonic())

        # ungroup player at power off
        player_was_sync_child = bool(player.synced_to or player.active_group)
        if (
            (player_was_sync_child or player_state.group_members)
            and player.type in UNGROUP_ON_POWER_OFF_TYPES
            and not powered
        ):
            # ungroup player if it is synced (or is a sync leader itself). Only this
            # player leaves: its followers keep their own power state, as leadership
            # transfers to one of them or their group re-forms without it
            await self.cmd_ungroup(player_id)

        # always stop player at power off
        if (
            not powered
            and not player_was_sync_child
            and player_state.playback_state in (PlaybackState.PLAYING, PlaybackState.PAUSED)
        ):
            # wait for the stop command to process and prevent race conditions
            async with self.wait_for_player_update(player_id, timeout=5):
                await self._stop_player_or_its_queue(player)

        # handle actual power command
        if player_state.power_control == PLAYER_CONTROL_NONE:
            self.logger.debug(
                "Player %s does not support power control, ignoring power command",
                player_state.name,
            )
            return
        if player_state.power_control == PLAYER_CONTROL_NATIVE:
            # player supports power command natively: forward to player provider
            if own := player.own_protocol:
                await own.power(powered)
                if powered:
                    await wait_for_power_on(self.logger, player)
        elif player_state.power_control == PLAYER_CONTROL_FAKE:
            # user wants to use fake power control - so we (optimistically) update the state
            # and store the state in the cache
            player.extra_data[ATTR_FAKE_POWER] = powered
            # Group players need to actively form/dissolve their session when the
            # user toggles fake power — otherwise the toggle would only update the
            # cosmetic state without ever capturing or releasing the members.
            if player_state.type == PlayerType.GROUP and (own := player.own_protocol):
                await own.power(powered)
            player.update_state()  # trigger update of the player state
            if player_state.type != PlayerType.GROUP:
                # see _publish_player(): group fake-power is intentionally not persisted
                # because there is no session to restore at boot.
                await self.mass.cache.set(
                    key=player_id,
                    data=powered,
                    provider=self.domain,
                    category=CACHE_CATEGORY_PLAYER_POWER,
                )
        # handle external player control
        elif player_control := self._controls.get(player.power_control):
            control_name = player_control.name
            self.logger.debug("Redirecting power command to PlayerControl %s", control_name)
            if not player_control.supports_power:
                raise UnsupportedFeaturedException(
                    f"Player control {control_name} is not available"
                )
            if powered and player.offline_by_power_control:
                if player.power_on_in_progress:
                    # joins a power on under way: the request that started it runs auto play
                    skip_auto_play = True
                player = await self._power_on_switched_off_player(player_id)
            elif powered:
                assert player_control.power_on is not None  # for type checking
                await player_control.power_on()
                await wait_for_power_on(self.logger, player, player_control)
            else:
                assert player_control.power_off is not None  # for type checking
                await player_control.power_off()
        # always trigger a state update to update the UI
        player.refresh_state()

        # handle 'auto play on power on' feature
        if (
            not skip_auto_play
            and not player_state.active_group
            and not player_state.synced_to
            and powered
            and player.config.get_value(CONF_AUTO_PLAY)
            and player_state.active_source in (None, player_id)
            and not player.extra_data.get(ATTR_ANNOUNCEMENT_IN_PROGRESS)
        ):
            await self.mass.player_queues.resume(player_id)

    async def _power_on_for_playback(self, player: Player, skip_auto_play: bool = False) -> None:
        """
        Power on a player that is about to start playback, if it is not on yet.

        :param player: The player that is about to play.
        :param skip_auto_play: Skip the auto play on power on, as the caller starts playback.
        """
        # a device reports its playback a moment after it started, so the idle power off of
        # the player and the members playing along counts from here, or it could switch off
        # playback that was just started
        for member_id in {player.player_id, *player.group_members}:
            if member := self.get_player(member_id):
                member.extra_data.pop(ATTR_IDLE_SINCE, None)
        if not player.powered and player.power_control != PLAYER_CONTROL_NONE:
            await self._handle_cmd_power(player.player_id, True, skip_auto_play=skip_auto_play)

    async def _power_on_switched_off_player(self, player_id: str) -> Player:
        """
        Power on a player that is switched off at its external power control.

        Returns right away for a player that is not switched off. Otherwise it returns once
        the device is online and the protocols that brought it back finished attaching,
        while the player stays available to clients in the meantime. Takes no lock: waiting
        for the device takes far longer than other commands wait for a player lock, so it
        must run before any lock is taken.

        For a group player its switched-off members are powered on instead (see
        _power_on_group_members).

        :param player_id: The player to power on.
        :raises InsufficientPermissions: When the current user has no access to the player.
        :raises PlayerUnavailableError: When the device does not come online in time, or
            it is switched off again before it does.
        :return: The player.
        """
        player = self.get_player(player_id, True)
        assert player is not None  # for type checking
        if player.type == PlayerType.GROUP:
            await self._power_on_group_members(player)
            return player
        if not player.offline_by_power_control:
            return player
        current_user = get_current_user()
        if current_user and not has_player_access(current_user, player.player_id, player):
            msg = f"{current_user.username} does not have access to player {player.name}"
            raise InsufficientPermissions(msg)
        # publish first: what the player published last may name another control while a
        # change of this setting is still being applied
        player.refresh_state()
        control_id = player.power_control
        player_control = self._controls[control_id]
        # a power on that is already under way is only waited for: switching the control
        # once more could switch a device behind a toggling control off again
        owns_power_on = not player.power_on_in_progress
        if owns_power_on:
            player.set_power_on_in_progress(True)
        try:
            if owns_power_on:
                assert player_control.power_on is not None  # for type checking
                await player_control.power_on()
            self.logger.debug("Waiting for player %s to come online", player.name)
            # a control reports its new state a moment after it was switched, so only an
            # off that follows its on means it was switched off again
            control_reported_on = False
            try:
                async with asyncio.timeout(OFFLINE_POWER_ON_TIMEOUT):
                    while True:
                        # a protocol that is still attaching may not be played to yet
                        if player.available_for_playback and self._attaching.keys().isdisjoint(
                            player.attached_protocols
                        ):
                            # a device can come online before its control reports on, while
                            # what follows (e.g. an announcement) reads the power state
                            if (control := self._controls.get(control_id)) is not None:
                                await wait_for_power_on(self.logger, player, control)
                            return player
                        if not player.power_on_in_progress or (
                            self._players.get(player_id) is not player
                        ):
                            # a power off called it off, or the power on waited for has ended
                            msg = f"Player {player.name} is switched off"
                            raise PlayerUnavailableError(msg)
                        if (control := self._controls.get(control_id)) is not None:
                            if control.power_state:
                                control_reported_on = True
                            elif control_reported_on:
                                msg = f"Player {player.name} is switched off"
                                raise PlayerUnavailableError(msg)
                        await asyncio.sleep(0.5)
            except TimeoutError:
                msg = f"Player {player.name} did not come online after powering it on"
                raise PlayerUnavailableError(msg) from None
        finally:
            if owns_power_on:
                player.set_power_on_in_progress(False)
                if refreshed := self.get_player(player_id):
                    refreshed.refresh_state()

    async def _power_on_group_members(self, group: Player) -> None:
        """
        Power on the members of a group player that are switched off at their power control.

        Each of them joins the group once its device is online. Returns right away while a
        member of the group can play, and for a group without such a member; otherwise it
        returns once the first device is online. Takes no lock, as waiting for a device takes
        far longer than other commands wait for a player lock. Does nothing for playback the
        server continues by itself (see without_member_power_on).

        :param group: The group player that is about to play.
        :raises PlayerUnavailableError: When no member can play and none comes online.
        """
        if asyncio.current_task() in self._continuing_tasks or group.extra_data.get(
            ATTR_ANNOUNCEMENT_IN_PROGRESS
        ):
            # playback the server continues by itself plays on the members that are on, and
            # so do an announcement to a group and the playback it restores afterwards
            return
        members = [
            member
            for member_id in dict.fromkeys((*group.group_members, *group.static_group_members))
            if (member := self.get_player(member_id)) is not None and member.available
        ]
        came_online = [
            self._power_on_member(group, member)
            for member in members
            if member.offline_by_power_control
        ]
        if not came_online or any(member.available_for_playback for member in members):
            return
        for online in asyncio.as_completed(came_online):
            if await online:
                return
        msg = f"None of the members of {group.name} came online after powering them on"
        raise PlayerUnavailableError(msg)

    def _power_on_member(
        self, owner: Player, member: Player, added: bool = False
    ) -> asyncio.Future[bool]:
        """
        Power on a switched-off player for the player that takes it along, which it then joins.

        Returns right away: the power on runs in the background and holds no lock. A player
        that is powered on for another owner already joins this one instead.

        :param owner: The group player or leader that takes the player along.
        :param member: The player, which is switched off at its power control.
        :param added: Whether the player is added to its owner, which adds it again once
            its device is online. Otherwise its owner is about to play, and takes it into
            what it plays by then.
        :return: A future that resolves to whether the device came online.
        """
        join = self._member_joins.get(member.player_id)
        if join is not None and not join.online.done():
            join.owner_id, join.added = owner.player_id, added
            return join.online
        join = MemberJoin(owner.player_id, added, asyncio.get_running_loop().create_future())
        self._member_joins[member.player_id] = join
        self.logger.debug("Powering on player %s for %s", member.name, owner.name)
        self.mass.create_task(self._join_once_online(member.player_id, join))
        return join.online

    async def _join_once_online(self, member_id: str, join: MemberJoin) -> None:
        """
        Power on a switched-off player and let it join the player that took it along.

        :param member_id: The player to power on.
        :param join: Its pending join.
        """
        joins_later = False
        try:
            # the user's access was checked on the player that takes this one along
            with system_auth_context():
                try:
                    member = await self._power_on_switched_off_player(member_id)
                except Exception as err:
                    join.online.set_result(False)
                    owner = self.get_player(join.owner_id)
                    owner_name = owner.name if owner else join.owner_id
                    if isinstance(err, PlayerUnavailableError):
                        self.logger.log(
                            # a power on that its owner called off is no failure
                            logging.WARNING
                            if self._member_joins.get(member_id) is join
                            else logging.DEBUG,
                            "%s, it is left out of %s",
                            err,
                            owner_name,
                        )
                    else:
                        # its power control could not switch it on
                        player = self.get_player(member_id)
                        self.logger.warning(
                            "Player %s could not be powered on, it is left out of %s: %s",
                            player.name if player else member_id,
                            owner_name,
                            err,
                        )
                    return
                # a play that waits for its first member does not start once the power on
                # was called off, also when the device is online by then
                join.online.set_result(self._member_joins.get(member_id) is join)
                joins_later = await self._join_owner(member, join)
        finally:
            # a join its group put off lasts until the group reports its end
            if not joins_later and self._member_joins.get(member_id) is join:
                del self._member_joins[member_id]
            if not join.online.done():
                join.online.set_result(False)

    async def _join_owner(self, member: Player, join: MemberJoin) -> bool:
        """
        Let a player whose device came online join the player that took it along.

        :param member: The player that was powered on.
        :param join: Its pending join.
        :return: Whether its group lets it join later (see
            PlayerProtocol.on_member_powered_on).
        """
        if (owner := self.get_player(join.owner_id)) is None:
            return False
        # what the owner and its members can group with includes the player again
        for player_id in dict.fromkeys((owner.player_id, *owner.group_members)):
            if player := self.get_player(player_id):
                player.refresh_state()
        try:
            # the lock that a member change of the owner runs under; for a group player
            # that is its own lock
            async with self.get_group_and_player_lock(owner.player_id):
                if self._member_joins.get(member.player_id) is not join:
                    # the owner was stopped or paused, or the player was removed from it,
                    # while this waited for the lock
                    return False
                if join.added:
                    await self.cmd_set_members(
                        owner.player_id, player_ids_to_add=[member.player_id]
                    )
                elif own := owner.own_protocol:
                    return await own.on_member_powered_on(member) is True
        except MusicAssistantError as err:
            self.logger.warning("Player %s could not join %s: %s", member.name, owner.name, err)
        return False

    def _cancel_power_on(self, player: Player) -> bool:
        """
        Call off a power on that still waits for a device to come online.

        That of the player itself, and those of the players it took along, which then stay
        out of it. The requests waiting for such a power on fail as switched off. The power
        controls are left as they are.

        :param player: The player whose power on, and that of its members, to call off.
        :return: Whether a power on of the player itself was under way.
        """
        called_off = player.power_on_in_progress
        if called_off:
            player.set_power_on_in_progress(False)
            self.logger.debug("Called off the power on of player %s", player.name)
        for member_id in list(self._member_joins):
            self._cancel_member_join(player, member_id)
        return called_off

    def _cancel_member_join(self, owner: Player, member_id: str) -> None:
        """
        Call off the power on of a player that its owner took along, which then stays out of it.

        Does nothing for a player that the owner did not take along. The power control is
        left as it is.

        :param owner: The group player or leader that took the player along.
        :param member_id: The player that was powered on for it.
        """
        join = self._member_joins.get(member_id)
        if join is None or join.owner_id != owner.player_id:
            return
        del self._member_joins[member_id]
        if member := self.get_player(member_id):
            self._cancel_power_on(member)
