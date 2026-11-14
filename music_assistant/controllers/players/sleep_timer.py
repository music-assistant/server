"""
Sleep Timer Mixin for the Player Controller.

Stops the playback of a player after a set time or at a point in its queue.

This module provides the SleepTimerMixin class which is inherited by
PlayerController to add the sleep timer commands.
"""

from __future__ import annotations

import time

from music_assistant_models.auth import Scope
from music_assistant_models.enums import EventType, SleepTimerMode
from music_assistant_models.errors import InvalidDataError

from music_assistant.helpers.api import api_command
from music_assistant.models.player import Player

from .base import _PlayerControllerBase


class SleepTimerMixin(_PlayerControllerBase):
    """Mixin class providing the sleep timer for PlayerController."""

    @api_command("players/sleep_timer/get", required_scope=Scope.PLAYERS_READ)
    def get_sleep_timer(self, player_id: str) -> float | None:
        """
        Return the active sleep timer expiry timestamp for the player.

        :param player_id: Player ID to check.
        :raises InsufficientPermissions: if the current user may not use the player, or the
            sync leader or group player it plays along with.
        """
        player = self._get_sleep_timer_player(player_id)
        return player.sleep_timer_expires_at

    @api_command("players/sleep_timer/set", required_scope=Scope.PLAYERS_CONTROL)
    def set_sleep_timer(
        self,
        player_id: str,
        seconds: int | None = None,
        mode: SleepTimerMode = SleepTimerMode.DURATION,
    ) -> float | None:
        """
        Set a sleep timer for the player.

        Returns the (estimated) expiry timestamp, or None while a queue item or chapter timer
        waits for paused playback.

        :param player_id: Player ID to set the timer for.
        :param seconds: Delay in seconds before playback is stopped, for the DURATION mode.
        :param mode: The point at which playback is stopped.
        :raises InsufficientPermissions: if the current user may not use the player, or the
            sync leader or group player it plays along with.
        """
        player = self._get_sleep_timer_player(player_id)
        if mode in (SleepTimerMode.END_OF_ITEM, SleepTimerMode.END_OF_CHAPTER):
            # the queue stops itself at that point, shows it on the player and raises when
            # nothing with an end plays
            expires_at = self.mass.player_queues.set_sleep_timer_target(player.player_id, mode)
            self.mass.cancel_timer(self._sleep_timer_task_id(player.player_id))
            return expires_at
        if mode != SleepTimerMode.DURATION:
            msg = f"Unsupported sleep timer mode: {mode}"
            raise InvalidDataError(msg)
        if seconds is None or seconds <= 0:
            msg = "Sleep timer duration must be greater than zero seconds"
            raise InvalidDataError(msg)
        try:
            # guard against absurd durations that overflow the float timestamp math
            expires_at = time.time() + seconds
        except OverflowError:
            msg = "Sleep timer duration is too large to schedule"
            raise InvalidDataError(msg) from None
        self.mass.player_queues.drop_sleep_timer_target(player.player_id)
        player.set_sleep_timer(SleepTimerMode.DURATION, expires_at)
        self._signal_sleep_timer_updated(player, expires_at)
        self.mass.call_later(
            seconds,
            self._handle_sleep_timer_expired,
            player.player_id,
            task_id=self._sleep_timer_task_id(player.player_id),
        )
        return expires_at

    @api_command("players/sleep_timer/clear", required_scope=Scope.PLAYERS_CONTROL)
    def clear_sleep_timer(self, player_id: str) -> None:
        """
        Clear the active sleep timer for the player.

        :param player_id: Player ID to clear the timer for.
        :raises InsufficientPermissions: if the current user may not use the player, or the
            sync leader or group player it plays along with.
        """
        self._clear_sleep_timer(self._get_sleep_timer_player(player_id))

    def update_sleep_timer(
        self, player_id: str, mode: SleepTimerMode, expires_at: float | None
    ) -> None:
        """
        Update a sleep timer that stops at a point in the player's queue.

        :param player_id: Player ID (and queue ID) whose sleep timer to update.
        :param mode: The point at which the queue stops.
        :param expires_at: The estimated unix (utc) timestamp of the stop, None while paused.
        """
        player = self.get_player(player_id)
        if player is None:
            return
        prev_expires_at = player.sleep_timer_expires_at
        if player.sleep_timer_mode == mode and (
            (prev_expires_at is None and expires_at is None)
            or (
                prev_expires_at is not None
                and expires_at is not None
                # position reports jitter, only announce a real change
                and abs(expires_at - prev_expires_at) < 1
            )
        ):
            return
        player.set_sleep_timer(mode, expires_at)
        self._signal_sleep_timer_updated(player, expires_at)

    def reset_sleep_timer(self, player: Player) -> None:
        """
        Reset the sleep timer state of the player, without touching its queue.

        :param player: Player whose sleep timer ended.
        """
        if player.sleep_timer_mode is None:
            return
        player.set_sleep_timer(None, None)
        self._signal_sleep_timer_updated(player, None)

    def _clear_sleep_timer(self, player: Player) -> None:
        """
        Clear the active sleep timer for the player.

        :param player: Player to clear the timer for.
        """
        self.mass.cancel_timer(self._sleep_timer_task_id(player.player_id))
        self.mass.player_queues.drop_sleep_timer_target(player.player_id)
        self.reset_sleep_timer(player)

    async def _handle_sleep_timer_expired(self, player_id: str) -> None:
        """
        Stop playback when a player's sleep timer expires.

        :param player_id: Player ID whose sleep timer expired.
        """
        player = self.get_player(player_id)
        if player is None or player.sleep_timer_expires_at is None:
            return
        self._clear_sleep_timer(player)
        if player.available_for_playback:
            await self.cmd_stop(player_id)

    def _signal_sleep_timer_updated(self, player: Player, expires_at: float | None) -> None:
        """
        Signal a sleep timer change for the player on the event bus.

        :param player: Player whose sleep timer changed.
        :param expires_at: New expiry timestamp, or None when the timer was cleared or waits
            for paused playback (see the player's sleep_timer_mode).
        """
        self.mass.signal_event(
            EventType.PLAYER_SLEEP_TIMER_UPDATED,
            object_id=player.player_id,
            data=expires_at,
        )

    @staticmethod
    def _sleep_timer_task_id(player_id: str) -> str:
        """
        Return the scheduled task ID for a player's sleep timer.

        :param player_id: Player ID to build the task ID for.
        """
        return f"player_sleep_timer_{player_id}"

    def _get_sleep_timer_player(self, player_id: str) -> Player:
        """
        Return the player that holds the sleep timer of the given player.

        That is the owner of its playback (see _resolve_playback_owner).

        :param player_id: The player the sleep timer command names.
        :raises InsufficientPermissions: if the current user may not use the player or that owner.
        """
        self.check_player_access(player_id)
        player = self._get_player_with_redirect(player_id)
        if player.player_id != player_id:
            self.check_player_access(player.player_id)
        return player
