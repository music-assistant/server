"""Background Tasks controller package."""

from __future__ import annotations

from .context import (
    TaskExecutionContext,
    get_current_task,
    get_current_task_context,
    get_current_task_id,
    report_current_task_failure,
    set_current_task_report,
    update_current_task_progress,
    update_current_task_progress_from_index,
    update_current_task_progress_text,
)
from .controller import TasksController

__all__ = [
    "TaskExecutionContext",
    "TasksController",
    "get_current_task",
    "get_current_task_context",
    "get_current_task_id",
    "report_current_task_failure",
    "set_current_task_report",
    "update_current_task_progress",
    "update_current_task_progress_from_index",
    "update_current_task_progress_text",
]
