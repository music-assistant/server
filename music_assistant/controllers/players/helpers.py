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

from music_assistant_models.errors import (
    InsufficientPermissions,
    MusicAssistantError,
    PlayerCommandFailed,
)

from music_assistant.controllers.players.constants import PlayerLockPurpose
from music_assistant.controllers.webserver.helpers.auth_middleware import get_current_user

if TYPE_CHECKING:
    import logging

    from music_assistant_models.player_control import PlayerControl

    from music_assistant.models.player import Player

    from .controller import PlayerController


class AnnounceData(TypedDict):
    """Announcement data for play_announcement command."""

    announcement_url: str
    pre_announce: bool
    pre_announce_url: str
    # player that fetches the announcement stream when it is not the
    # visible player itself (e.g. a linked protocol player)
    announce_player_id: str | None


@overload
def handle_player_command[PlayerControllerT: "PlayerController", **P, R](
    func: Callable[Concatenate[PlayerControllerT, P], Awaitable[R]],
) -> Callable[Concatenate[PlayerControllerT, P], Coroutine[Any, Any, R | None]]: ...


@overload
def handle_player_command[PlayerControllerT: "PlayerController", **P, R](
    func: None = None,
    *,
    lock: PlayerLockPurpose | None = None,
) -> Callable[
    [Callable[Concatenate[PlayerControllerT, P], Awaitable[R]]],
    Callable[Concatenate[PlayerControllerT, P], Coroutine[Any, Any, R | None]],
]: ...


def handle_player_command[PlayerControllerT: "PlayerController", **P, R](
    func: Callable[Concatenate[PlayerControllerT, P], Awaitable[R]] | None = None,
    *,
    lock: PlayerLockPurpose | None = None,
) -> (
    Callable[Concatenate[PlayerControllerT, P], Coroutine[Any, Any, R | None]]
    | Callable[
        [Callable[Concatenate[PlayerControllerT, P], Awaitable[R]]],
        Callable[Concatenate[PlayerControllerT, P], Coroutine[Any, Any, R | None]],
    ]
):
    """
    Decorator to check and log commands to players.

    Validates that the player exists and is available before executing the command.
    Also checks user permissions and optionally acquires a per-player lock.

    :param func: The function to wrap (when used without parentheses).
    :param lock: PlayerLockPurpose to serialize commands in the same category per
        player. Commands with the same lock purpose on the same player will not run
        concurrently. None (default) means no locking.
    """  # noqa: D401

    def decorator(
        fn: Callable[Concatenate[PlayerControllerT, P], Awaitable[R]],
    ) -> Callable[Concatenate[PlayerControllerT, P], Coroutine[Any, Any, R | None]]:
        @functools.wraps(fn)
        async def wrapper(self: PlayerControllerT, *args: P.args, **kwargs: P.kwargs) -> None:
            """Log and handle_player_command commands to players."""
            player_id = kwargs.get("player_id") or args[0]
            assert isinstance(player_id, str)  # for type checking
            if (player := self._players.get(player_id)) is None or not player.available:
                self.logger.warning(
                    "Ignoring command %s for unavailable player %s",
                    fn.__name__,
                    player_id,
                )
                return

            # this should not happen, but in case a player_id of a protocol player is used,
            # auto-resolve it to the parent player
            if player.protocol_parent_id and (
                protocol_parent := self._players.get(player.protocol_parent_id)
            ):
                player = protocol_parent
                if "player_id" in kwargs:
                    kwargs["player_id"] = protocol_parent.player_id
                else:
                    args = (protocol_parent.player_id, *args[1:])  # type: ignore[assignment]
                self.logger.debug(
                    "Auto-resolved protocol player %s to linked parent %s for command %s",
                    player_id,
                    protocol_parent.player_id,
                    fn.__name__,
                )

            current_user = get_current_user()
            if (
                current_user
                and current_user.player_filter
                and player.player_id not in current_user.player_filter
            ):
                msg = (
                    f"{current_user.username} does not have access to player {player.display_name}"
                )
                raise InsufficientPermissions(msg)

            self.logger.debug(
                "Handling command %s for player %s (%s)",
                fn.__name__,
                player.display_name,
                f"by user {current_user.username}" if current_user else "unauthenticated",
            )

            try:
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

    # Support both @handle_player_command and @handle_player_command(lock=...)
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
                while not player.powered:
                    await asyncio.sleep(0.1)
    except TimeoutError:
        logger.debug(
            "Player %s did not report powered on within %s seconds",
            player.state.name,
            timeout,
        )
