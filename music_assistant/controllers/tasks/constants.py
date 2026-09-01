"""Constants for the background tasks controller."""

from __future__ import annotations

from contextvars import ContextVar

DEFAULT_MAX_CONCURRENT_TASKS = 2
DEFAULT_TASK_LOG_LINES = 250
DEFAULT_TASK_FAILURE_MESSAGES = 25
MAX_FINISHED_TASK_HISTORY = 100
TASK_LIFECYCLE_UPDATE_DEBOUNCE = 0.25
TASK_ACTIVITY_UPDATE_INTERVAL = 10.0
TASK_CANCEL_TIMEOUT = 10.0
TASK_UPDATE_TIMER_ID = "managed_tasks_signal_event"
TASK_STATE_CONFIG_KEY = "scheduled_task_states"

ACTIVE_TASK_ID: ContextVar[str | None] = ContextVar("active_background_task_id", default=None)
