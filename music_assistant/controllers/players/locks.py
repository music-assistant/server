"""
Player Locks Mixin for the Player Controller.

Serializes the commands on a player, per purpose. A command that needs the lock of a
player and that of the group holding it takes the group's first.

This module provides the PlayerLocksMixin class which is inherited by
PlayerController to add the player command locks.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator

from music_assistant_models.errors import ResourceBusyError

from .base import _PlayerControllerBase
from .constants import (
    PLAYER_LOCK_OWNER_ATTEMPTS,
    PLAYER_LOCK_SLOW_THRESHOLD,
    PLAYER_LOCK_TIMEOUT,
    PLAYER_LOCKS_ATTEMPTS,
    PlayerLockPurpose,
)


class PlayerLocksMixin(_PlayerControllerBase):
    """Mixin class providing the player command locks for PlayerController."""

    @contextlib.asynccontextmanager
    async def get_player_lock(
        self,
        player_id: str,
        purpose: PlayerLockPurpose = PlayerLockPurpose.PLAYBACK,
    ) -> AsyncIterator[None]:
        """
        Acquire a purpose-scoped lock for a player, with re-entrant support.

        Tracks lock ownership per asyncio Task so that nested calls within the same
        task skip re-acquisition (preventing deadlocks), while deferred callbacks
        (call_later / create_task) correctly acquire a fresh lock.

        The body never runs without the lock: a command that cannot get it within
        120s fails with ResourceBusyError, as the player is still busy with another.

        Ordering rule: when a command needs both a group/leader lock and a member
        lock, it must take the group's first. The group players themselves lock their
        members from under their own lock, so a member-first acquisition is an
        inversion and will deadlock. A command on a member that reaches its group uses
        get_group_and_player_lock for that.

        :param player_id: The player to lock.
        :param purpose: Lock category. Commands with different purposes can run
            concurrently on the same player.
        :raises ResourceBusyError: When the lock is still held by another command at the
            timeout.
        """
        async with self._player_lock(player_id, purpose, wait=True):
            yield

    @contextlib.asynccontextmanager
    async def get_player_lock_if_free(
        self,
        player_id: str,
        purpose: PlayerLockPurpose = PlayerLockPurpose.PLAYBACK,
    ) -> AsyncIterator[bool]:
        """
        Acquire a player's lock only when no other command holds it or waits for it.

        For work nobody asked for at this moment, such as an automatic re-add of a group
        member: it skips a player that is busy and never waits itself. A command that
        arrives while the lock is held waits behind it, as behind any other holder.
        Re-entrant like get_player_lock.

        :param player_id: The player to lock.
        :param purpose: Lock category.
        :return: Whether the lock is held in the body.
        """
        async with self._player_lock(player_id, purpose, wait=False) as acquired:
            yield acquired

    def holds_player_lock(
        self, player_id: str, purpose: PlayerLockPurpose = PlayerLockPurpose.PLAYBACK
    ) -> bool:
        """
        Return whether the running task holds a player's lock.

        Work that the holder hands to tasks of their own and waits for must not wait for a
        lock the holder has: the work on such a player runs in the holder's task instead.

        :param player_id: The player whose lock to check.
        :param purpose: The lock's category.
        """
        task = asyncio.current_task()
        return task is not None and f"{purpose.value}_{player_id}" in self._task_held_locks.get(
            task, set()
        )

    def holds_playback_owner_lock(self, player_id: str) -> bool:
        """
        Return whether the running task holds the playback lock of a player's owner.

        The owner is the player itself, its sync leader or the group holding it. A task that
        holds a player that got another owner since (it joined a group) does not hold that
        owner's lock, and a playback command on the player from that task fails as busy.

        :param player_id: The player whose owner to check.
        """
        return self.holds_player_lock(self._playback_owner_id(player_id))

    @contextlib.asynccontextmanager
    async def get_group_and_player_lock(self, player_id: str) -> AsyncIterator[None]:
        """
        Acquire the playback lock of a player, preceded by that of the group holding it.

        For a command that works on a group member under its (sync)group: releasing the
        member, joining it back or restarting the group all take the group's lock,
        which has to be taken before the member's own (see get_player_lock). A player
        that is not part of a group only takes its own lock. Each lock has its own
        timeout, so a player held by a group waits for both in turn.

        The body runs once both locks are held and the group still holds the player (see
        _playback_owner_locks for a player that joined or left a group meanwhile).

        :param player_id: The player to lock.
        :raises ResourceBusyError: When either lock is still held by another command at
            its timeout, or the player kept changing its group while the command waited.
        """
        async with self._playback_owner_locks(player_id, hold_player=True):
            yield

    @contextlib.asynccontextmanager
    async def get_playback_locks(self, *player_ids: str) -> AsyncIterator[None]:
        """
        Acquire the playback locks of several players, each preceded by that of its owner.

        For a command that works on several players at once, such as a queue transfer. Each
        player is held the way get_group_and_player_lock holds it, until the body is done. The
        command never waits for a player's lock while it holds that of another player that does
        not own it, so it never waits for a command that waits for it, and commands over other
        players do not wait for it at all. The locks the caller holds already stay held while it
        waits, so the caller must hold none of a player that is not one of these players or their
        owners.

        :param player_ids: The players to lock.
        :raises ResourceBusyError: When the locks are not all held within the timeout of one
            lock, a player stayed busy each time the others were held, or a player kept
            changing its group while the command waited.
        """
        async with contextlib.AsyncExitStack() as stack:
            await self._enter_playback_locks(stack, player_ids)
            yield

    @contextlib.asynccontextmanager
    async def _playback_owner_locks(
        self, player_id: str, *, hold_player: bool
    ) -> AsyncIterator[None]:
        """
        Hold the playback lock of the owner of a player's playback, and the player's own.

        A command holds the lock of the player it works on and of no player it only passes
        through: one that is redirected to the owner (a sync leader, or the group holding
        the player) holds the owner's lock alone, as the owner's handler locks each member it
        works on itself; one that works on the player under its group holds the group's lock
        and then the player's. The body runs once the locks are held and the owner still
        owns the player. When the player joined or left a group meanwhile, the locks are
        released and taken again in the order of its new owner, so a command never holds a
        player's lock while it waits for the lock of that player's owner. A command that
        holds the player's lock already, and finds the player in a group whose lock it does
        not hold, fails at once instead of waiting for that group's lock.

        :param player_id: The player the command is addressed to.
        :param hold_player: Hold the player's own lock after the owner's, for a command that
            works on the player itself.
        :raises ResourceBusyError: When a lock is still held by another command at its
            timeout, or the player kept changing its group while the command waited.
        """
        task = asyncio.current_task()
        for _attempt in range(PLAYER_LOCK_OWNER_ATTEMPTS):
            owner_id = self._playback_owner_id(player_id)
            held = self._task_held_locks.get(task, set()) if task else set()
            if (
                owner_id != player_id
                and f"{PlayerLockPurpose.PLAYBACK.value}_{player_id}" in held
                and f"{PlayerLockPurpose.PLAYBACK.value}_{owner_id}" not in held
            ):
                # the task holds the player already, so it can not let go of it to wait for
                # the owner the player got since
                break
            async with contextlib.AsyncExitStack() as stack:
                await stack.enter_async_context(
                    self.get_player_lock(owner_id, PlayerLockPurpose.PLAYBACK)
                )
                if hold_player and owner_id != player_id:
                    await stack.enter_async_context(
                        self.get_player_lock(player_id, PlayerLockPurpose.PLAYBACK)
                    )
                if self._playback_owner_id(player_id) == owner_id:
                    yield
                    return
            self.logger.debug(
                "Player %s changed its group while waiting for its lock, taking it again",
                player_id,
            )
        name = player.name if (player := self.get_player(player_id)) else player_id
        msg = f"Player {name} changed its group while the command waited for its playback lock"
        raise ResourceBusyError(
            msg,
            translation_key="player_busy",
            translation_owner=self.translation_owner,
            translation_args=[name],
        )

    async def _enter_playback_locks(
        self, stack: contextlib.AsyncExitStack, player_ids: tuple[str, ...]
    ) -> None:
        """
        Take the playback locks of several players and of their owners onto a stack.

        The locks of one player (and its owner) are waited for, those of the others only taken
        when they are free. When one is busy, every lock taken is let go of again and the next
        attempt waits for that player first.

        :param stack: The stack that holds the locks once they are all taken.
        :param player_ids: The players to lock.
        :raises ResourceBusyError: See get_playback_locks.
        """
        # commands over the same players wait for the same player first, so they take turns
        # rather than each holding a lock that the other one finds busy
        order = sorted(
            set(player_ids), key=lambda player_id: (self._playback_owner_id(player_id), player_id)
        )
        waited = order[0]
        try:
            async with asyncio.timeout(PLAYER_LOCK_TIMEOUT):
                for _attempt in range(PLAYER_LOCKS_ATTEMPTS):
                    async with contextlib.AsyncExitStack() as attempt:
                        await attempt.enter_async_context(self.get_group_and_player_lock(waited))
                        for player_id in order:
                            if player_id != waited and not await self._enter_free_playback_locks(
                                attempt, player_id
                            ):
                                waited = player_id
                                break
                        else:
                            stack.push_async_exit(attempt.pop_all())
                            return
        except TimeoutError:
            # name the lock that another command held at the timeout: the owner's or the player's
            task = asyncio.current_task()
            busy_id = next(
                (
                    lock_id
                    for lock_id in (self._playback_owner_id(waited), waited)
                    if self._lock_holders.get(f"{PlayerLockPurpose.PLAYBACK.value}_{lock_id}")
                    not in (None, task)
                ),
                waited,
            )
            raise self._player_busy_error(
                f"{PlayerLockPurpose.PLAYBACK.value}_{busy_id}", PlayerLockPurpose.PLAYBACK, busy_id
            ) from None
        name = player.name if (player := self.get_player(waited)) else waited
        msg = f"Player {name} stayed busy while the command took the locks of several players"
        raise ResourceBusyError(
            msg,
            translation_key="player_busy",
            translation_owner=self.translation_owner,
            translation_args=[name],
        )

    async def _enter_free_playback_locks(
        self, stack: contextlib.AsyncExitStack, player_id: str
    ) -> bool:
        """
        Take the playback lock of a player's owner and the player's own onto a stack, if free.

        :param stack: The stack to hold the locks on.
        :param player_id: The player to lock.
        :return: Whether both are held; when not, the stack can hold the owner's.
        """
        # a free lock is taken without suspending, so the owner can not change in between
        owner_id = self._playback_owner_id(player_id)
        for lock_id in dict.fromkeys((owner_id, player_id)):
            if not await stack.enter_async_context(
                self._player_lock(lock_id, PlayerLockPurpose.PLAYBACK, wait=False)
            ):
                return False
        return True

    @contextlib.asynccontextmanager
    async def _player_lock(
        self, player_id: str, purpose: PlayerLockPurpose, *, wait: bool
    ) -> AsyncIterator[bool]:
        """
        Hold a player's lock, re-entrant within one task.

        :param player_id: The player to lock.
        :param purpose: Lock category.
        :param wait: Wait for the lock up to the timeout; otherwise take it only when it is
            free right now.
        :return: Whether the lock is held in the body; always True when waiting.
        :raises ResourceBusyError: When a wait for the lock times out.
        """
        lock_key = f"{purpose.value}_{player_id}"
        task = asyncio.current_task()

        if task is not None and lock_key in self._task_held_locks.get(task, set()):
            yield True
            return

        lock = self._player_command_locks.setdefault(lock_key, asyncio.Lock())
        if wait:
            await self._acquire_player_lock(lock, lock_key, purpose, player_id)
        else:
            acquired = True
            try:
                # a free lock is taken without suspending, so only a held one (or one others
                # wait for) reaches the timeout
                async with asyncio.timeout(0):
                    await lock.acquire()
            except TimeoutError:
                acquired = False
            if not acquired:
                yield False
                return
        if task is not None:
            self._task_held_locks.setdefault(task, set()).add(lock_key)
            self._lock_holders[lock_key] = task
        try:
            yield True
        finally:
            if task is not None:
                if (held := self._task_held_locks.get(task)) is not None:
                    held.discard(lock_key)
                    if not held:
                        del self._task_held_locks[task]
                if self._lock_holders.get(lock_key) is task:
                    del self._lock_holders[lock_key]
            lock.release()

    async def _acquire_player_lock(
        self, lock: asyncio.Lock, lock_key: str, purpose: PlayerLockPurpose, player_id: str
    ) -> None:
        """
        Wait for a player lock, logging a wait that gets long.

        :param lock: The lock to acquire.
        :param lock_key: The key the lock is kept under, which names its holder.
        :param purpose: The lock's category, named in the log lines and the error.
        :param player_id: The player the lock belongs to.
        :raises ResourceBusyError: When the lock is still held at the timeout.
        """
        try:
            async with asyncio.timeout(PLAYER_LOCK_SLOW_THRESHOLD):
                await lock.acquire()
            return
        except TimeoutError:
            self.logger.debug(
                "Acquiring %s lock for player %s is slow (>%ss)",
                purpose.value,
                player_id,
                PLAYER_LOCK_SLOW_THRESHOLD,
            )
        try:
            async with asyncio.timeout(PLAYER_LOCK_TIMEOUT - PLAYER_LOCK_SLOW_THRESHOLD):
                await lock.acquire()
            return
        except TimeoutError:
            pass
        raise self._player_busy_error(lock_key, purpose, player_id)

    def _player_busy_error(
        self, lock_key: str, purpose: PlayerLockPurpose, player_id: str
    ) -> ResourceBusyError:
        """
        Log what holds a lock that a command gave up waiting for, and return its error.

        :param lock_key: The key the lock is kept under, which names its holder.
        :param purpose: The lock's category, named in the log line and the error.
        :param player_id: The player the lock belongs to.
        """
        name = player.name if (player := self.get_player(player_id)) else player_id
        # the holder's call graph shows what it waits for, so a stuck command can be found
        holder = self._lock_holders.get(lock_key)
        self.logger.warning(
            "Gave up waiting %ss for the %s lock of player %s, which is held by %s",
            PLAYER_LOCK_TIMEOUT,
            purpose.value,
            name,
            asyncio.format_call_graph(holder)
            if holder is not None and not holder.done()
            else "a task that is gone",
        )
        msg = (
            f"Player {name} is still busy with a previous {purpose.value} command "
            f"after {PLAYER_LOCK_TIMEOUT}s"
        )
        return ResourceBusyError(
            msg,
            translation_key="player_busy",
            translation_owner=self.translation_owner,
            translation_args=[name],
        )
