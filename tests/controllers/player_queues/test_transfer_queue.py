"""Tests for PlayerQueuesController.transfer_queue protocol/ungroup and settings handover."""

from __future__ import annotations

import time
from typing import cast
from unittest.mock import AsyncMock, MagicMock

from music_assistant_models.enums import PlaybackState
from music_assistant_models.player_queue import PlayerQueue

from music_assistant.controllers.player_queues import PlayerQueuesController
from music_assistant.controllers.player_queues.state import PlayerQueueData


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


def _shuffle_controller(
    source_shuffle_enabled: bool,
    source_shuffle_set_at: float | None,
    target_shuffle_set_at: float | None,
) -> MagicMock:
    """
    Build a controller stand-in whose two queues carry real state records.

    Both the shuffle flag and the shuffle intent behind it live on separate per-queue objects, so
    unlike the ungroup tests above these need real queues rather than one shared mock.

    :param source_shuffle_enabled: Whether shuffle is on for the queue being handed over.
    :param source_shuffle_set_at: When the user last switched shuffle on for the source queue.
    :param target_shuffle_set_at: When the user last switched shuffle on for the target queue.
    """
    source_queue = PlayerQueue(
        queue_id="src",
        active=True,
        display_name="Src",
        available=True,
        items=0,
        shuffle_enabled=source_shuffle_enabled,
    )
    target_queue = PlayerQueue(
        queue_id="tgt", active=True, display_name="Tgt", available=True, items=0
    )

    fake = MagicMock()
    fake.get = MagicMock(side_effect=lambda qid: source_queue if qid == "src" else target_queue)
    fake._queue_data = {
        "src": PlayerQueueData(queue=source_queue, shuffle_set_at=source_shuffle_set_at),
        "tgt": PlayerQueueData(queue=target_queue, shuffle_set_at=target_shuffle_set_at),
    }
    fake.stop = AsyncMock()
    fake.load = AsyncMock()
    fake.resume = AsyncMock()
    fake._clear = MagicMock()
    fake.update_items = MagicMock()
    fake.is_smart_shuffle_active = MagicMock(return_value=False)
    target_player = MagicMock()
    target_player.state.synced_to = None
    target_player.state.active_group = None
    fake.mass.players.get_player = MagicMock(return_value=target_player)
    fake.mass.streams.is_smart_fades_active = MagicMock(return_value=False)
    return fake


async def test_transfer_queue_drops_a_stale_shuffle_intent_on_the_target() -> None:
    """
    A shuffle the user switched on for the target earlier does not shuffle the queue moved onto it.

    The transfer brings its own (off) shuffle state; leaving the target's own stamp behind would
    make the media started next read it as a "shuffle this" gesture the user never made for it.
    """
    fake = _shuffle_controller(
        source_shuffle_enabled=False,
        source_shuffle_set_at=None,
        target_shuffle_set_at=time.monotonic(),
    )

    await PlayerQueuesController.transfer_queue(
        cast("PlayerQueuesController", fake), "src", "tgt", auto_play=False
    )

    assert fake.get("tgt").shuffle_enabled is False
    assert fake._queue_data["tgt"].shuffle_set_at is None


async def test_transfer_queue_carries_the_source_shuffle_intent() -> None:
    """A shuffle switched on moments before the transfer still counts for the media started next."""
    switched_on_at = time.monotonic()
    fake = _shuffle_controller(
        source_shuffle_enabled=True,
        source_shuffle_set_at=switched_on_at,
        target_shuffle_set_at=None,
    )

    await PlayerQueuesController.transfer_queue(
        cast("PlayerQueuesController", fake), "src", "tgt", auto_play=False
    )

    assert fake.get("tgt").shuffle_enabled is True
    assert fake._queue_data["tgt"].shuffle_set_at == switched_on_at
    # the gesture is good for one play and it followed the queue, so it must not be left behind
    # to shuffle whatever gets started on the player it was moved off
    assert fake._queue_data["src"].shuffle_set_at is None
