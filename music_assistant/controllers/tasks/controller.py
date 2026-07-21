"""Controller that manages long running background tasks."""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from collections.abc import Awaitable, Callable
from contextlib import suppress
from datetime import datetime
from functools import partial
from threading import get_ident
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from music_assistant_models.auth import Scope, User
from music_assistant_models.background_task import (
    BackgroundTask,
    TaskMetadata,
    TaskMetadataValue,
    TaskSchedule,
)
from music_assistant_models.enums import EventType, TaskStatus
from music_assistant_models.errors import InvalidDataError

from music_assistant.constants import CONF_ENTRY_MAX_CONCURRENT_TASKS, CONF_MAX_CONCURRENT_TASKS
from music_assistant.controllers.webserver.helpers.auth_middleware import (
    get_current_user,
    has_scope,
)
from music_assistant.helpers.api import api_command
from music_assistant.models.core_controller import CoreController

from .constants import (
    ACTIVE_TASK_ID,
    DEFAULT_MAX_CONCURRENT_TASKS,
    DEFAULT_TASK_FAILURE_MESSAGES,
    DEFAULT_TASK_LOG_LINES,
    MAX_FINISHED_TASK_HISTORY,
    TASK_ACTIVITY_UPDATE_INTERVAL,
    TASK_LIFECYCLE_UPDATE_DEBOUNCE,
    TASK_STATE_CONFIG_KEY,
    TASK_UPDATE_TIMER_ID,
)
from .context import ACTIVE_TASK_CONTEXT, TaskExecutionContext
from .helpers import (
    TaskLogHandler,
    format_task_log_line,
    get_task_schedule_delay,
    get_task_timer_id,
    get_visible_tasks,
    merge_task_schedule_state,
    restore_task_state,
    serialize_task_schedule_state,
    serialize_task_state,
    trim_finished_history,
    utcnow,
)
from .models import ManagedTask

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigEntry, ConfigValueType, CoreConfig

    from music_assistant import MusicAssistant
    from music_assistant.helpers.json import SerializableType


