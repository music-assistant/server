"""Task execution context helpers for long running background tasks."""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

    from music_assistant_models.background_task import BackgroundTask


@dataclass(slots=True)
class TaskExecutionContext:
    """Runtime task context exposed to long-running task code."""

    task_id: str
    get_task: Callable[[str], BackgroundTask]
    update_progress: Callable[[str, int | None, str | None], None]
    update_progress_text: Callable[[str, str | None], None]
    add_failure: Callable[[str, str], None]
    update_report: Callable[[str, str | None], None]

    @property
    def task(self) -> BackgroundTask:
        """Return the attached background task object."""
        return self.get_task(self.task_id)

    def set_progress(self, progress: int | None, text: str | None = None) -> None:
        """Set an absolute progress percentage and optional phase text."""
        self.update_progress(self.task_id, progress, text)

    def set_progress_text(self, text: str | None) -> None:
        """Update the human-readable progress text only."""
        self.update_progress_text(self.task_id, text)

    def set_progress_from_index(self, current: int, total: int, text: str | None = None) -> int:
        """Set progress from the current item index and total item count."""
        progress = calculate_progress(current, total)
        self.update_progress(self.task_id, progress, text)
        return progress

    def record_failure(self, message: str) -> None:
        """Record a non-fatal failure for the current task."""
        self.add_failure(self.task_id, message)

    def set_report(self, markdown: str | None) -> None:
        """Set the Markdown report for the current task."""
        self.update_report(self.task_id, markdown)


ACTIVE_TASK_CONTEXT: ContextVar[TaskExecutionContext | None] = ContextVar(
    "active_background_task_context",
    default=None,
)


def calculate_progress(current: int, total: int) -> int:
    """Convert the current item index and total item count into a percentage."""
    if total <= 0:
        raise ValueError("Task progress total must be > 0")
    current = max(current, 0)
    return min(int((current * 100) / total), 100)


def get_current_task_context() -> TaskExecutionContext | None:
    """Return the task context active in the current async/thread context."""
    return ACTIVE_TASK_CONTEXT.get()


def get_current_task() -> BackgroundTask | None:
    """Return the active background task for the current async/thread context."""
    if task_context := get_current_task_context():
        return task_context.task
    return None


def get_current_task_id() -> str | None:
    """Return the active task id for the current async/thread context."""
    if task_context := get_current_task_context():
        return task_context.task_id
    return None


def update_current_task_progress(progress: int | None, text: str | None = None) -> None:
    """Update progress for the task active in the current async/thread context."""
    if task_context := get_current_task_context():
        task_context.set_progress(progress, text)


def update_current_task_progress_text(text: str | None) -> None:
    """Update progress text for the task active in the current async/thread context."""
    if task_context := get_current_task_context():
        task_context.set_progress_text(text)


def update_current_task_progress_from_index(
    current: int, total: int, text: str | None = None
) -> int | None:
    """Update progress from item counts for the current async/thread context."""
    if task_context := get_current_task_context():
        return task_context.set_progress_from_index(current, total, text)
    return None


def report_current_task_failure(message: str) -> None:
    """Record a non-fatal failure for the current async/thread context."""
    if task_context := get_current_task_context():
        task_context.record_failure(message)


def set_current_task_report(markdown: str | None) -> None:
    """Set the Markdown report for the current async/thread context."""
    if task_context := get_current_task_context():
        task_context.set_report(markdown)
