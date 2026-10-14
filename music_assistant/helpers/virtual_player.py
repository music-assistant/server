"""Helper for removing a virtual player that outlived its creation."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import logging
    from collections.abc import Awaitable, Callable


async def cleanup_virtual_player(
    player_id: str,
    delays: tuple[float, ...],
    is_present: Callable[[str], bool],
    remove: Callable[[str], Awaitable[None]],
    logger: logging.Logger,
    failure_message: str,
) -> None:
    """
    Remove a leftover virtual player, retrying removal across the given delays.

    :param player_id: Virtual player to remove.
    :param delays: Delay before each attempt; a zero delay attempts immediately.
    :param is_present: Predicate returning whether the player is still ours to remove.
    :param remove: Coroutine function that removes the player.
    :param logger: Logger for the warning emitted once every attempt fails.
    :param failure_message: printf-style warning logged with the player_id and the
        last error when all attempts are exhausted.
    """
    last_error: Exception | None = None
    for delay in delays:
        if delay:
            await asyncio.sleep(delay)
        try:
            # another teardown won the race; a config it left behind is not ours
            # to delete - it is kept for the owner to reclaim, and swept at
            # startup once that owner is gone
            if not is_present(player_id):
                return
            # awaited to completion on purpose: a timeout is no reliable bound on
            # the teardown - parts of it swallow the cancellation (see
            # AsyncProcess.close), and one that does land leaves the player
            # half torn down for the next attempt to trip over
            await remove(player_id)
            return
        except Exception as err:
            last_error = err
    logger.warning(failure_message, player_id, last_error)
