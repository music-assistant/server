"""Helper utilities for the background tasks controller."""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Mapping
from datetime import UTC, datetime, time, timedelta
from typing import TYPE_CHECKING, Any

from music_assistant_models.auth import Scope, User
from music_assistant_models.background_task import BackgroundTask, TaskSchedule
from music_assistant_models.enums import TaskScheduleType, TaskStatus

from music_assistant.controllers.webserver.helpers.auth_middleware import has_scope
from music_assistant.helpers.datetime import utc

from .constants import ACTIVE_TASK_ID
from .models import ManagedTask

if TYPE_CHECKING:
    from music_assistant import MusicAssistant

TASK_LOG_FORMAT = "%(asctime)s %(levelname)s [%(name)s] %(message)s"
TASK_LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
TASK_LOG_FORMATTER = logging.Formatter(TASK_LOG_FORMAT, datefmt=TASK_LOG_DATE_FORMAT)


def utcnow() -> datetime:
    """Return current UTC datetime."""
    return utc()


def get_task_timer_id(task_id: str) -> str:
    """Return timer id for a managed task."""
    return f"managed_task_timer_{task_id}"


def task_sort_key(managed: ManagedTask) -> tuple[int, float]:
    """Sort active tasks first, then recently updated tasks."""
    task_info = managed.task_info
    if task_info.status == TaskStatus.RUNNING:
        priority = 0
    elif task_info.status == TaskStatus.PENDING:
        priority = 1
    elif managed.is_scheduled:
        priority = 2
    else:
        priority = 3
    return (priority, -task_info.updated_at.timestamp())


def is_task_visible_to_user(task: ManagedTask, user: User | None) -> bool:
    """Return if a task should be visible to the given user."""
    if user is None or has_scope(user, Scope.SYSTEM_MANAGE):
        return True
    return task.task_info.user_id == user.user_id


def get_visible_tasks(tasks: Iterable[ManagedTask], user: User | None) -> list[ManagedTask]:
    """Return tasks visible to the given user."""
    visible_tasks = list(tasks)
    if user is not None and not has_scope(user, Scope.SYSTEM_MANAGE):
        visible_tasks = [task for task in visible_tasks if is_task_visible_to_user(task, user)]
    visible_tasks.sort(key=task_sort_key)
    return visible_tasks


def trim_finished_history(tasks: dict[str, ManagedTask], max_history: int) -> None:
    """Keep finished ad-hoc task history bounded."""
    finished = [managed for managed in tasks.values() if managed.can_remove]
    if len(finished) <= max_history:
        return
    finished.sort(key=lambda managed: managed.task_info.updated_at.timestamp())
    for managed in finished[: len(finished) - max_history]:
        tasks.pop(managed.task_info.id, None)


def get_task_schedule_next_run(
    schedule: TaskSchedule,
    now: datetime | None = None,
) -> datetime:
    """Calculate the next run moment in UTC for a task schedule."""
    current_time = now or utcnow()
    if schedule.type == TaskScheduleType.HOURLY:
        if schedule.every is None:
            raise ValueError("Hourly schedule requires every")
        return current_time + timedelta(hours=schedule.every)

    local_now = current_time.astimezone(UTC)
    scheduled_time = time(hour=schedule.hour or 0, minute=schedule.minute or 0)

    if schedule.type == TaskScheduleType.DAILY:
        candidate = datetime.combine(local_now.date(), scheduled_time, UTC)
        if candidate <= local_now:
            candidate += timedelta(days=schedule.every or 1)
        return candidate

    if schedule.type == TaskScheduleType.WEEKLY:
        if not schedule.days_of_week:
            raise ValueError("Weekly schedule requires days_of_week")
        next_candidate: datetime | None = None
        for day_of_week in schedule.days_of_week:
            days_ahead = (day_of_week - local_now.weekday()) % 7
            candidate_date = local_now.date() + timedelta(days=days_ahead)
            candidate = datetime.combine(candidate_date, scheduled_time, UTC)
            if candidate <= local_now:
                candidate += timedelta(days=7)
            if next_candidate is None or candidate < next_candidate:
                next_candidate = candidate
        if next_candidate is None:
            raise ValueError("Unable to calculate next run for weekly schedule")
        return next_candidate

    raise ValueError(f"Unsupported task schedule type: {schedule.type}")


