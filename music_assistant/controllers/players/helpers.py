"""
Helper utilities for the Player Controller.

Contains decorators, type definitions, and utility functions used by the
PlayerController that don't need direct access to the controller class.
"""

from __future__ import annotations

import asyncio
import functools
from collections.abc import Awaitable, Callable, Coroutine
from typing import TYPE_CHECKING, Any, Concatenate, TypedDict, overload

from music_assistant_models.enums import PlayerType
from music_assistant_models.errors import (
    InsufficientPermissions,
    MusicAssistantError,
    PlayerCommandFailed,
    PlayerUnavailableError,
)

from music_assistant.controllers.players.constants import PlayerLockPurpose, SwitchedOffAction
from music_assistant.controllers.webserver.helpers.auth_middleware import (
    get_current_user,
    has_player_access,
)

if TYPE_CHECKING:
    import logging

    from music_assistant_models.player_control import PlayerControl

    from music_assistant.models.player import Player

    from .base import _PlayerControllerBase


class AnnounceData(TypedDict):
    """Announcement data for play_announcement command."""

    announcement_url: str
    pre_announce: bool
    pre_announce_url: str
    # protocol that fetches the announcement stream when it is not the
    # own protocol of the player (e.g. one of its audio protocols)
    announce_player_id: str | None


@overload
def handle_player_command[PlayerControllerT: "_PlayerControllerBase", **P, R](
    func: Callable[Concatenate[PlayerControllerT, P], Awaitable[R]],
) -> Callable[Concatenate[PlayerControllerT, P], Coroutine[Any, Any, R | None]]: ...


@overload
def handle_player_command[PlayerControllerT: "_PlayerControllerBase", **P, R](
    func: None = None,
    *,
    lock: PlayerLockPurpose | None = None,
    switched_off: SwitchedOffAction = SwitchedOffAction.REFUSE,
    accept_unavailable: bool = False,
    to_playback_owner: bool = False,
) -> Callable[
    [Callable[Concatenate[PlayerControllerT, P], Awaitable[R]]],
    Callable[Concatenate[PlayerControllerT, P], Coroutine[Any, Any, R | None]],
]: ...


