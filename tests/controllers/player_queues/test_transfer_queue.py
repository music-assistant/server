"""Tests for PlayerQueuesController.transfer_queue protocol/ungroup handling."""

from __future__ import annotations

from typing import cast
from unittest.mock import AsyncMock, MagicMock

from music_assistant_models.enums import PlaybackState

from music_assistant.controllers.player_queues import PlayerQueuesController


class _DummyACM:
    """Minimal async context manager stand-in for wait_for_player_update."""

    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *args: object) -> bool:
        return False


def _fake_controller(source_id: str, target_player: MagicMock) -> MagicMock:
    """
    Build a MagicMock standing in for the controller for transfer_queue tests.

    The source queue is idle so the capture/stop/resume path is a no-op; the test
    only exercises the pre-transfer ungroup branch.
    """
    source_queue = MagicMock()
    source_queue.state = PlaybackState.IDLE
    source_queue.resume_pos = 0
    source_queue.elapsed_time = 0
    source_queue.current_index = 0
    source_queue.current_item = None
    target_queue = MagicMock()

    fake = MagicMock()
    fake.get = MagicMock(side_effect=lambda qid: source_queue if qid == source_id else target_queue)
    fake.stop = AsyncMock()
    fake.load = AsyncMock()
    fake.resume = AsyncMock()
    fake.clear = MagicMock()
    fake.update_items = MagicMock()
    fake.mass.players.get_player = MagicMock(return_value=target_player)
    fake.mass.players.cmd_ungroup = AsyncMock()
    fake.mass.players.wait_for_player_update = MagicMock(return_value=_DummyACM())
    fake.mass.streams.is_smart_fades_active = MagicMock(return_value=False)
    return fake


async def test_transfer_queue_ad_hoc_member_ungroups_target_not_leader() -> None:
    """
    Transferring onto an ad-hoc sync member frees the target itself, not its leader.

    Ungrouping the leader would transfer leadership to a remaining member and
    recurse back into transfer_queue, so only the target may be ungrouped.
    """
    target_player = MagicMock()
    target_player.state.synced_to = "leaderA"
    target_player.state.active_group = None
    fake = _fake_controller("src", target_player)

    await PlayerQueuesController.transfer_queue(
        cast("PlayerQueuesController", fake), "src", "memberB", auto_play=False
    )

    fake.mass.players.cmd_ungroup.assert_awaited_once_with("memberB")


async def test_transfer_queue_group_member_ungroups_group() -> None:
    """Transferring onto a virtual-group member releases the group player itself."""
    target_player = MagicMock()
    target_player.state.synced_to = None
    target_player.state.active_group = "groupP"
    fake = _fake_controller("src", target_player)

    await PlayerQueuesController.transfer_queue(
        cast("PlayerQueuesController", fake), "src", "memberB", auto_play=False
    )

    fake.mass.players.cmd_ungroup.assert_awaited_once_with("groupP")
