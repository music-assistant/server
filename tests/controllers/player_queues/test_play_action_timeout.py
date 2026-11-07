"""Playback deadlines cancel the lock holder and let subsequent commands run."""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, Mock

import pytest
from music_assistant_models.errors import PlayerCommandFailed
from music_assistant_models.player_queue import PlayerQueue

from music_assistant.constants import ATTR_PLAY_ACTION_IN_PROGRESS
from music_assistant.controllers.player_queues import PlayerQueuesController
from music_assistant.controllers.player_queues import helpers as queue_helpers
from music_assistant.controllers.player_queues.helpers import handle_play_action
from music_assistant.controllers.player_queues.state import PlayerQueueData
from tests.common import bare_player_controller


@pytest.fixture
def controller(monkeypatch: pytest.MonkeyPatch) -> PlayerQueuesController:
    """Build a queue using real playback locks and a short execution deadline."""
    monkeypatch.setattr(queue_helpers, "PLAY_ACTION_TIMEOUT", 0.05)
    ctrl = PlayerQueuesController.__new__(PlayerQueuesController)
    ctrl.mass = MagicMock()
    ctrl.mass.players = bare_player_controller()
    ctrl.signal_update = Mock()  # type: ignore[method-assign]
    ctrl._queue_data = {
        "q1": PlayerQueueData(
            queue=PlayerQueue(
                queue_id="q1", display_name="Living Room", available=True, active=True, items=0
            )
        )
    }
    return ctrl


def _assert_released(ctrl: PlayerQueuesController) -> None:
    """Check that the queue is no longer busy and no task owns its playback lock."""
    data = ctrl._queue_data["q1"]
    assert not data.queue.extra_attributes[ATTR_PLAY_ACTION_IN_PROGRESS]
    assert data.play_action_refcount == 0
    assert not ctrl.mass.players._player_command_locks["playback_q1"].locked()
    assert not ctrl.mass.players._task_held_locks


async def test_timeout_cancels_provider_work_and_unblocks_waiter(
    controller: PlayerQueuesController,
) -> None:
    """A hung multi-source lookup is cancelled before the next command takes the lock."""
    started = [asyncio.Event(), asyncio.Event()]
    cancelled: set[int] = set()

    async def provider_lookup(index: int) -> None:
        started[index].set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.add(index)

    @handle_play_action
    async def resolve(self: PlayerQueuesController, queue_id: str) -> None:
        assert self._queue_data[queue_id].play_action_refcount == 1
        await asyncio.gather(provider_lookup(0), provider_lookup(1))

    @handle_play_action
    async def following_command(self: PlayerQueuesController, queue_id: str) -> str:
        assert self._queue_data[queue_id].play_action_refcount == 1
        assert cancelled == {0, 1}
        return "ready"

    async with asyncio.timeout(2):
        async with asyncio.TaskGroup() as tasks:

            async def expect_timeout() -> None:
                with pytest.raises(PlayerCommandFailed, match=r"resolve.*q1.*timed out") as exc:
                    await resolve(controller, "q1")
                assert exc.value.translation_key == "play_action_timeout"
                assert exc.value.translation_owner == "core.player_queues"

            tasks.create_task(expect_timeout())
            await asyncio.gather(*(event.wait() for event in started))
            assert await following_command(controller, "q1") == "ready"

    _assert_released(controller)


async def test_nested_successful_calls_do_not_reset_deadline(
    controller: PlayerQueuesController,
) -> None:
    """Many individually successful calls still share one overall time budget."""
    calls = 0

    @handle_play_action
    async def page(self: PlayerQueuesController, queue_id: str) -> None:
        nonlocal calls
        assert self._queue_data[queue_id].play_action_refcount == 2
        calls += 1
        await asyncio.sleep(0)

    @handle_play_action
    async def catalogue(self: PlayerQueuesController, queue_id: str) -> None:
        while True:
            await page(self, queue_id)

    async with asyncio.timeout(2):
        with pytest.raises(PlayerCommandFailed, match=r"catalogue.*timed out"):
            await catalogue(controller, "q1")

    assert calls > 1
    _assert_released(controller)


async def test_provider_timeout_keeps_its_original_error(
    controller: PlayerQueuesController,
) -> None:
    """A provider's own timeout must not be reported as the queue's deadline expiring."""
    error = TimeoutError("provider request timed out")

    @handle_play_action
    async def resolve(self: PlayerQueuesController, queue_id: str) -> None:
        assert queue_id in self._queue_data
        raise error

    with pytest.raises(TimeoutError) as exc:
        await resolve(controller, "q1")

    assert exc.value is error
    _assert_released(controller)


async def test_external_cancellation_is_preserved(controller: PlayerQueuesController) -> None:
    """Shutdown or caller cancellation still propagates as cancellation, with cleanup."""
    started = asyncio.Event()

    @handle_play_action
    async def resolve(self: PlayerQueuesController, queue_id: str) -> None:
        assert queue_id in self._queue_data
        started.set()
        await asyncio.Event().wait()

    async with asyncio.timeout(2):
        task = asyncio.create_task(resolve(controller, "q1"))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    _assert_released(controller)
