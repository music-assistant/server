# Background Tasks Controller

This package contains the long-running task manager used for user-visible background work such as library syncs, playlist mutations, and other scheduled/system jobs.

## Responsibilities

- Register recurring scheduled tasks and keep them visible even while idle.
- Queue and execute long-running ad hoc tasks with bounded concurrency.
- Capture per-task in-memory log output for UI inspection and export.
- Expose a task execution context so long-running code can report progress and non-fatal issues.
- Publish task state changes through `EventType.TASKS_UPDATED`.
- Retain only a limited history of completed ad hoc tasks in memory.
- Persist scheduled-task runtime state such as `last_run` and pause/enable state.

## Package Layout

- `controller.py`: main `TasksController` orchestration, API handlers, queueing, and execution lifecycle.
- `constants.py`: shared queue/log/retention constants and context variables.
- `context.py`: runtime task-context helpers used by long-running code to report progress/issues.
- `helpers.py`: reusable helper functions for visibility, sorting, retention, timer ids, and log capture.
- `models.py`: runtime-only task state container used by the controller.

## Design Notes

- This controller is intentionally scoped to long-running work. Short-lived internal jobs should continue using `mass.create_task` and `mass.call_later`.
- Scheduled tasks are retained in memory permanently; completed ad hoc tasks are retained only in a bounded history.
- Progress/log churn is throttled before being mirrored onto the event bus, while lifecycle changes stay responsive.
- Scheduled-task runtime state is persisted in the `tasks` core config under a dedicated raw value so restarts preserve `last_run`, failure state, and the enabled/disabled schedule flag.
