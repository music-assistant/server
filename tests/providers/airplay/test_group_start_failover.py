"""Tests for AirPlay group starts that go on without the members that fail."""

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from music_assistant.providers.airplay.stream_session import AirPlayStreamSession


def _member(player_id: str, leader_of: list[str] | None = None) -> MagicMock:
    """
    Create a mock AirPlay member of a group.

    :param player_id: The player id.
    :param leader_of: The members it leads, when it is the group leader.
    """
    player = MagicMock()
    player.player_id = player_id
    player.display_name = player_id
    player.group_members = leader_of or []
    player.synced_to = None if leader_of else "leader"
    return player


def _session(members: list[MagicMock]) -> AirPlayStreamSession:
    """
    Create a stream session over the given members.

    :param members: The session's sync clients, the leader first.
    """
    prov = MagicMock()
    session = AirPlayStreamSession(prov, list(members), MagicMock(), MagicMock())
    session.mass.players.get_player = MagicMock(
        side_effect=lambda pid: next((m for m in members if m.player_id == pid), None)
    )
    return session


def _failing_for(failing: set[str]) -> Any:
    """
    Return a start step that fails for the given members and succeeds for the rest.

    :param failing: The ids of the members whose step fails.
    """

    async def step(player: MagicMock) -> None:
        if player.player_id in failing:
            raise TimeoutError(f"{player.player_id} did not connect")

    return step


async def test_a_failing_member_is_left_out_and_the_rest_start() -> None:
    """A member that fails is dropped, ungrouped with its re-join scheduled."""
    leader = _member("leader", leader_of=["leader", "speaker", "apple_tv"])
    speaker = _member("speaker")
    apple_tv = _member("apple_tv")
    session = _session([leader, speaker, apple_tv])
    with patch.object(session, "remove_client", new_callable=AsyncMock) as remove_client:
        await session._start_step_dropping_failures(
            "connect to its device", _failing_for({"apple_tv"})
        )
    remove_client.assert_awaited_once()
    assert remove_client.await_args is not None
    assert remove_client.await_args.args[0] is apple_tv
    session.mass.players.cmd_set_members.assert_called_once_with(
        "leader", player_ids_to_remove=["apple_tv"]
    )
    apple_tv.schedule_group_rejoin.assert_called_once()
    assert apple_tv.schedule_group_rejoin.call_args.args[0][0] == "leader"


async def test_a_failing_leader_fails_the_start_without_waiting_for_the_rest() -> None:
    """The leader owns the session, so its failure ends the start and cancels the others."""
    leader = _member("leader", leader_of=["leader", "speaker"])
    speaker = _member("speaker")
    session = _session([leader, speaker])
    speaker_cancelled = asyncio.Event()

    async def step(player: MagicMock) -> None:
        if player is leader:
            raise TimeoutError("leader did not connect")
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            speaker_cancelled.set()
            raise

    with pytest.raises(TimeoutError, match="leader did not connect"):
        await asyncio.wait_for(
            session._start_step_dropping_failures("connect to its device", step), timeout=5
        )
    assert speaker_cancelled.is_set()


async def test_every_member_failing_fails_the_start() -> None:
    """With no member left to play, the start fails."""
    leader = _member("leader", leader_of=["leader", "speaker"])
    leader.group_members = []  # e.g. the leader's own grouping already dropped
    speaker = _member("speaker")
    session = _session([leader, speaker])
    with (
        patch.object(session, "remove_client", new_callable=AsyncMock),
        pytest.raises(TimeoutError),
    ):
        await session._start_step_dropping_failures(
            "connect to its device", _failing_for({"leader", "speaker"})
        )


async def test_all_members_connecting_drops_nobody() -> None:
    """A start where every member connects leaves the session untouched."""
    leader = _member("leader", leader_of=["leader", "speaker"])
    speaker = _member("speaker")
    session = _session([leader, speaker])
    with patch.object(session, "remove_client", new_callable=AsyncMock) as remove_client:
        await session._start_step_dropping_failures("connect to its device", _failing_for(set()))
    remove_client.assert_not_awaited()
    session.mass.players.cmd_set_members.assert_not_called()
