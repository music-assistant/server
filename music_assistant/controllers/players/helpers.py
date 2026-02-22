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

from music_assistant_models.errors import InsufficientPermissions, PlayerCommandFailed

from music_assistant.controllers.webserver.helpers.auth_middleware import get_current_user

if TYPE_CHECKING:
    from .controller import PlayerController


class AnnounceData(TypedDict):
    """Announcement data for play_announcement command."""

    announcement_url: str
    pre_announce: bool
    pre_announce_url: str


@overload
def handle_player_command[PlayerControllerT: "PlayerController", **P, R](
    func: Callable[Concatenate[PlayerControllerT, P], Awaitable[R]],
) -> Callable[Concatenate[PlayerControllerT, P], Coroutine[Any, Any, R | None]]: ...


@overload
def handle_player_command[PlayerControllerT: "PlayerController", **P, R](
    func: None = None,
    *,
    lock: bool = False,
) -> Callable[
    [Callable[Concatenate[PlayerControllerT, P], Awaitable[R]]],
    Callable[Concatenate[PlayerControllerT, P], Coroutine[Any, Any, R | None]],
]: ...


def handle_player_command[PlayerControllerT: "PlayerController", **P, R](
    func: Callable[Concatenate[PlayerControllerT, P], Awaitable[R]] | None = None,
    *,
    lock: bool = False,
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
    :param lock: If True, acquire a lock per player_id and function name before executing.
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
                self.logger.info(
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

            async def execute() -> None:
                async with self._player_throttlers[player.player_id]:
                    try:
                        await fn(self, *args, **kwargs)
                    except Exception as err:
                        raise PlayerCommandFailed(str(err)) from err

            if lock:
                # Acquire a lock specific to player_id and function name
                lock_key = f"{fn.__name__}_{player_id}"
                if lock_key not in self._player_command_locks:
                    self._player_command_locks[lock_key] = asyncio.Lock()
                async with self._player_command_locks[lock_key]:
                    await execute()
            else:
                await execute()

        return wrapper

    # Support both @handle_player_command and @handle_player_command(lock=True)
    if func is not None:
        return decorator(func)
    return decorator
