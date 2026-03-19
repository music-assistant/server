"""Tests for the background tasks controller."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from datetime import UTC, datetime

import pytest
from music_assistant_models.background_task import TaskSchedule
from music_assistant_models.enums import TaskScheduleType, TaskStatus

from music_assistant.controllers.tasks import (
    TasksController,
    get_current_task,
    get_current_task_id,
    report_current_task_failure,
    update_current_task_progress,
    update_current_task_progress_from_index,
    update_current_task_progress_text,
)
from music_assistant.controllers.tasks.constants import TASK_UPDATE_TIMER_ID
from music_assistant.mass import MusicAssistant


async def _wait_for_task_status(
    controller: TasksController,
    task_id: str,
    *statuses: TaskStatus,
    timeout: float = 2.0,
) -> None:
    """Wait until a managed task reaches one of the expected statuses."""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if controller.get_task(task_id).status in statuses:
            return
        await asyncio.sleep(0.01)
    msg = (
        f"Task {task_id} did not reach one of {[status.value for status in statuses]} "
        f"before timeout"
    )
    raise AssertionError(msg)


@pytest.fixture
async def tasks_controller(mass_minimal: MusicAssistant) -> AsyncGenerator[TasksController, None]:
    """Set up the background tasks controller on a minimal Music Assistant instance."""
    controller = TasksController(mass_minimal)
    mass_minimal.tasks = controller
    await controller.setup(await mass_minimal.config.get_core_config(controller.domain))
    try:
        yield controller
    finally:
        mass_minimal.cancel_timer(TASK_UPDATE_TIMER_ID)
        await controller.close()


async def test_create_task_runs_immediately(tasks_controller: TasksController) -> None:
    """Ad hoc tasks queued immediately should transition to success and capture context."""
    handler_started = asyncio.Event()
    seen_task_id: str | None = None

    async def handler() -> None:
        nonlocal seen_task_id
        current_task = get_current_task()
        assert current_task is not None
        seen_task_id = get_current_task_id()
        update_current_task_progress(42, "Processing playlist items")
        update_current_task_progress_text("Refreshing playlist")
        handler_started.set()

    task = tasks_controller.create_task(
        name="Add tracks to playlist",
        handler=handler,
        user_id="user-123",
    )

    await handler_started.wait()
    await _wait_for_task_status(tasks_controller, task.id, TaskStatus.SUCCESS)

    task = tasks_controller.get_task(task.id)
    assert seen_task_id == task.id
    assert task.status == TaskStatus.SUCCESS
    assert task.user_id == "user-123"
    assert task.last_run_user_id == "user-123"
    assert task.started_at is not None
    assert task.finished_at is not None
    assert task.progress == 42
    assert task.progress_text == "Refreshing playlist"
    assert any("Task started" in line for line in task.logs)
    assert any("Task completed successfully" in line for line in task.logs)


async def test_task_can_report_partial_success(tasks_controller: TasksController) -> None:
    """Task context helpers should surface progress and non-fatal failures."""

    async def handler() -> None:
        progress = update_current_task_progress_from_index(2, 4, "Matching playlist items")
        assert progress == 50
        report_current_task_failure("Skipped duplicate playlist item")

    task = tasks_controller.create_task(
        name="Update playlist",
        handler=handler,
        allow_retry=True,
    )

    await _wait_for_task_status(tasks_controller, task.id, TaskStatus.PARTIAL_SUCCESS)

    task = tasks_controller.get_task(task.id)
    assert task.status == TaskStatus.PARTIAL_SUCCESS
    assert task.allow_retry is True
    assert task.failure_count == 1
    assert task.failure_messages == ["Skipped duplicate playlist item"]
    assert task.progress == 50
    assert task.progress_text == "Matching playlist items"
    assert any("completed with 1 issue" in line for line in task.logs)


async def test_scheduled_task_state_is_restored(mass_minimal: MusicAssistant) -> None:
    """Scheduled tasks should restore their edited schedule and persisted runtime state."""
    controller = TasksController(mass_minimal)
    mass_minimal.tasks = controller
    await controller.setup(await mass_minimal.config.get_core_config(controller.domain))

    async def handler() -> None:
        """No-op test handler."""

    task = controller.register_scheduled_task(
        task_id="sync_spotify_artists",
        name="Sync artists for Spotify",
        handler=handler,
        schedule=TaskSchedule.hourly(every=3),
        initial_delay=1800,
    )
    controller.set_task_enabled(task.id, False)
    controller.update_task_schedule(
        task.id,
        TaskSchedule.weekly(days_of_week=[1, 3, 5], hour=7, minute=15),
    )
    task.status = TaskStatus.PARTIAL_SUCCESS
    task.last_run = datetime(2026, 3, 19, 5, 30, tzinfo=UTC)
    task.last_run_user_id = "admin-user"
    task.failure_count = 2
    task.failure_messages[:] = ["Album import failed", "Artwork lookup failed"]
    controller._persist_scheduled_task_state(controller._get_managed_task(task.id))

    persisted_states = mass_minimal.config.get_raw_core_config_value(
        controller.domain,
        "scheduled_task_states",
        {},
    )
    assert isinstance(persisted_states, dict)
    assert task.id in persisted_states

    mass_minimal.cancel_timer(TASK_UPDATE_TIMER_ID)
    await controller.close()

    restored = TasksController(mass_minimal)
    mass_minimal.tasks = restored
    await restored.setup(await mass_minimal.config.get_core_config(restored.domain))
    try:
        restored_task = restored.register_scheduled_task(
            task_id="sync_spotify_artists",
            name="Sync artists for Spotify",
            handler=handler,
            schedule=TaskSchedule.hourly(every=6),
            initial_delay=1800,
        )

        assert restored_task.status == TaskStatus.PARTIAL_SUCCESS
        assert restored_task.last_run == datetime(2026, 3, 19, 5, 30, tzinfo=UTC)
        assert restored_task.last_run_user_id == "admin-user"
        assert restored_task.failure_count == 2
        assert restored_task.failure_messages == [
            "Album import failed",
            "Artwork lookup failed",
        ]
        assert restored_task.schedule is not None
        assert restored_task.schedule.enabled is False
        assert restored_task.schedule.type == TaskScheduleType.WEEKLY
        assert restored_task.schedule.days_of_week == [1, 3, 5]
        assert restored_task.schedule.hour == 7
        assert restored_task.schedule.minute == 15
        assert restored_task.next_run is None
    finally:
        mass_minimal.cancel_timer(TASK_UPDATE_TIMER_ID)
        await restored.close()
