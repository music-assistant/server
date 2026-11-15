"""Tests for PlayerController.get_playback_locks, the locks a command over several players holds."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import cast
from unittest.mock import MagicMock

import pytest
from music_assistant_models.errors import ResourceBusyError

from music_assistant.controllers.players import PlayerController
from music_assistant.controllers.players.constants import PLAYER_LOCK_TIMEOUT, PlayerLockPurpose
from tests.common import bare_player_controller, record_lock_waits, virtual_loop_clock


def _players(owners: dict[str, str]) -> PlayerController:
    """
    Return a players controller with real locks, whose players are owned as given.

    :param owners: The owner (sync leader or group) of each player that has one, by player id;
        the test changes it to group or release a player.
    """
    players = bare_player_controller()
    players._playback_owner_id = lambda player_id: owners.get(player_id, player_id)  # type: ignore[method-assign]
    return players


def _locked(players: PlayerController, *player_ids: str) -> list[str]:
    """Return which of the players' playback locks are held."""
    return [
        player_id
        for player_id in player_ids
        if (lock := players._player_command_locks.get(f"playback_{player_id}")) and lock.locked()
    ]


async def _until(condition: Callable[[], bool]) -> None:
    """Let the other tasks run until the condition holds."""
    for _ in range(50):
        if condition():
            return
        await asyncio.sleep(0)
    msg = "the condition never held"
    raise AssertionError(msg)


async def test_every_player_and_its_owner_stay_locked_until_the_end() -> None:
    """Each player is held with its owner for the whole body, and is free again afterwards."""
    players = _players({"a": "c"})

    async with players.get_playback_locks("a", "b"):
        assert _locked(players, "a", "b", "c") == ["a", "b", "c"]

    assert _locked(players, "a", "b", "c") == []


async def test_the_same_player_given_twice_is_locked_once() -> None:
    """A transfer onto its own player holds that player, and lets go of it at the end."""
    players = _players({})

    async with players.get_playback_locks("a", "a"):
        assert _locked(players, "a") == ["a"]

    assert _locked(players, "a") == []


@pytest.mark.parametrize("busy", ["a", "b", "c"])
async def test_a_player_that_stays_busy_fails_as_busy_and_holds_nothing(busy: str) -> None:
    """
    The command never runs beside the holder of one of the locks: it fails at the lock timeout.

    :param busy: The player whose lock another command holds: the member, the solo player or
        the member's owner.
    """
    players = _players({"a": "c"})
    release = asyncio.Event()
    holding = asyncio.Event()

    async def _hold() -> None:
        async with players.get_player_lock(busy, PlayerLockPurpose.PLAYBACK):
            holding.set()
            await release.wait()

    with virtual_loop_clock():
        loop = asyncio.get_running_loop()
        holder = asyncio.create_task(_hold())
        await holding.wait()
        started = loop.time()
        try:
            with pytest.raises(ResourceBusyError) as error:
                async with players.get_playback_locks("a", "b"):
                    pytest.fail("the body ran while another command held a lock")
            waited = loop.time() - started
        finally:
            release.set()
            await holder

    # the loop clock is a float that has run for a while, so a sum of waits rounds
    assert waited == pytest.approx(PLAYER_LOCK_TIMEOUT, abs=1e-3)
    assert error.value.translation_key == "player_busy"
    assert error.value.translation_args == [busy]
    assert busy in cast("MagicMock", players.logger).warning.call_args.args
    assert _locked(players, "a", "b", "c") == []


async def test_a_player_grouped_while_its_lock_is_waited_for_is_held_with_its_new_owner() -> None:
    """A player captured by a group while its lock is awaited is held with that group's lock."""
    owners: dict[str, str] = {}
    players = _players(owners)
    release = asyncio.Event()
    held: list[list[str]] = []

    async def _hold() -> None:
        async with players.get_player_lock("b", PlayerLockPurpose.PLAYBACK):
            await release.wait()

    async def _transfer() -> None:
        async with players.get_playback_locks("b"):
            held.append(_locked(players, "b", "g"))

    holder = asyncio.create_task(_hold())
    await asyncio.sleep(0)
    transfer = asyncio.create_task(_transfer())
    await asyncio.sleep(0)
    owners["b"] = "g"
    release.set()
    await asyncio.gather(holder, transfer)

    assert held == [["b", "g"]]
    assert _locked(players, "b", "g") == []


async def test_transfers_in_opposite_directions_run_one_after_the_other() -> None:
    """
    A transfer from A to B and one from B to A that get their players at once both run, in turn.

    Both wait for their players behind other commands that end together. Neither may hold one
    player while it waits for the other, which the other one holds: both would sit out the lock
    timeout, or keep finding the other one busy.
    """
    players = _players({})
    release = asyncio.Event()
    ran: list[str] = []

    async def _hold(player_id: str) -> None:
        async with players.get_player_lock(player_id, PlayerLockPurpose.PLAYBACK):
            await release.wait()

    async def _transfer(name: str, *player_ids: str) -> None:
        async with players.get_playback_locks(*player_ids):
            ran.append(f"{name} start")
            await asyncio.sleep(0)
            ran.append(f"{name} end")

    with virtual_loop_clock():
        loop = asyncio.get_running_loop()
        holders = [asyncio.create_task(_hold(player_id)) for player_id in ("a", "b")]
        await asyncio.sleep(0)
        started = loop.time()
        transfers = asyncio.gather(_transfer("a to b", "a", "b"), _transfer("b to a", "b", "a"))
        await asyncio.sleep(0)
        release.set()
        await asyncio.gather(*holders, transfers)
        waited = loop.time() - started

    assert waited == 0
    assert ran == ["a to b start", "a to b end", "b to a start", "b to a end"]