def handle_player_command[PlayerControllerT: "_PlayerControllerBase", **P, R](
    func: Callable[Concatenate[PlayerControllerT, P], Awaitable[R]] | None = None,
    *,
    lock: PlayerLockPurpose | None = None,
    switched_off: SwitchedOffAction = SwitchedOffAction.REFUSE,
    accept_unavailable: bool = False,
    to_playback_owner: bool = False,
) -> (
    Callable[Concatenate[PlayerControllerT, P], Coroutine[Any, Any, R | None]]
    | Callable[
        [Callable[Concatenate[PlayerControllerT, P], Awaitable[R]]],
        Callable[Concatenate[PlayerControllerT, P], Coroutine[Any, Any, R | None]],
    ]
):
    """
    Decorator to check and log commands to players.

    Validates that the player exists and is available before executing the command,
    raising PlayerUnavailableError otherwise (see accept_unavailable).
    Also checks user permissions and optionally acquires a per-player lock.

    :param func: The function to wrap (when used without parentheses).
    :param lock: PlayerLockPurpose to serialize commands in the same category per
        player. Commands with the same lock purpose on the same player will not run
        concurrently. None (default) means no locking.
    :param switched_off: What the command does with a player that is switched off at its
        external power control (see SwitchedOffAction): refused by default, ignored when
        the command has nothing to act on (volume), ignored after calling off a power on
        under way (stop, pause), powered on first (before any lock is taken) for commands
        that start playback, or run as is for commands that handle it themselves. A command
        that powers such a player on does the same for the switched-off members of the group
        player it plays (the player itself, or the owner of its playback), and one that calls
        a power on off does so for the members that the owner of the player's playback took
        along.
    :param accept_unavailable: Run the command for an unavailable player too, for a
        command that handles such a player itself (power).
    :param to_playback_owner: The command acts on the owner of the player's playback (its
        sync leader or group player), so the user needs access to that owner as well.
    """  # noqa: D401

    def decorator(
        fn: Callable[Concatenate[PlayerControllerT, P], Awaitable[R]],
    ) -> Callable[Concatenate[PlayerControllerT, P], Coroutine[Any, Any, R | None]]:
        @functools.wraps(fn)
        async def wrapper(self: PlayerControllerT, /, *args: P.args, **kwargs: P.kwargs) -> None:
            """Log and handle_player_command commands to players."""
            player_id = kwargs.get("player_id") or args[0]
            assert isinstance(player_id, str)  # for type checking

            if (command_player_id := self._command_player_id(player_id)) != player_id:
                self.logger.debug(
                    "Resolved %s to its player %s for command %s",
                    player_id,
                    command_player_id,
                    fn.__name__,
                )
            player = self.get_player(command_player_id, raise_unavailable=not accept_unavailable)
            if player is None:
                msg = f"Player {player_id} is not available"
                raise PlayerUnavailableError(msg)
            # the command works with the current id of the player
            if "player_id" in kwargs:
                kwargs["player_id"] = player.player_id
            else:
                args = (player.player_id, *args[1:])  # type: ignore[assignment]

            current_user = get_current_user()
            if current_user and not has_player_access(current_user, player.player_id, player):
                msg = f"{current_user.username} does not have access to player {player.name}"
                raise InsufficientPermissions(msg)
            if to_playback_owner and (owner := self._resolve_playback_owner(player)) is not player:
                self.check_player_access(owner.player_id)

            self.logger.debug(
                "Handling command %s for player %s (%s)",
                fn.__name__,
                player.name,
                f"by user {current_user.username}" if current_user else "unauthenticated",
            )

            is_switched_off = player.offline_by_power_control
            if is_switched_off and switched_off == SwitchedOffAction.REFUSE:
                msg = f"Player {player.name} is switched off"
                raise PlayerUnavailableError(msg)
            if switched_off == SwitchedOffAction.CANCEL_POWER_ON:
                for target in (player, self._resolve_playback_owner(player)):
                    self._cancel_power_on(target)
            if is_switched_off and switched_off in (
                SwitchedOffAction.IGNORE,
                SwitchedOffAction.CANCEL_POWER_ON,
            ):
                self.logger.debug(
                    "Ignoring command %s for player %s: it is switched off",
                    fn.__name__,
                    player.name,
                )
                return

            try:
                if switched_off == SwitchedOffAction.POWER_ON:
                    if is_switched_off:
                        await self._power_on_switched_off_player(player.player_id)
                    # the switched-off members of the group player the command plays, unless
                    # it runs inside a command on that group, which powered them on before
                    # it took the lock
                    group = self._resolve_playback_owner(player) if to_playback_owner else player
                    if group.type == PlayerType.GROUP and not self.holds_player_lock(
                        group.player_id
                    ):
                        await self._power_on_switched_off_player(group.player_id)
                if lock:
                    async with self.get_player_lock(player.player_id, lock):
                        await fn(self, *args, **kwargs)
                else:
                    await fn(self, *args, **kwargs)
            except MusicAssistantError:
                # A typed error already carries its own error code and translation
                # (e.g. "this device needs a password"); re-wrapping it here would
                # flatten every specific failure into the generic message.
                raise
            except Exception as err:
                raise PlayerCommandFailed(str(err)) from err

        return wrapper

    # Support both @handle_player_command and @handle_player_command(lock=..., ...)
    if func is not None:
        return decorator(func)
    return decorator


async def wait_for_power_on(
    logger: logging.Logger,
    player: Player,
    player_control: PlayerControl | None = None,
    timeout: float = 5.0,
) -> None:
    """
    Wait for a player (or player control) to report powered on after a power on command.

    :param logger: Logger instance for debug logging.
    :param player: The player to wait for (checked when player_control is None).
    :param player_control: Optional PlayerControl to check instead of the player.
    :param timeout: Maximum time to wait in seconds.
    """
    try:
        async with asyncio.timeout(timeout):
            if player_control is not None:
                while not player_control.power_state:
                    await asyncio.sleep(0.1)
            else:
                while not ((own := player.own_protocol) and own.powered):
                    await asyncio.sleep(0.1)
    except TimeoutError:
        logger.debug(
            "Player %s did not report powered on within %s seconds",
            player.name,
            timeout,
        )
