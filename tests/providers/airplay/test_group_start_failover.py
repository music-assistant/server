"""Tests for AirPlay group starts that go on without the members that fail."""

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from music_assistant.providers.airplay.stream_session import AirPlayStreamSession


def _speaker(protocol_id: str, leader_of: list[str] | None = None) -> MagicMock:
    """
    Create a mock AirPlay speaker that is its own player.

    :param protocol_id: The protocol id.
    :param leader_of: The members it leads, when it is the group leader.
    """
    player = MagicMock()
    player.protocol_id = protocol_id
    player.display_name = protocol_id
    player.owner = player
    player.group_members = leader_of or []
    player.synced_to = None if leader_of else "leader"
    return player


def _set_speaker(protocol_id: str) -> MagicMock:
    """
    Create a mock HomePod that plays as a speaker of an Apple TV's set.

    :param protocol_id: The protocol id.
    """
    player = _speaker(protocol_id)
    player.owner = MagicMock(protocol_id="apple_tv")
    player.synced_to = None
    return player


def _session(members: list[MagicMock]) -> AirPlayStreamSession:
    """
    Create a stream session over the given members.

    :param members: The session's sync clients, the leader first.
    """
    return AirPlayStreamSession(MagicMock(), list(members), MagicMock(), MagicMock())


def _failing_for(failing: set[str]) -> Any:
    """
    Return a start step that fails for the given members and succeeds for the rest.

    :param failing: The ids of the members whose step fails.
    """

    async def step(player: MagicMock) -> None:
        if player.protocol_id in failing:
            raise TimeoutError(f"{player.protocol_id} did not connect")

    return step


async def test_a_failing_member_is_returned_and_the_rest_finish() -> None:
    """A member synced to the leader that fails is returned; the others finish the step."""
    leader = _speaker("leader", leader_of=["leader", "kitchen", "bedroom"])
    kitchen = _speaker("kitchen")
    bedroom = _speaker("bedroom")
    session = _session([leader, kitchen, bedroom])
    failed = await session._run_start_step("connect to its device", _failing_for({"bedroom"}))
    assert failed == [bedroom]


async def test_a_dropped_member_is_removed_and_handled_as_a_lost_stream() -> None:
    """A dropped member leaves the session, and its stream's loss handling ungroups it."""
    leader = _speaker("leader", leader_of=["leader", "bedroom"])
    bedroom = _speaker("bedroom")
    stream = bedroom.stream
    session = _session([leader, bedroom])
    with patch.object(session, "remove_client", new_callable=AsyncMock) as remove_client:
        await session._drop_failed_members([bedroom], "connect to its device")
    remove_client.assert_awaited_once()
    assert remove_client.await_args is not None
    assert remove_client.await_args.args[0] is bedroom
    stream.handle_stream_loss.assert_called_once_with()


async def test_a_failing_leader_fails_the_start_without_waiting_for_the_rest() -> None:
    """The leader owns the session, so its failure ends the start and cancels the others."""
    leader = _speaker("leader", leader_of=["leader", "bedroom"])
    bedroom = _speaker("bedroom")
    session = _session([leader, bedroom])
    bedroom_cancelled = asyncio.Event()

    async def step(player: Any) -> None:
        if player is leader:
            raise TimeoutError("leader did not connect")
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            bedroom_cancelled.set()
            raise

    with pytest.raises(TimeoutError, match="leader did not connect"):
        await asyncio.wait_for(session._run_start_step("connect to its device", step), timeout=5)
    assert bedroom_cancelled.is_set()


async def test_a_failing_speaker_of_a_set_fails_the_start() -> None:
    """A set plays on all of its speakers or not at all, so one failing speaker fails it."""
    left = _set_speaker("left")
    right = _set_speaker("right")
    session = _session([left, right])
    with pytest.raises(TimeoutError, match="right did not connect"):
        await session._run_start_step("connect to its device", _failing_for({"right"}))


async def test_every_member_failing_fails_the_start() -> None:
    """With no member left to play, the start fails."""
    kitchen = _speaker("kitchen")
    bedroom = _speaker("bedroom")
    session = _session([kitchen, bedroom])
    with pytest.raises(TimeoutError):
        await session._run_start_step("connect to its device", _failing_for({"kitchen", "bedroom"}))


async def test_all_members_connecting_fail_nobody() -> None:
    """A step every member finishes returns no failed member."""
    leader = _speaker("leader", leader_of=["leader", "bedroom"])
    bedroom = _speaker("bedroom")
    session = _session([leader, bedroom])
    assert await session._run_start_step("connect to its device", _failing_for(set())) == []