@pytest.mark.parametrize("group_change", ["regroup", "leader hand-over"])
async def test_a_group_change_that_makes_the_source_a_member_of_the_target_meets_a_transfer(
    group_change: str,
) -> None:
    """
    A transfer from A to C meets a command on C that makes A its member: both run, in turn.

    The group change holds C and then waits for A, which the transfer has just taken: the
    transfer must not wait for C while it holds A, or both sit out the lock timeout.

    :param group_change: A regroup that takes A's lock, or a leader hand-over whose transfer of
        C's queue to A takes the locks of both.
    """
    owners: dict[str, str] = {}
    players = _players(owners)
    waits = record_lock_waits(players)
    seen_owner: list[str | None] = []

    async def _group_change() -> None:
        async with players.get_player_lock("c", PlayerLockPurpose.PLAYBACK):
            # let the transfer take A first
            await asyncio.sleep(0)
            if group_change == "regroup":
                async with players.get_player_lock("a", PlayerLockPurpose.PLAYBACK):
                    owners["a"] = "c"
            else:
                owners["a"] = "c"
                async with players.get_playback_locks("c", "a"):
                    pass

    async def _transfer() -> None:
        async with players.get_playback_locks("a", "c"):
            seen_owner.append(owners.get("a"))

    with virtual_loop_clock():
        loop = asyncio.get_running_loop()
        started = loop.time()
        await asyncio.gather(_group_change(), _transfer())
        waited = loop.time() - started

    assert waited == 0
    # the group change did wait for A while the transfer held it
    assert "playback_a" in waits
    assert seen_owner == ["c"]
    assert _locked(players, "a", "c") == []


async def test_transfers_between_unrelated_players_do_not_wait_for_each_other() -> None:
    """A transfer from C to D runs while one from A to B holds its players: no server-wide turn."""
    players = _players({})
    release = asyncio.Event()
    holding = asyncio.Event()

    async def _transfer_a_to_b() -> None:
        async with players.get_playback_locks("a", "b"):
            holding.set()
            await release.wait()

    first = asyncio.create_task(_transfer_a_to_b())
    await holding.wait()
    waits = record_lock_waits(players)

    async with players.get_playback_locks("c", "d"):
        assert _locked(players, "a", "b", "c", "d") == ["a", "b", "c", "d"]

    release.set()
    await first
    assert waits == []


async def test_a_command_holding_a_players_owner_takes_the_players_without_waiting() -> None:
    """
    A command that holds C already takes C and its member A again without waiting for itself.

    The transfer within a leader hand-over runs under the lock of the old leader, which it takes
    again.
    """
    players = _players({"a": "c"})
    waits = record_lock_waits(players)

    async with (
        players.get_player_lock("c", PlayerLockPurpose.PLAYBACK),
        players.get_playback_locks("c", "a"),
    ):
        assert _locked(players, "a", "c") == ["a", "c"]

    assert waits == []
    assert _locked(players, "a", "c") == []


async def test_a_player_that_is_busy_at_every_attempt_fails_as_busy_without_waiting_on() -> None:
    """
    A command that finds one of its players busy at each attempt gives up before the timeout.

    Each attempt waits for the player that was busy and finds the other one busy then.
    """
    players = _players({})
    step = asyncio.Event()

    async def _keep_one_busy() -> None:
        # holds B, then A, then B again: each time the one the transfer does not wait for
        async with players.get_player_lock("b", PlayerLockPurpose.PLAYBACK):
            await step.wait()
            step.clear()
        async with players.get_player_lock("a", PlayerLockPurpose.PLAYBACK):
            await step.wait()
            step.clear()
        async with players.get_player_lock("b", PlayerLockPurpose.PLAYBACK):
            await step.wait()

    async def _transfer() -> None:
        async with players.get_playback_locks("a", "b"):
            pytest.fail("the body ran while another command held a lock")

    with virtual_loop_clock():
        loop = asyncio.get_running_loop()
        holder = asyncio.create_task(_keep_one_busy())
        await asyncio.sleep(0)
        waits = record_lock_waits(players)
        started = loop.time()
        transfer = asyncio.create_task(_transfer())
        await _until(lambda: waits == ["playback_b"])
        step.set()
        await _until(lambda: waits == ["playback_b", "playback_a"])
        step.set()
        with pytest.raises(ResourceBusyError) as error:
            await transfer
        waited = loop.time() - started
        step.set()
        await holder

    assert waited == 0
    assert error.value.translation_args == ["b"]
    assert _locked(players, "a", "b") == []