def get_task_schedule_delay(
    schedule: TaskSchedule,
    now: datetime | None = None,
    last_run: datetime | None = None,
    fallback_delay: float | None = None,
) -> tuple[float, datetime]:
    """Calculate the delay and next run moment for a task schedule."""
    current_time = now or utcnow()
    if schedule.type == TaskScheduleType.HOURLY and schedule.every is not None:
        if last_run is not None:
            next_run = last_run + timedelta(hours=schedule.every)
            if next_run > current_time:
                return (next_run - current_time).total_seconds(), next_run
        if fallback_delay is not None:
            next_run = current_time + timedelta(seconds=fallback_delay)
            return fallback_delay, next_run
    elif schedule.type == TaskScheduleType.DAILY:
        scheduled_time = time(hour=schedule.hour or 0, minute=schedule.minute or 0)
        if last_run is not None:
            next_run = datetime.combine(last_run.astimezone(UTC).date(), scheduled_time, UTC)
            if next_run <= last_run:
                next_run += timedelta(days=schedule.every or 1)
            while next_run <= current_time:
                next_run += timedelta(days=schedule.every or 1)
            return max((next_run - current_time).total_seconds(), 0.0), next_run
    next_run = get_task_schedule_next_run(schedule, current_time)
    if last_run is None and fallback_delay is not None:
        next_run = current_time + timedelta(seconds=fallback_delay)
        return fallback_delay, next_run
    delay = max((next_run - current_time).total_seconds(), 0.0)
    return delay, next_run


def serialize_task_state(task: BackgroundTask) -> dict[str, Any]:
    """Serialize the persisted runtime state for a scheduled task."""
    return {
        "status": task.status.value,
        "last_run": task.last_run.isoformat() if task.last_run else None,
        "last_run_user_id": task.last_run_user_id,
        "finished_at": task.finished_at.isoformat() if task.finished_at else None,
        "updated_at": task.updated_at.isoformat() if task.updated_at else None,
        "last_error": task.last_error,
        "failure_count": task.failure_count,
        "failure_messages": list(task.failure_messages),
        "schedule": serialize_task_schedule_state(task.schedule),
        "schedule_enabled": task.schedule.enabled if task.schedule else True,
    }


def restore_task_state(task: BackgroundTask, state: Mapping[str, Any]) -> None:
    """Restore persisted runtime state for a scheduled task."""
    if task.schedule and isinstance(state.get("schedule"), Mapping):
        task.schedule = merge_task_schedule_state(task.schedule, state["schedule"])
    elif task.schedule and isinstance(state.get("schedule_enabled"), bool):
        task.schedule.enabled = state["schedule_enabled"]

    status_value = state.get("status")
    if isinstance(status_value, str):
        status = TaskStatus(status_value)
        if status in (TaskStatus.PENDING, TaskStatus.RUNNING):
            status = TaskStatus.IDLE
        task.status = status

    task.last_run = parse_utc_datetime(state.get("last_run"))
    task.last_run_user_id = (
        state.get("last_run_user_id") if isinstance(state.get("last_run_user_id"), str) else None
    )
    task.finished_at = parse_utc_datetime(state.get("finished_at"))
    task.updated_at = parse_utc_datetime(state.get("updated_at")) or task.updated_at
    task.last_error = state.get("last_error") if isinstance(state.get("last_error"), str) else None

    failure_count = state.get("failure_count")
    task.failure_count = (
        failure_count if isinstance(failure_count, int) and failure_count > 0 else 0
    )

    failure_messages = state.get("failure_messages")
    if isinstance(failure_messages, list):
        task.failure_messages = [item for item in failure_messages if isinstance(item, str)]
    else:
        task.failure_messages = []


def serialize_task_schedule_state(schedule: TaskSchedule | None) -> dict[str, Any] | None:
    """Serialize only the editable schedule fields for persistence."""
    if schedule is None:
        return None
    result: dict[str, Any] = {
        "type": schedule.type.value,
        "enabled": schedule.enabled,
    }
    if schedule.type in (TaskScheduleType.HOURLY, TaskScheduleType.DAILY):
        result["every"] = schedule.every
    if schedule.type == TaskScheduleType.DAILY:
        result["hour"] = schedule.hour
        result["minute"] = schedule.minute
    elif schedule.type == TaskScheduleType.WEEKLY:
        result["days_of_week"] = list(schedule.days_of_week or [])
        result["hour"] = schedule.hour
        result["minute"] = schedule.minute
    return result


