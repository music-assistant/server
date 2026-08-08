"""
Tests for Cast-member readiness handling in SendspinPlayer.set_members.

Adding a Cast-bridged member waits for its Sendspin app to report ready. The caller
always learns why a member did not join, so a member failing after that wait gave up
must not also surface through the loop exception handler.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from music_assistant_models.errors import PlayerCommandFailed

from music_assistant.providers.sendspin import player as player_module
from music_assistant.providers.sendspin.player import SendspinPlayer
from tests.common import collect_loop_errors

MEMBER_ID = "cast-member"
MEMBER_NAME = "Living Room TV"


def _make_player_mock(ready: asyncio.Future[None]) -> MagicMock:
    """Create a mock leader whose single member to add is Cast-bridged."""
    mock = MagicMock()
    mock.translation_owner = "sendspin"
    mock.api.group.has_active_stream = True
    mock.api.group.add_client = AsyncMock()
    mock.api.group.remove_client = AsyncMock()
    member = MagicMock()
    member.display_name = MEMBER_NAME
    mock.mass.players.get_player.return_value = member
    bridge = MagicMock()
    bridge.reset_cast_app_ready.return_value = ready
    mock._get_cast_bridge_manager.return_value.get_bridge_by_client_id.return_value = bridge
    return mock


@pytest.mark.asyncio
async def test_member_failing_after_the_timeout_logs_no_loop_error() -> None:
    """A member reporting its failure once the wait gave up stays out of the loop handler."""
    ready: asyncio.Future[None] = asyncio.get_running_loop().create_future()
    mock = _make_player_mock(ready)
    rollback_started = asyncio.Event()
    release = asyncio.Event()

    async def _park_rollback(_api: object) -> None:
        rollback_started.set()
        await release.wait()

    mock.api.group.remove_client = AsyncMock(side_effect=_park_rollback)

    with (
        collect_loop_errors() as reported,
        patch.object(player_module, "CAST_APP_READY_TIMEOUT", 0.01),
    ):
        call = asyncio.create_task(SendspinPlayer.set_members(mock, player_ids_to_add=[MEMBER_ID]))
        await rollback_started.wait()
        # fail the member only once the readiness wait has demonstrably given up, so the
        # failure reliably lands after the waiter is gone
        ready.set_exception(PlayerCommandFailed("Cast app stopped before reporting ready."))
        ready.exception()
        release.set()
        with pytest.raises(PlayerCommandFailed, match="did not report ready"):
            await call
        await asyncio.sleep(0)

    assert reported == []


@pytest.mark.asyncio
async def test_member_failure_is_surfaced_over_the_timeout() -> None:
    """A member's own failure reaches the caller instead of the readiness timeout."""
    ready: asyncio.Future[None] = asyncio.get_running_loop().create_future()
    mock = _make_player_mock(ready)
    ready.set_exception(PlayerCommandFailed(f"Sendspin isn't supported on {MEMBER_NAME}."))
    ready.exception()

    with (
        patch.object(player_module, "CAST_APP_READY_TIMEOUT", 5.0),
        pytest.raises(PlayerCommandFailed, match="isn't supported"),
    ):
        await SendspinPlayer.set_members(mock, player_ids_to_add=[MEMBER_ID])

    mock.api.group.remove_client.assert_awaited_once()


@pytest.mark.asyncio
async def test_timeout_names_the_members_that_never_reported_ready() -> None:
    """Members still pending when the wait expires are named and then cleaned up."""
    ready: asyncio.Future[None] = asyncio.get_running_loop().create_future()
    mock = _make_player_mock(ready)

    with (
        patch.object(player_module, "CAST_APP_READY_TIMEOUT", 0.01),
        pytest.raises(PlayerCommandFailed, match=MEMBER_NAME),
    ):
        await SendspinPlayer.set_members(mock, player_ids_to_add=[MEMBER_ID])

    assert ready.cancelled()
