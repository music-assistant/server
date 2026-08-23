"""Runtime models for the background tasks controller."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from music_assistant_models.enums import TaskStatus

from .constants import DEFAULT_TASK_LOG_LINES

if TYPE_CHECKING:
    from music_assistant_models.background_task import BackgroundTask


@dataclass
class ManagedTask:
    """Runtime state for a managed background task."""

    task_info: BackgroundTask
    handler: Callable[[], Awaitable[Any]]
    max_log_lines: int = DEFAULT_TASK_LOG_LINES
    current_task: asyncio.Task[Any] | None = None
    timer_delay: float | None = None
    priority: bool = False
    removed: bool = False
    clear_persisted_state_on_remove: bool = True
    run_id: int = 0

    @property
    def is_scheduled(self) -> bool:
        """Return if this task has a recurring schedule."""
        return self.task_info.schedule is not None

    @property
    def is_active(self) -> bool:
        """Return if this task is pending or running."""
        return self.task_info.status in (TaskStatus.PENDING, TaskStatus.RUNNING)

    @property
    def can_remove(self) -> bool:
        """Return if this task can be removed from history."""
        return not self.is_scheduled and not self.is_active