def _build_schedule_from_state(
    default_schedule: TaskSchedule,
    schedule_type: TaskScheduleType,
    state: Mapping[str, Any],
    enabled: bool,
) -> TaskSchedule:
    """Build a schedule from persisted editable state."""
    if schedule_type == TaskScheduleType.HOURLY:
        return TaskSchedule(
            type=TaskScheduleType.HOURLY,
            enabled=enabled,
            every=(
                state["every"]
                if isinstance(state.get("every"), int) and state["every"] > 0
                else (
                    default_schedule.every
                    if default_schedule.type == TaskScheduleType.HOURLY
                    else 1
                )
            ),
        )
    if schedule_type == TaskScheduleType.DAILY:
        raw_hour = state.get("hour")
        raw_minute = state.get("minute")
        return TaskSchedule(
            type=TaskScheduleType.DAILY,
            enabled=enabled,
            every=(
                state["every"]
                if isinstance(state.get("every"), int) and state["every"] > 0
                else (
                    default_schedule.every if default_schedule.type == TaskScheduleType.DAILY else 1
                )
            ),
            hour=(
                raw_hour
                if isinstance(raw_hour, int)
                else (
                    default_schedule.hour
                    if default_schedule.type == TaskScheduleType.DAILY
                    and default_schedule.hour is not None
                    else 0
                )
            ),
            minute=(
                raw_minute
                if isinstance(raw_minute, int)
                else (
                    default_schedule.minute
                    if default_schedule.type == TaskScheduleType.DAILY
                    and default_schedule.minute is not None
                    else 0
                )
            ),
        )
    if schedule_type == TaskScheduleType.WEEKLY:
        raw_days_of_week = state.get("days_of_week")
        raw_hour = state.get("hour")
        raw_minute = state.get("minute")
        return TaskSchedule(
            type=TaskScheduleType.WEEKLY,
            enabled=enabled,
            days_of_week=(
                raw_days_of_week
                if isinstance(raw_days_of_week, list)
                and all(isinstance(day, int) for day in raw_days_of_week)
                else (
                    default_schedule.days_of_week
                    if default_schedule.type == TaskScheduleType.WEEKLY
                    and default_schedule.days_of_week
                    else [0]
                )
            ),
            hour=(
                raw_hour
                if isinstance(raw_hour, int)
                else (
                    default_schedule.hour
                    if default_schedule.type == TaskScheduleType.WEEKLY
                    and default_schedule.hour is not None
                    else 0
                )
            ),
            minute=(
                raw_minute
                if isinstance(raw_minute, int)
                else (
                    default_schedule.minute
                    if default_schedule.type == TaskScheduleType.WEEKLY
                    and default_schedule.minute is not None
                    else 0
                )
            ),
        )
    raise ValueError(f"Unsupported task schedule type: {schedule_type}")


def merge_task_schedule_state(
    default_schedule: TaskSchedule,
    state: Mapping[str, Any],
) -> TaskSchedule:
    """Merge persisted/editable schedule state onto a default schedule definition."""
    enabled = (
        state["enabled"] if isinstance(state.get("enabled"), bool) else default_schedule.enabled
    )

    schedule_type = default_schedule.type
    raw_type = state.get("type")
    if isinstance(raw_type, str):
        try:
            parsed_type = TaskScheduleType(raw_type)
        except ValueError:
            parsed_type = default_schedule.type
        if parsed_type != TaskScheduleType.UNKNOWN:
            schedule_type = parsed_type

    try:
        return _build_schedule_from_state(default_schedule, schedule_type, state, enabled)
    except ValueError:
        return default_schedule


def parse_utc_datetime(value: Any) -> datetime | None:
    """Parse an ISO datetime string into an aware UTC datetime."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def format_task_log_line(
    message: str,
    *,
    level: int,
    logger_name: str,
    created_at: datetime | None = None,
) -> str:
    """Format a synthetic task lifecycle log line like regular captured logs."""
    record = logging.LogRecord(logger_name, level, "", 0, message, (), None)
    if created_at is not None:
        created_ts = created_at.timestamp()
        record.created = created_ts
        record.msecs = (created_ts - int(created_ts)) * 1000
    return TASK_LOG_FORMATTER.format(record)


class TaskLogHandler(logging.Handler):
    """Logging handler that mirrors log lines into the active managed task."""

    def __init__(
        self,
        mass: MusicAssistant,
        append_log: Callable[[str, str], None],
    ) -> None:
        """Initialize the handler."""
        super().__init__(logging.DEBUG)
        self._mass = mass
        self._append_log = append_log
        self.setFormatter(TASK_LOG_FORMATTER)

    def emit(self, record: logging.LogRecord) -> None:
        """Forward the formatted log line to the active task buffer."""
        if not (task_id := ACTIVE_TASK_ID.get()):
            return
        try:
            line = self.format(record)
        except Exception:  # pragma: no cover - logging internals
            self.handleError(record)
            return
        self._mass.loop.call_soon_threadsafe(self._append_log, task_id, line)
