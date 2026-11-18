"""
Power Mixin for the Player Controller.

The power commands of a player, a player whose external power control cuts its power
(powered on for itself, or for the group or leader that is about to play on it, and waited
for while it starts up), a power off that happens outside of Music Assistant and the idle
power off.

This module provides the PowerMixin class which is inherited by
PlayerController to add the power commands.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Iterable, Iterator
from contextlib import contextmanager, suppress
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any

from music_assistant_models.auth import Scope
from music_assistant_models.constants import (
    PLAYER_CONTROL_FAKE,
    PLAYER_CONTROL_NATIVE,
    PLAYER_CONTROL_NONE,
)
from music_assistant_models.enums import PlaybackState, PlayerType
from music_assistant_models.errors import (
    InsufficientPermissions,
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
    CONF_POWER_ON_STARTUP_TIME,
    DEFAULT_POWER_ON_STARTUP_TIME,
    POWER_ON_STARTUP_TIME_RANGE,
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
    POWER_RESTORE_TIMEOUT,
    UNGROUP_ON_POWER_OFF_TYPES,
    PlayerLockPurpose,
    SwitchedOffAction,
)
from .helpers import handle_player_command, wait_for_power_on

if TYPE_CHECKING:
    from music_assistant_models.player_control import PlayerControl

    from music_assistant.models.player_protocol import PlayerProtocol

# How long a player must stay powered off before the queue it was playing is ended, and
# before its power counts as cut at a power control that cuts it.
# Home Assistant reports an entity that is (briefly) unavailable or unknown as off, so an
# external power control can report a power off that comes straight back - which must not
# stop the music. Long enough to sit out a reloading integration or a device missing a
# poll, short enough that a real power off is not left streaming.
EXTERNAL_POWER_OFF_STOP_DELAY = 15

# Set while the server continues playback by itself (see without_member_power_on).
_CONTINUES_PLAYBACK: ContextVar[bool] = ContextVar("continues_playback", default=False)


class _PowerOnCalledOff(PlayerUnavailableError):
    """A power on that was called off, or whose player was switched off again, meanwhile."""


class _PowerOnWait:
    """The commands that wait for a player to start up after its power was switched on."""

    __slots__ = ("called_off", "commands", "over")

    def __init__(self) -> None:
        """Initialize a wait that is not over."""
        # set once the player started up, or the wait was called off
        self.over = asyncio.Event()
        self.called_off = False
        # how many commands wait
        self.commands = 0


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
        player = self.get_player(player_id)
        if player is not None and not powered:
            # a group reads as off itself while its start waits for its switched-off members
            self._cancel_member_power_ons(player, self._resolve_playback_owner(player))
        if player is not None and powered:
            if player.offline_by_power_control:
                # no lock while the switched off device starts up: that takes far longer
                # than other commands wait for the lock, and it has no group to form
                await self._handle_cmd_power(player_id, True)
                return
            if player.type == PlayerType.GROUP and player.available and player.powered is False:
                # the group forms once its switched-off members started up, which is waited
                # for before the lock as well
                await self._power_on_members(player)
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
        control stay off and nothing waits for them.
        """
        token = _CONTINUES_PLAYBACK.set(True)
        try:
            yield
        finally:
            _CONTINUES_PLAYBACK.reset(token)

    def get_power_on_wait(self, player_id: str) -> int:
        """
        Return how long (in seconds) a command to a player can wait for its power to come on.

        That is the start-up time of a player whose power control cuts its power, with the
        time its protocols get to connect after it, and 0 for any other player.

        :param player_id: The player the command goes to.
        """
        player = self.get_player(player_id)
        if player is None or not player.power_control_cuts_power:
            return 0
        return self._get_startup_time(player) + POWER_RESTORE_TIMEOUT

    def _handle_external_power_on(self, player: Player) -> None:
        """
        Let the start-up time of a player run when its power came on outside of MA.

        Only once its power counted as cut: a power control that read off for a moment
        switched nothing. Call this when the power control of the player reports, before
        the player publishes what it reports.

        :param player: The player whose power control reports.
        """
        control = self._controls.get(player.power_control)
        if control is None or not control.power_state or player.powered is not False:
            return
        # a control that reads off since before anyone looked counts as a cut too
        confirmed = self._power_cuts.pop(player.player_id, True)
        if confirmed and player.power_control_cuts_power:
            self._begin_startup(player)

    def _handle_external_power_off(
        self, player: Player, changed_values: dict[str, tuple[Any, Any]]
    ) -> None:
        """
        Follow up on a player whose power was turned off outside of MA.

        Ends the queue it played, and takes its power as cut at a power control that cuts
        it, both once it has stayed off for a while.
        """
        if (
            changed_values.get(ATTR_POWERED) != (True, False)
            or player.type not in PLAYBACK_TARGET_TYPES
        ):
            return
        self._schedule_power_cut(player)
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
        # a start of the player that waits for players to start up is not called off by
        # this stop: it switches the power on or fails, and then this looks again
        while (player := self.get_player(player_id)) and self._has_waiting_start(player):
            await asyncio.sleep(1)
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

    def _schedule_power_cut(self, player: Player) -> None:
        """
        Take the power of a player as cut once its power control has read off for a while.

        Only for a power control that cuts the power of the player. A player that was
        still starting up has its power cut at once.

        :param player: The player whose power control reported off.
        """
        player_id = player.player_id
        if not player.power_control_cuts_power or player_id in self._power_cuts:
            # also a cut that the power command that caused it took as cut already
            return
        if player.startup_ends_at is not None:
            # nothing of a device that did not start up yet is left to wait for
            self._confirm_power_cut(player)
            return
        self._power_cuts[player_id] = False
        self.mass.call_later(
            EXTERNAL_POWER_OFF_STOP_DELAY,
            self._confirm_external_power_cut,
            player_id,
            task_id=f"power_cut_{player_id}",
        )

    def _confirm_external_power_cut(self, player_id: str) -> None:
        """
        Take the power of a player as cut, unless its power control came on again meanwhile.

        Neither when that control no longer cuts the power of the player: the setting or
        the power control of the player changed, or the control is gone.

        :param player_id: The player whose power control reported off a while ago.
        """
        player = self._players.get(player_id)
        if player is None or self._power_cuts.get(player_id) is not False:
            return
        if player.offline_by_power_control:
            self._confirm_power_cut(player)
        else:
            del self._power_cuts[player_id]

    def _confirm_power_cut(self, player: Player) -> None:
        """
        Take the power of a player as cut at its power control, and tell its protocols.

        Ends its start-up time: the commands that wait for it fail as switched off.

        :param player: The player whose power control is off.
        """
        self._end_startup(player, called_off=True)
        if self._power_cuts.get(player.player_id):
            return
        self._power_cuts[player.player_id] = True
        self.logger.debug("The power of player %s is cut", player.name)
        self.mass.create_task(self._tell_protocols(player, power_is_back=False))

    def _drop_power_state(self, player: Player, *, release: bool = False) -> None:
        """
        Forget what is kept about the power of a player.

        For a player that goes away, or whose power control or the setting that says it
        cuts the power of the player changed or went away: what was kept belongs to the
        control it had. Its start-up time ends, and the commands that wait for it fail as
        switched off.

        :param player: The player.
        :param release: Whether the player stays. Its protocols are then told that the power
            is back when they were told that it was cut, or still wait to be told: nothing
            else tells them any more, so they reach the device by themselves again.
        """
        told = self._power_cuts.pop(player.player_id, None) or player.startup_ends_at is not None
        self._end_startup(player, called_off=True)
        self.mass.cancel_timer(f"power_cut_{player.player_id}")
        if release and told and not self.mass.closing:
            self.mass.create_task(self._tell_protocols(player, power_is_back=True))

    def _check_idle_power_off(self, player: Player) -> None:
        """
        Power off a player once it has been idle for its configured idle power off time.

        :param player: The player to check.
        """
        if not self._get_idle_power_off_minutes(player) or self._is_powered_on_for_a_start(player):
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

    @staticmethod
    def _get_idle_power_off_minutes(player: Player) -> int:
        """
        Return after how many idle minutes the player is to be powered off.

        Returns 0 while that does not apply: the player has no idle power off configured, is
        not online, powered on and stopped, or is about to play an announcement.

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

        # a player that starts up is waited for (power on) or switched off (power off)
        # below, whatever its control reports by now
        if player_state.powered == powered and player.startup_ends_at is None:
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
                if player.player_id in self._power_on_waits:
                    # joins a power on under way: the request that started it runs auto play
                    skip_auto_play = True
                player = await self._power_on_switched_off_player(player_id)
            elif powered:
                assert player_control.power_on is not None  # for type checking
                await player_control.power_on()
                await wait_for_power_on(self.logger, player, player_control)
            else:
                await self._power_off_at_control(player, player_control)
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

    async def _power_off_at_control(self, player: Player, player_control: PlayerControl) -> None:
        """
        Switch a player off at its external power control.

        :param player: The player to switch off.
        :param player_control: Its power control.
        """
        # calls off a power on that still waits for the player to start up
        self._cancel_power_on(player)
        assert player_control.power_off is not None  # for type checking
        await player_control.power_off()
        if player.power_control_cuts_power:
            # no need to wait and see whether the control comes back on
            self._confirm_power_cut(player)

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
        Power on a player that is off, or starting up, at a power control that cuts its power.

        Returns right away for any other player. Otherwise it switches the control on,
        unless the player starts up already, and returns once the start-up time of the
        player is over, while the player stays available to clients in the meantime. Takes
        no lock: a start-up takes far longer than other commands wait for a player lock, so
        it must run before any lock is taken.

        :param player_id: The player to power on.
        :raises InsufficientPermissions: When the current user has no access to the player.
        :raises PlayerUnavailableError: When the power on is called off or the player is
            switched off again before it started up, or it can not be played to once it did.
        :return: The player.
        """
        player = self.get_player(player_id, True)
        assert player is not None  # for type checking
        switched_off = player.offline_by_power_control
        if not switched_off:
            return player
        player_id = player.player_id
        current_user = get_current_user()
        if current_user and not has_player_access(current_user, player.player_id, player):
            msg = f"{current_user.username} does not have access to player {player.name}"
            raise InsufficientPermissions(msg)
        # publish first: what the player published last may name another control while a
        # change of this setting is still being applied
        player.refresh_state()
        player_control = self._controls[player.power_control]
        if (wait := self._power_on_waits.get(player_id)) is None:
            wait = self._power_on_waits[player_id] = _PowerOnWait()
        wait.commands += 1
        try:
            # a player that starts up already is only waited for: switching its control
            # once more could switch a device behind a toggling control off again
            if player.startup_ends_at is None:
                # the start-up time runs from here, so the control reporting on changes
                # nothing
                power_cut = self._power_cuts.get(player_id)
                self._begin_startup(player)
                try:
                    assert player_control.power_on is not None  # for type checking
                    await player_control.power_on()
                except BaseException:
                    control = self._controls.get(player_control.id)
                    if control is not None and not control.power_state:
                        # not switched on: the power is cut as it was, unless a newer
                        # cut was recorded meanwhile. A control that reads on did switch,
                        # so the start-up time runs on and only this command ends
                        self._end_startup(player, called_off=True)
                        if power_cut is not None:
                            self._power_cuts.setdefault(player_id, power_cut)
                    raise
            self.logger.debug("Waiting for player %s to start up", player.name)
            await wait.over.wait()
        finally:
            wait.commands -= 1
            if not wait.commands and self._power_on_waits.get(player_id) is wait:
                # the last command that waited was cancelled
                del self._power_on_waits[player_id]
        if wait.called_off or player.offline_by_power_control:
            control = self._controls.get(player.power_control)
            if control is None or control.power_state:
                # called off while its control reads on, such as by a stop
                msg = f"The power on of player {player.name} was called off"
                raise _PowerOnCalledOff(
                    msg,
                    translation_key="player_stopped_during_startup",
                    translation_owner=self.translation_owner,
                    translation_args=[player.name],
                )
            msg = f"Player {player.name} is switched off"
            raise _PowerOnCalledOff(
                msg,
                translation_key="player_switched_off",
                translation_owner=self.translation_owner,
                translation_args=[player.name],
            )
        # a protocol that is still attaching may not be played to yet
        if not player.available_for_playback or self._is_attaching(player):
            seconds = self._get_startup_time(player)
            msg = f"Player {player.name} could not be reached {seconds}s after it was switched on"
            raise PlayerUnavailableError(
                msg,
                translation_key="player_not_started_up",
                translation_owner=self.translation_owner,
                translation_args=[player.name, seconds],
            )
        return player

    def _begin_startup(self, player: Player) -> None:
        """
        Let the start-up time of a player run from now, unless it runs already.

        :param player: The player whose power control is switched on.
        """
        if player.startup_ends_at is not None:
            return
        self._power_cuts.pop(player.player_id, None)
        seconds = self._get_startup_time(player)
        player.set_startup_ends_at(asyncio.get_running_loop().time() + seconds)
        self.mass.call_later(
            seconds,
            self._finish_startup,
            player.player_id,
            task_id=f"power_on_startup_{player.player_id}",
        )

    async def _finish_startup(self, player_id: str) -> None:
        """
        End the start-up time of a player that is over: the commands that wait for it go on.

        Its protocols are told that the power is back first, and get POWER_RESTORE_TIMEOUT
        to connect.

        :param player_id: The player whose start-up time is over.
        """
        if (player := self._players.get(player_id)) is None or player.startup_ends_at is None:
            return
        deadline = asyncio.get_running_loop().time() + POWER_RESTORE_TIMEOUT
        await self._tell_protocols(player, power_is_back=True)
        with suppress(TimeoutError):
            async with asyncio.timeout_at(deadline):
                while self._is_attaching(player):
                    await asyncio.sleep(0.1)
        self._end_startup(player, called_off=False)
        player.refresh_state()

    async def _tell_protocols(self, player: Player, *, power_is_back: bool) -> None:
        """
        Tell every attached protocol of a player that its power was cut, or is back.

        All at once. Returns once each of them handled it, failed or took longer than
        POWER_RESTORE_TIMEOUT.

        :param player: The player whose power control cuts its power.
        :param power_is_back: Whether the power is back and the player started up, else
            the power was cut.
        """

        async def _tell(protocol: PlayerProtocol) -> None:
            try:
                async with asyncio.timeout(POWER_RESTORE_TIMEOUT):
                    await (
                        protocol.on_power_restored() if power_is_back else protocol.on_power_cut()
                    )
            except Exception as err:
                # deliberately broad: this is a raw provider call, which changes nothing here
                self.logger.debug(
                    "Protocol %s of player %s did not handle that its power %s: %r",
                    protocol.protocol_id,
                    player.name,
                    "is back" if power_is_back else "was cut",
                    err,
                )

        await asyncio.gather(*(_tell(protocol) for protocol in player.attached_protocols.values()))

    def _end_startup(self, player: Player, *, called_off: bool) -> None:
        """
        End the start-up time of a player, and the wait of the commands for it.

        :param player: The player.
        :param called_off: Whether the commands that wait fail as switched off, for a
            player whose power is cut again or that goes away.
        """
        task_id = f"power_on_startup_{player.player_id}"
        self.mass.cancel_timer(task_id)
        if called_off:
            # what runs once the start-up time is over
            self.mass.cancel_task(task_id)
        if player.startup_ends_at is not None:
            player.set_startup_ends_at(None)
        if (wait := self._power_on_waits.pop(player.player_id, None)) is not None:
            wait.called_off = called_off
            wait.over.set()

    def _is_attaching(self, player: Player) -> bool:
        """
        Return whether a protocol of a player is still attaching, so it may not be played to yet.

        :param player: The player.
        """
        return not self._attaching.keys().isdisjoint(player.attached_protocols)

    def _get_startup_time(self, player: Player) -> int:
        """
        Return the start-up time (in seconds) of a player whose power control cuts its power.

        :param player: The player.
        """
        seconds = self.mass.config.get_player_value(player.player_id, CONF_POWER_ON_STARTUP_TIME)
        if not isinstance(seconds, int):
            return DEFAULT_POWER_ON_STARTUP_TIME
        # the range is only enforced by the frontend, an API client can store any value
        shortest, longest = POWER_ON_STARTUP_TIME_RANGE
        return min(max(seconds, shortest), longest)

    async def _power_on_members(
        self, owner: Player, member_ids: Iterable[str] | None = None
    ) -> set[str]:
        """
        Power on the switched-off players that a group player or leader is about to play on.

        Powers on the given players that are off, or starting up, at a power control that
        cuts their power and waits until they started up, all at once. A player that can
        not be powered on or played to is logged and left to the caller. Takes no lock: a
        start-up takes far longer than other commands wait for a player lock, so this must
        run before any lock is taken. Does nothing in a task that holds the lock of the
        owner already, and for playback the server continues by itself (see
        without_member_power_on).

        :param owner: The group player that is about to play, or the group player or leader
            that players are added to.
        :param member_ids: The players to power on. The members of the owner when omitted.
        :raises InsufficientPermissions: When the current user has no access to the owner.
        :raises PlayerUnavailableError: When the power on is called off meanwhile (see
            _cancel_member_power_ons).
        :return: The ids of the players that can not be played to.
        """
        if _CONTINUES_PLAYBACK.get() or self.holds_player_lock(owner.player_id):
            return set()
        owner_members = (*owner.group_members, *owner.static_group_members)
        members = self._members_to_power_on(owner, member_ids)
        if not members:
            return set()
        current_user = get_current_user()
        if current_user and not has_player_access(current_user, owner.player_id, owner):
            msg = f"{current_user.username} does not have access to player {owner.name}"
            raise InsufficientPermissions(msg)
        power_ons = asyncio.gather(*(self._power_on_member(owner, member) for member in members))
        self._member_power_ons[power_ons] = (
            owner.player_id,
            frozenset(member.player_id for member in members),
        )
        try:
            started_up = await power_ons
        except asyncio.CancelledError:
            if (task := asyncio.current_task()) is not None and task.cancelling():
                raise
            # only the wait was cancelled, which is how it is called off
            msg = f"The power on of the members of {owner.name} was called off"
            raise _PowerOnCalledOff(
                msg,
                translation_key="player_stopped_during_startup",
                translation_owner=self.translation_owner,
                translation_args=[owner.name],
            ) from None
        finally:
            del self._member_power_ons[power_ons]
        # what the members of the owner can group with includes these players again, and
        # with that what the owner can
        for player_id in dict.fromkeys((*owner_members, owner.player_id)):
            if (player := self.get_player(player_id)) is not None:
                player.refresh_state()
        return {
            member.player_id
            for member, started in zip(members, started_up, strict=True)
            if not started
        }

    async def _power_on_member(self, owner: Player, member: Player) -> bool:
        """
        Power on a switched-off player for the player that is about to play on it.

        :param owner: The group player or leader that is about to play on the player.
        :param member: The player, which is off or starting up at its power control.
        :return: Whether it started up and can be played to.
        """
        try:
            # the user's access was checked on the owner
            with system_auth_context():
                await self._power_on_switched_off_player(member.player_id)
        except Exception as err:
            # deliberately broad: switching a power control is a raw provider call
            self.logger.log(
                logging.DEBUG if isinstance(err, _PowerOnCalledOff) else logging.WARNING,
                "Player %s is left out of %s: %s",
                member.name,
                owner.name,
                err,
            )
            return False
        return True

    def _members_to_power_on(
        self, owner: Player, member_ids: Iterable[str] | None = None
    ) -> list[Player]:
        """
        Return the players a start of a group player or leader powers on or waits for.

        A player that is off, or starting up, at a power control that cuts its power.

        :param owner: The group player or leader that is about to play.
        :param member_ids: The players to select from. The members of the owner when omitted.
        """
        owner_members = (*owner.group_members, *owner.static_group_members)
        return [
            member
            for member_id in dict.fromkeys(owner_members if member_ids is None else member_ids)
            if (member := self.get_player(member_id)) is not None
            and member.available
            and member.offline_by_power_control
        ]

    def _cancel_power_on(self, player: Player) -> None:
        """
        Call off a power on of the player that still waits for it to start up.

        The requests waiting for it fail as switched off. Its power control is left as it
        is, and its start-up time runs on.

        :param player: The player whose power on to call off.
        """
        if (wait := self._power_on_waits.pop(player.player_id, None)) is not None:
            wait.called_off = True
            wait.over.set()
            self.logger.debug("Called off the power on of player %s", player.name)

    def _cancel_member_power_ons(self, *players: Player) -> None:
        """
        Call off the power on of the players that a start of a player, or an addition to it, waits for.

        The commands that wait for those players fail as unavailable, and nothing is started
        or added. The power controls are left as they are.

        :param players: The group players or leaders whose members are powered on.
        """
        owners = {player.player_id: player.name for player in players}
        for power_ons, (owner_id, _) in self._member_power_ons.items():
            if owner_id in owners and power_ons.cancel():
                self.logger.debug("Called off the power on of the members of %s", owners[owner_id])

    def _is_powered_on_for_a_start(self, player: Player) -> bool:
        """
        Return whether a start, or an addition, still waits for players it powered this one on with.

        Such a player may have started up for a while before the others have: it is about
        to play.

        :param player: The player to check.
        """
        return any(
            player.player_id in member_ids for _, member_ids in self._member_power_ons.values()
        )

    def _has_waiting_start(self, player: Player) -> bool:
        """
        Return whether a command still waits for players to start up before it acts on this one.

        That is a start of the player, or an addition to it, that waits for the switched-off
        players it powered on, or a command that waits for the player itself.

        :param player: The player to check.
        """
        return player.player_id in self._power_on_waits or any(
            owner_id == player.player_id for owner_id, _ in self._member_power_ons.values()
        )