class TasksController(CoreController):
    """Controller that manages long running background tasks."""

    domain = "tasks"

    def __init__(self, mass: MusicAssistant) -> None:
        """Initialize controller."""
        super().__init__(mass)
        self.manifest.name = "Background tasks"
        self.manifest.description = "Manage long running scheduled, user and system tasks."
        self.manifest.icon = "playlist-play"
        self._tasks: dict[str, ManagedTask] = {}
        self._pending_task_ids: deque[str] = deque()
        self._log_handler: TaskLogHandler | None = None
        self._max_concurrent_tasks = DEFAULT_MAX_CONCURRENT_TASKS
        self._last_task_update_signal = 0.0
        self._scheduled_task_update_at: float | None = None

    async def setup(self, config: CoreConfig) -> None:
        """Set up the controller."""
        self.config = config
        self._max_concurrent_tasks = config.get_value(
            CONF_MAX_CONCURRENT_TASKS, DEFAULT_MAX_CONCURRENT_TASKS
        )
        if self._log_handler is None:
            self._log_handler = TaskLogHandler(self.mass, self._append_task_log)
            logging.getLogger().addHandler(self._log_handler)

    async def get_config_entries(
        self,
        action: str | None = None,
        values: dict[str, ConfigValueType] | None = None,
    ) -> tuple[ConfigEntry, ...]:
        """Return all Config Entries for this core module (if any)."""
        del action, values
        return (CONF_ENTRY_MAX_CONCURRENT_TASKS,)

    async def close(self) -> None:
        """Clean up the controller."""
        for task_id in list(self._tasks):
            self._unregister_task(task_id, clear_persisted_state=False)
        if self._log_handler is not None:
            logging.getLogger().removeHandler(self._log_handler)
            self._log_handler = None

    async def get_diagnostics(self) -> dict[str, SerializableType]:
        """Return diagnostics info for this controller to include in diagnostics reports."""
        by_status: dict[str, int] = {}
        for managed in self._tasks.values():
            status = managed.task_info.status.value
            by_status[status] = by_status.get(status, 0) + 1
        return {
            "total": len(self._tasks),
            "by_status": by_status,
            "scheduled": sum(managed.is_scheduled for managed in self._tasks.values()),
            "pending_queue": len(self._pending_task_ids),
            "max_concurrent": self._max_concurrent_tasks,
        }

    @api_command("tasks/list", required_scope=Scope.SYSTEM_READ)
    def list_tasks(self) -> list[BackgroundTask]:
        """Return all visible managed tasks."""
        return self.list_tasks_for_user(get_current_user())

    def list_tasks_for_user(self, user: User | None) -> list[BackgroundTask]:
        """Return tasks visible to the given user."""
        return [managed.task_info for managed in get_visible_tasks(self._tasks.values(), user)]

    @api_command("tasks/get", required_scope=Scope.SYSTEM_READ)
    def get_task(self, task_id: str) -> BackgroundTask:
        """Return a single task by id."""
        return self._get_visible_managed_task(task_id, get_current_user()).task_info

    @api_command("tasks/log", required_scope=Scope.SYSTEM_READ)
    def get_task_log(self, task_id: str) -> str:
        """Return the log buffer for a single task."""
        return "\n".join(self._get_visible_managed_task(task_id, get_current_user()).task_info.logs)

    @api_command("tasks/run", required_scope=Scope.SYSTEM_MANAGE)
    def run_task(self, task_id: str) -> BackgroundTask:
        """Queue a task for immediate execution."""
        managed = self._get_managed_task(task_id)
        if not managed.is_scheduled:
            raise InvalidDataError(f"Task {task_id} can not be run manually")
        user = get_current_user()
        self._queue_task(
            managed,
            reset_logs=True,
            run_user_id=user.user_id if user else None,
        )
        return managed.task_info

    @api_command("tasks/retry", required_scope=Scope.SYSTEM_MANAGE)
    def retry_task(self, task_id: str) -> BackgroundTask:
        """Retry a failed or cancelled task."""
        managed = self._get_managed_task(task_id)
        if not managed.task_info.allow_retry:
            raise InvalidDataError(f"Task {task_id} can not be retried")
        if managed.task_info.status not in (
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
            TaskStatus.PARTIAL_SUCCESS,
        ):
            raise InvalidDataError(f"Task {task_id} is not in a retryable state")
        user = get_current_user()
        self._queue_task(
            managed,
            reset_logs=True,
            run_user_id=user.user_id if user else None,
        )
        return managed.task_info

    @api_command("tasks/cancel", required_scope=Scope.SYSTEM_MANAGE)
    def cancel_task(self, task_id: str) -> BackgroundTask:
        """Cancel a pending or running task."""
        managed = self._get_managed_task(task_id)
        self._cancel_managed_task(managed)
        return managed.task_info

    @api_command("tasks/set_enabled", required_scope=Scope.SYSTEM_MANAGE)
    def set_task_enabled(self, task_id: str, enabled: bool) -> BackgroundTask:
        """Enable or disable automatic scheduling for a recurring task."""
        managed = self._get_managed_task(task_id)
        if not managed.task_info.schedule:
            raise InvalidDataError(f"Task {task_id} does not have a recurring schedule")
        managed.task_info.schedule.enabled = enabled
        self.mass.cancel_timer(get_task_timer_id(task_id))
        if enabled:
            if not managed.is_active:
                self._schedule_managed_task(managed)
            else:
                managed.task_info.updated_at = utcnow()
                self._persist_scheduled_task_state(managed)
                self._schedule_task_update(force=True)
        else:
            managed.timer_delay = None
            managed.task_info.next_run = None
            managed.task_info.updated_at = utcnow()
            self._persist_scheduled_task_state(managed)
            self._schedule_task_update(force=True)
        return managed.task_info

    @api_command("tasks/update_schedule", required_scope=Scope.SYSTEM_MANAGE)
    def update_task_schedule(
        self,
        task_id: str,
        schedule: TaskSchedule,
    ) -> BackgroundTask:
        """Update the schedule definition for a recurring task."""
        managed = self._get_managed_task(task_id)
        current_schedule = managed.task_info.schedule
        if current_schedule is None:
            raise InvalidDataError(f"Task {task_id} does not have a recurring schedule")
        managed.task_info.schedule = self._resolve_updated_schedule(
            current_schedule=current_schedule,
            schedule=schedule,
        )
        self.mass.cancel_timer(get_task_timer_id(task_id))
        managed.task_info.next_run = None
        managed.task_info.updated_at = utcnow()
        if not managed.is_active:
            self._schedule_managed_task(managed)
        else:
            self._persist_scheduled_task_state(managed)
            self._schedule_task_update(force=True)
        return managed.task_info

    @api_command("tasks/remove", required_scope=Scope.SYSTEM_MANAGE)
    def remove_task(self, task_id: str) -> None:
        """Remove a finished task from history."""
        managed = self._get_managed_task(task_id)
        if not managed.can_remove:
            raise InvalidDataError(f"Task {task_id} can not be removed")
        self._tasks.pop(task_id, None)
        self._schedule_task_update(force=True)

    @api_command("tasks/clear_finished", required_scope=Scope.SYSTEM_MANAGE)
    def clear_finished_tasks(self) -> None:
        """Remove finished non-scheduled tasks from history."""
        for task_id in [task_id for task_id, task in self._tasks.items() if task.can_remove]:
            self._tasks.pop(task_id, None)
        self._schedule_task_update(force=True)

    def run_background_task(  # noqa: PLR0913
        self,
        *,
        name: str,
        handler: Callable[[], Awaitable[Any]],
        task_id: str | None = None,
        translation_key: str | None = None,
        translation_args: list[Any] | None = None,
        translation_owner: str | None = None,
        user_id: str | None = None,
        metadata: TaskMetadata | None = None,
        allow_retry: bool = False,
        allow_cancel: bool = True,
        priority: bool = False,
        max_log_lines: int = DEFAULT_TASK_LOG_LINES,
    ) -> BackgroundTask:
        """
        Create and queue a long running background task.

        :param name: Human-readable display name for the task.
        :param handler: Async callable that performs the actual work.
        :param task_id: Optional deterministic id. Auto-generated if not provided.
            When a task with the same id already exists and is active,
            the existing task is returned as-is. If inactive, it is replaced.
        :param translation_key: Optional translation key for localised task names.
        :param translation_args: Optional arguments for the translation key.
        :param translation_owner: Owner namespace the (relative) translation_key resolves under,
            e.g. the calling module's ``translation_owner`` ("core.<domain>"/"provider.<domain>").
        :param user_id: Optional user id that initiated the task.
        :param metadata: Optional key/value metadata attached to the task.
        :param allow_retry: Whether the task can be retried after failure.
        :param allow_cancel: Whether the task can be cancelled by a user.
        :param priority: When True, the task is inserted at the front of the pending queue
            so it runs before lower-priority tasks. Use this for user-initiated actions that
            should not be delayed by background work such as metadata refreshes.
        :param max_log_lines: Maximum number of log lines to retain for this task.
        """
        resolved_task_id = task_id or uuid4().hex
        if existing := self._tasks.get(resolved_task_id):
            if existing.is_active:
                return existing.task_info
            self._tasks.pop(resolved_task_id, None)

        task_info = BackgroundTask(
            id=resolved_task_id,
            name=name,
            status=TaskStatus.IDLE,
            translation_key=_namespaced_translation_key(translation_key),
            translation_args=translation_args or [],
            translation_owner=translation_owner,
            user_id=user_id,
            metadata=metadata or {},
            allow_retry=allow_retry,
            allow_cancel=allow_cancel,
        )
        managed = ManagedTask(
            task_info=task_info,
            handler=handler,
            priority=priority,
            max_log_lines=max_log_lines,
        )
        self._tasks[task_info.id] = managed
        self._queue_task(managed, reset_logs=True, run_user_id=user_id)
        return task_info

    def register_scheduled_task(  # noqa: PLR0913
        self,
        *,
        task_id: str,
        name: str,
        handler: Callable[[], Awaitable[Any]],
        schedule: TaskSchedule,
        initial_delay: float | None = None,
        translation_key: str | None = None,
        translation_args: list[Any] | None = None,
        translation_owner: str | None = None,
        metadata: TaskMetadata | None = None,
        allow_retry: bool = False,
        allow_cancel: bool = True,
    ) -> BackgroundTask:
        """
        Register or update a recurring scheduled task.

        :param task_id: Deterministic id for the scheduled task.
        :param name: Human-readable display name for the task.
        :param handler: Async callable that performs the actual work.
        :param schedule: Schedule definition controlling when the task runs.
        :param initial_delay: Optional delay in seconds before the first run.
        :param translation_key: Optional translation key for localised task names.
        :param translation_args: Optional arguments for the translation key.
        :param translation_owner: Owner namespace the (relative) translation_key resolves under,
            e.g. the calling module's ``translation_owner`` ("core.<domain>"/"provider.<domain>").
        :param metadata: Optional key/value metadata attached to the task.
        :param allow_retry: Whether the task can be retried after failure.
        :param allow_cancel: Whether the task can be cancelled by a user.
        """
        resolved_schedule = self._resolve_schedule(schedule=schedule)
        if existing := self._tasks.get(task_id):
            task_info = existing.task_info
            task_info.name = name
            task_info.translation_key = _namespaced_translation_key(translation_key)
            task_info.translation_args = translation_args or []
            task_info.translation_owner = translation_owner
            task_info.metadata = metadata or {}
            if task_info.schedule is not None:
                task_info.schedule = merge_task_schedule_state(
                    resolved_schedule,
                    serialize_task_schedule_state(task_info.schedule) or {},
                )
            else:
                task_info.schedule = resolved_schedule
            task_info.allow_retry = allow_retry
            task_info.allow_cancel = allow_cancel
            task_info.updated_at = utcnow()
            existing.handler = handler
            existing.removed = False
            if not existing.is_active:
                self._schedule_managed_task(existing, initial_delay)
            self._persist_scheduled_task_state(existing)
            self._schedule_task_update(force=True)
            return task_info

        task_info = BackgroundTask(
            id=task_id,
            name=name,
            status=TaskStatus.IDLE,
            translation_key=_namespaced_translation_key(translation_key),
            translation_args=translation_args or [],
            translation_owner=translation_owner,
            metadata=metadata or {},
            schedule=resolved_schedule,
            allow_retry=allow_retry,
            allow_cancel=allow_cancel,
        )
        self._restore_scheduled_task_state(task_info)
        managed = ManagedTask(task_info=task_info, handler=handler)
        self._tasks[task_id] = managed
        self._schedule_managed_task(managed, initial_delay)
        self._persist_scheduled_task_state(managed)
        self._schedule_task_update(force=True)
        return task_info

    def unregister_scheduled_task(self, task_id: str, clear_persisted_state: bool = True) -> None:
        """
        Unregister a recurring scheduled task and cancel any active work.

        If a stale ad-hoc task exists with the same deterministic task id,
        remove that too so provider/task re-registration can recover cleanly.

        :param task_id: The id of the scheduled task to unregister.
        :param clear_persisted_state: Whether to remove persisted state from config.
        """
        self._unregister_task(task_id, clear_persisted_state)

    def update_task_progress(
        self, task_id: str, progress: int | None, text: str | None = None
    ) -> None:
        """
        Update progress for a task.

        :param task_id: The id of the task to update.
        :param progress: Progress percentage (0-100) or None to clear.
        :param text: Optional progress description text.
        """
        if get_ident() != self.mass.loop_thread_id:
            self.mass.loop.call_soon_threadsafe(self.update_task_progress, task_id, progress, text)
            return
        if not (managed := self._tasks.get(task_id)):
            return
        managed.task_info.progress = self._validate_progress(progress)
        managed.task_info.progress_text = text
        managed.task_info.updated_at = utcnow()
        self._schedule_task_update()

    def update_task_progress_text(self, task_id: str, text: str | None) -> None:
        """
        Update progress text for a task without changing the percentage.

        :param task_id: The id of the task to update.
        :param text: Progress description text or None to clear.
        """
        if get_ident() != self.mass.loop_thread_id:
            self.mass.loop.call_soon_threadsafe(self.update_task_progress_text, task_id, text)
            return
        if not (managed := self._tasks.get(task_id)):
            return
        managed.task_info.progress_text = text
        managed.task_info.updated_at = utcnow()
        self._schedule_task_update()

    def update_current_task_progress(self, progress: int | None, text: str | None = None) -> None:
        """
        Update progress for the task active in the current async context.

        :param progress: Progress percentage (0-100) or None to clear.
        :param text: Optional progress description text.
        """
        if not (task_id := ACTIVE_TASK_ID.get()):
            return
        self.update_task_progress(task_id, progress, text)

    def add_task_failure(self, task_id: str, message: str) -> None:
        """
        Record a non-fatal failure for a task.

        :param task_id: The id of the task to record the failure on.
        :param message: Human-readable failure description.
        """
        if get_ident() != self.mass.loop_thread_id:
            self.mass.loop.call_soon_threadsafe(self.add_task_failure, task_id, message)
            return
        if not (managed := self._tasks.get(task_id)):
            return
        task_info = managed.task_info
        task_info.failure_count += 1
        message = message.strip()
        if message:
            task_info.failure_messages.append(message)
            if len(task_info.failure_messages) > DEFAULT_TASK_FAILURE_MESSAGES:
                del task_info.failure_messages[
                    : len(task_info.failure_messages) - DEFAULT_TASK_FAILURE_MESSAGES
                ]
        task_info.updated_at = utcnow()
        self._schedule_task_update()

    def get_tasks_by_metadata(self, **metadata: TaskMetadataValue) -> list[BackgroundTask]:
        """
        Return tasks matching the given metadata key/value pairs.

        :param metadata: Key/value pairs that must all match on a task's metadata.
        """
        result: list[BackgroundTask] = []
        for managed in self._tasks.values():
            if all(managed.task_info.metadata.get(key) == value for key, value in metadata.items()):
                result.append(managed.task_info)
        return result

    def _unregister_task(self, task_id: str, clear_persisted_state: bool = True) -> None:
        """Unregister a managed task and cancel any active work."""
        if not (managed := self._tasks.get(task_id)):
            return
        managed.removed = True
        managed.clear_persisted_state_on_remove = clear_persisted_state
        self.mass.cancel_timer(get_task_timer_id(task_id))
        self._remove_from_pending(task_id)
        if managed.current_task and not managed.current_task.done():
            managed.current_task.cancel()
            return
        self._tasks.pop(task_id, None)
        if clear_persisted_state:
            self._clear_scheduled_task_state(task_id)
        self._schedule_task_update(force=True)

    def _get_managed_task(self, task_id: str) -> ManagedTask:
        """Return runtime state for a managed task."""
        if not (managed := self._tasks.get(task_id)):
            raise InvalidDataError(f"Task {task_id} not found")
        return managed

    def _get_visible_managed_task(self, task_id: str, user: User | None) -> ManagedTask:
        """Return a managed task if it is visible to the given user."""
        managed = self._get_managed_task(task_id)
        if (
            user is not None
            and not has_scope(user, Scope.SYSTEM_MANAGE)
            and managed.task_info.user_id != user.user_id
        ):
            raise InvalidDataError(f"Task {task_id} not found")
        return managed

    def _append_task_log(self, task_id: str, line: str) -> None:
        """Append a log line to a task."""
        if get_ident() != self.mass.loop_thread_id:
            self.mass.loop.call_soon_threadsafe(self._append_task_log, task_id, line)
            return
        if not (managed := self._tasks.get(task_id)):
            return
        logs = managed.task_info.logs
        logs.append(line)
        if len(logs) > managed.max_log_lines:
            del logs[: len(logs) - managed.max_log_lines]
        managed.task_info.updated_at = utcnow()
        self._schedule_task_update()

    def _append_task_lifecycle_log(
        self,
        task_id: str,
        *,
        level: int,
        message: str,
        created_at: datetime | None = None,
    ) -> None:
        """Append a synthetic lifecycle log line using the default task log format."""
        self._append_task_log(
            task_id,
            format_task_log_line(
                message,
                level=level,
                logger_name=self.logger.name,
                created_at=created_at,
            ),
        )

    def _mark_task_running(self, managed: ManagedTask) -> None:
        """Update task state for the start of a managed run."""
        task_info = managed.task_info
        task_info.status = TaskStatus.RUNNING
        task_info.started_at = utcnow()
        task_info.last_run = task_info.started_at
        task_info.updated_at = task_info.started_at
        self._persist_scheduled_task_state(managed)
        self._append_task_lifecycle_log(
            task_info.id,
            level=logging.INFO,
            message="Task started",
            created_at=task_info.started_at,
        )
        self._schedule_task_update(force=True)

    def _finalize_task_run(self, managed: ManagedTask) -> None:
        """Finalize task bookkeeping after a managed run."""
        task_info = managed.task_info
        task_info.finished_at = utcnow()
        task_info.updated_at = task_info.finished_at
        managed.current_task = None
        if managed.removed:
            self._tasks.pop(task_info.id, None)
            if managed.clear_persisted_state_on_remove:
                self._clear_scheduled_task_state(task_info.id)
        elif managed.is_scheduled:
            self._schedule_managed_task(managed)
            self._persist_scheduled_task_state(managed)
        trim_finished_history(self._tasks, MAX_FINISHED_TASK_HISTORY)
        self._schedule_task_update(force=True)
        self._start_pending_tasks()

    def _queue_task(
        self,
        managed: ManagedTask,
        *,
        reset_logs: bool,
        run_user_id: str | None = None,
    ) -> None:
        """Queue a task for execution."""
        if managed.removed:
            raise InvalidDataError(f"Task {managed.task_info.id} is no longer available")
        if managed.task_info.status in (TaskStatus.PENDING, TaskStatus.RUNNING):
            return
        self.mass.cancel_timer(get_task_timer_id(managed.task_info.id))
        if reset_logs:
            managed.task_info.logs.clear()
            managed.task_info.progress = None
            managed.task_info.progress_text = None
            managed.task_info.last_error = None
            managed.task_info.failure_count = 0
            managed.task_info.failure_messages.clear()
            managed.task_info.finished_at = None
        managed.task_info.status = TaskStatus.PENDING
        managed.task_info.last_run_user_id = run_user_id
        managed.task_info.started_at = None
        managed.task_info.next_run = None
        managed.task_info.updated_at = utcnow()
        if managed.task_info.id not in self._pending_task_ids:
            if managed.priority:
                self._pending_task_ids.appendleft(managed.task_info.id)
            else:
                self._pending_task_ids.append(managed.task_info.id)
        self._schedule_task_update(force=True)
        self._start_pending_tasks()

    def _start_pending_tasks(self) -> None:
        """Start queued tasks while concurrency allows it."""
        while self._running_tasks_count < self._max_concurrent_tasks and self._pending_task_ids:
            task_id = self._pending_task_ids.popleft()
            if not (managed := self._tasks.get(task_id)) or managed.removed:
                continue
            if managed.task_info.status != TaskStatus.PENDING:
                continue
            managed.current_task = self.mass.create_task(self._run_task(managed))

    @property
    def _running_tasks_count(self) -> int:
        """Return count of currently running managed tasks."""
        return sum(
            1 for managed in self._tasks.values() if managed.task_info.status == TaskStatus.RUNNING
        )

    async def _run_task(self, managed: ManagedTask) -> None:
        """Run a managed task."""
        task_info = managed.task_info
        self._mark_task_running(managed)
        task_context = TaskExecutionContext(
            task_id=task_info.id,
            get_task=self.get_task,
            update_progress=self.update_task_progress,
            update_progress_text=self.update_task_progress_text,
            add_failure=self.add_task_failure,
        )
        token = ACTIVE_TASK_ID.set(task_info.id)
        context_token = ACTIVE_TASK_CONTEXT.set(task_context)
        try:
            await managed.handler()
        except asyncio.CancelledError:
            task_info.status = TaskStatus.CANCELLED
            task_info.last_error = None
            now = utcnow()
            self._append_task_lifecycle_log(
                task_info.id, level=logging.WARNING, message="Task cancelled", created_at=now
            )
        except Exception as err:
            task_info.status = TaskStatus.FAILED
            task_info.last_error = str(err)
            self.logger.warning(
                "Background task %s failed: %s",
                task_info.name,
                str(err),
                exc_info=err if self.logger.isEnabledFor(logging.DEBUG) else None,
            )
            now = utcnow()
            self._append_task_lifecycle_log(
                task_info.id,
                level=logging.ERROR,
                message=f"Task failed: {err}",
                created_at=now,
            )
        else:
            if task_info.failure_count:
                now = utcnow()
                task_info.status = TaskStatus.PARTIAL_SUCCESS
                self._append_task_lifecycle_log(
                    task_info.id,
                    level=logging.WARNING,
                    message=f"Task completed with {task_info.failure_count} issue(s)",
                    created_at=now,
                )
            else:
                task_info.status = TaskStatus.SUCCESS
                now = utcnow()
                self._append_task_lifecycle_log(
                    task_info.id,
                    level=logging.INFO,
                    message="Task completed successfully",
                    created_at=now,
                )
        finally:
            ACTIVE_TASK_CONTEXT.reset(context_token)
            ACTIVE_TASK_ID.reset(token)
            self._finalize_task_run(managed)

    def _cancel_managed_task(self, managed: ManagedTask) -> None:
        """Cancel a pending or running task."""
        task_info = managed.task_info
        self.mass.cancel_timer(get_task_timer_id(task_info.id))
        if managed.current_task and not managed.current_task.done():
            managed.current_task.cancel()
        elif task_info.status == TaskStatus.PENDING:
            self._remove_from_pending(task_info.id)
            task_info.status = TaskStatus.CANCELLED
            task_info.finished_at = utcnow()
            task_info.updated_at = task_info.finished_at
            if managed.is_scheduled:
                self._schedule_managed_task(managed)
        elif managed.is_scheduled:
            self._schedule_managed_task(managed)
        self._persist_scheduled_task_state(managed)
        self._schedule_task_update(force=True)

    def _schedule_managed_task(self, managed: ManagedTask, delay: float | None = None) -> None:
        """Schedule the next recurring execution of a task."""
        if managed.removed or not managed.task_info.schedule:
            return
        if not managed.task_info.schedule.enabled:
            managed.timer_delay = None
            managed.task_info.next_run = None
            managed.task_info.updated_at = utcnow()
            self._persist_scheduled_task_state(managed)
            self._schedule_task_update(force=True)
            return
        delay, next_run = get_task_schedule_delay(
            managed.task_info.schedule,
            last_run=managed.task_info.last_run,
            fallback_delay=delay,
        )
        managed.timer_delay = delay
        managed.task_info.next_run = next_run
        managed.task_info.updated_at = utcnow()
        self._persist_scheduled_task_state(managed)
        self.mass.call_later(
            delay,
            partial(self._queue_task, managed, reset_logs=True),
            task_id=get_task_timer_id(managed.task_info.id),
        )
        self._schedule_task_update(force=True)

    def _restore_scheduled_task_state(self, task_info: BackgroundTask) -> None:
        """Restore persisted runtime state for a scheduled task."""
        if task_info.schedule is None:
            return
        states = self._get_persisted_task_states()
        if not (state := states.get(task_info.id)) or not isinstance(state, dict):
            return
        restore_task_state(task_info, state)

    def _persist_scheduled_task_state(self, managed: ManagedTask) -> None:
        """Persist runtime state for a scheduled task."""
        if not managed.is_scheduled:
            return
        updated_states = dict(self._get_persisted_task_states())
        updated_states[managed.task_info.id] = serialize_task_state(managed.task_info)
        self._set_persisted_task_states(updated_states)

    def _clear_scheduled_task_state(self, task_id: str) -> None:
        """Remove persisted runtime state for a scheduled task."""
        updated_states = dict(self._get_persisted_task_states())
        if task_id not in updated_states:
            return
        updated_states.pop(task_id, None)
        self._set_persisted_task_states(updated_states)

    def _get_persisted_task_states(self) -> dict[str, Any]:
        """Return persisted runtime state for scheduled tasks."""
        states = self.mass.config.get(f"core/{self.domain}/{TASK_STATE_CONFIG_KEY}", {})
        return states if isinstance(states, dict) else {}

    def _set_persisted_task_states(self, states: dict[str, Any]) -> None:
        """Persist runtime state for scheduled tasks."""
        self.mass.config.set(f"core/{self.domain}/{TASK_STATE_CONFIG_KEY}", states)

    @staticmethod
    def _resolve_schedule(
        *,
        schedule: TaskSchedule | None,
    ) -> TaskSchedule:
        """Resolve the requested schedule configuration."""
        if schedule is None:
            raise InvalidDataError("Scheduled task requires a schedule")
        return schedule

    @staticmethod
    def _resolve_updated_schedule(
        *,
        current_schedule: TaskSchedule,
        schedule: TaskSchedule | None,
    ) -> TaskSchedule:
        """Resolve an updated schedule while preserving enabled state."""
        if schedule is None:
            raise InvalidDataError("Updated schedule requires a schedule")
        schedule.enabled = current_schedule.enabled
        return schedule

    @staticmethod
    def _validate_progress(progress: int | None) -> int | None:
        """Validate task progress percentage."""
        if progress is None:
            return None
        if isinstance(progress, bool) or not isinstance(progress, int):
            raise InvalidDataError("Task progress must be an integer percentage")
        if not 0 <= progress <= 100:
            raise InvalidDataError("Task progress must be between 0 and 100")
        return progress

    def _remove_from_pending(self, task_id: str) -> None:
        """Remove a task from the pending queue."""
        with suppress(ValueError):
            self._pending_task_ids.remove(task_id)

    def _schedule_task_update(self, *, force: bool = False) -> None:
        """Coalesce task update events while keeping lifecycle updates responsive."""
        if get_ident() != self.mass.loop_thread_id:
            self.mass.loop.call_soon_threadsafe(partial(self._schedule_task_update, force=force))
            return
        now = self.mass.loop.time()
        if force:
            delay = TASK_LIFECYCLE_UPDATE_DEBOUNCE
        else:
            delay = max(
                TASK_ACTIVITY_UPDATE_INTERVAL - (now - self._last_task_update_signal),
                TASK_LIFECYCLE_UPDATE_DEBOUNCE,
            )
        scheduled_at = now + delay
        if (
            self._scheduled_task_update_at is not None
            and self._scheduled_task_update_at <= scheduled_at
        ):
            return
        self._scheduled_task_update_at = scheduled_at
        self.mass.call_later(delay, self._signal_task_update, task_id=TASK_UPDATE_TIMER_ID)

    def _signal_task_update(self) -> None:
        """Emit the current managed task list."""
        self._scheduled_task_update_at = None
        self._last_task_update_signal = self.mass.loop.time()
        self.mass.signal_event(EventType.TASKS_UPDATED, data=self.list_tasks_for_user(None))


def _namespaced_translation_key(translation_key: str | None) -> str | None:
    """
    Namespace a bare task key under the shared ``background_task`` group.

    Callers pass just the task key (e.g. ``database_cleanup``); the ``background_task`` group is
    implicit for tasks and added here. A key that already carries a namespace (anything containing
    a ``.``, e.g. a fully-qualified key) is returned unchanged.
    """
    if translation_key and "." not in translation_key:
        return f"background_task.{translation_key}"
    return translation_key
