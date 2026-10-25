"""
Helper utilities for the Player Controller.

Contains decorators, type definitions, and utility functions used by the
PlayerController that don't need direct access to the controller class.
"""

from __future__ import annotations

import asyncio
import functools
from collections.abc import Awaitable, Callable, Coroutine
from typing import TYPE_CHECKING, Any, Concatenate, NamedTuple, TypedDict, overload

from music_assistant_models.enums import SleepTimerMode
from music_assistant_models.errors import (
    InsufficientPermissions,
    MusicAssistantError,
    PlayerCommandFailed,
)

from music_assistant.controllers.players.constants import PlayerLockPurpose
from music_assistant.controllers.webserver.helpers.auth_middleware import (
    get_current_user,
    has_player_access,
)

if TYPE_CHECKING:
    import logging

    from music_assistant_models.player_control import PlayerControl
    from music_assistant_models.queue_item import QueueItem

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


class SleepTimerTarget(NamedTuple):
    """Queue item and media position at which a sleep timer stops playback."""

    queue_item_id: str
    stop_position: float


def get_sleep_timer_stop_position(
    mode: SleepTimerMode, queue_item: QueueItem, elapsed_time: float
) -> float | None:
    """
    Return the position in the queue item at which a sleep timer in the given mode stops it.

    Returns None when the item has no known end. Outside of any chapter, END_OF_CHAPTER stops
    at the end of the item.

    :param mode: END_OF_ITEM or END_OF_CHAPTER.
    :param queue_item: The queue item that is playing.
    :param elapsed_time: The (media-time) position in the queue item.
    """
    if not queue_item.duration:
        return None
    item_end = float(queue_item.duration)
    if mode != SleepTimerMode.END_OF_CHAPTER or queue_item.media_item is None:
        return item_end
    chapters = sorted(queue_item.media_item.metadata.chapters or [], key=lambda x: x.start)
    # a seek takes whole seconds, so a seek to a chapter start lands up to a second before it
    position = elapsed_time + 1
    for index, chapter in enumerate(chapters):
        if chapter.end is not None:
            chapter_end = chapter.end
        elif index + 1 < len(chapters):
            chapter_end = chapters[index + 1].start
        else:
            chapter_end = item_end
        if chapter.start <= position < chapter_end:
            return chapter_end
    return item_end


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

    Validates that the player exists and is available before executing the command,
    raising PlayerUnavailableError otherwise.
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
        async def wrapper(self: PlayerControllerT, /, *args: P.args, **kwargs: P.kwargs) -> None:
            """Log and handle_player_command commands to players."""
            player_id = kwargs.get("player_id") or args[0]
            assert isinstance(player_id, str)  # for type checking

            # this should not happen, but in case a player_id of a protocol player is used,
            # auto-resolve it to the parent player
            if (
                (player := self._players.get(player_id))
                and player.protocol_parent_id
                and (protocol_parent := self._players.get(player.protocol_parent_id))
            ):
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
                player_id = protocol_parent.player_id

            player = self.get_player(player_id, raise_unavailable=True)
            assert player is not None  # for type checking

            current_user = get_current_user()
            if current_user and not has_player_access(current_user, player.player_id, player):
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
