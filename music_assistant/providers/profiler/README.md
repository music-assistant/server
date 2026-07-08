# Profiler

A debugging aid to diagnose performance issues (high CPU, memory growth, audio dropouts)
on a running Music Assistant server — no shell access needed. **This is not a regular
provider**: only install it temporarily while investigating an issue (or when asked to in
a support request) and uninstall it when done.

## Workflow

1. Install the Profiler provider (Settings → Providers → Add provider → Profiler).
2. Let it run while you reproduce the issue (measurements start immediately).
3. Call the `profiler/report` API command (each generated report is also saved as
   `report.json` / `report.md` in the `profiler` folder inside the server data
   directory).
4. Paste the report into a GitHub issue or the LLM of your choice for analysis.
5. Uninstall the provider.

The report contains only code identifiers (function names, file:line locations),
counters and byte/time metrics — no media titles, paths, URLs, account names or other
personal data — so it is safe to share publicly.

## What is measured (continuously, while installed)

- **Flight recorder** — every 10 seconds a sample of RSS memory, CPU%, event-loop lag,
  asyncio task count, websocket clients, ffmpeg child processes, events/sec and log
  errors/sec is stored in a 24h in-memory ring (and appended to `stats.csv`). This
  answers "what happened at 3am".
- **Event-loop lag monitor** — samples scheduling delay every 0.5s. High lag means
  something is blocking the event loop (the usual cause of audio dropouts and a
  sluggish UI).
- **Event & error counters** — events on the MA event bus counted by type; WARNING+
  log records counted by source location/level/exception type (never message content).
- **Periodic CPU profile windows** (default on: 60s window every 30 minutes, first one
  ~1 minute after load) — yappi profile with CPU clock; top functions are included in
  the report and the full `.pstats` files (last 10) are kept in the profiler storage
  folder for offline analysis. Use the "Profile now" button in the provider settings to
  capture a window on demand while reproducing an issue.
- **Memory allocation tracking** (default off, config option) — tracemalloc-based top
  allocation sites plus growth between reports. Roughly doubles memory-tracking
  overhead; only enable when hunting a memory leak.

Idle overhead is negligible: apart from the 10s sampler, 0.5s lag probe and cheap
counters, nothing runs unless a CPU profile window is active.

## The report

`profiler/report` (admin only) returns one self-describing JSON object:

| Section | Contents |
| --- | --- |
| `server` | MA version, python/platform, uptime |
| `config_summary` | provider counts by type, player counts, library sizes |
| `memory` | RSS/VMS, gc stats, thread/fd/task counts, optional object census & tracemalloc |
| `event_loop` | lag avg/max (last minute + max since load) |
| `asyncio_tasks` | running tasks grouped by suspended code location |
| `events` / `log_errors` | busiest event types, warning/error counts |
| `cpu_profile` | top functions of the last CPU profile window |
| `flight_recorder` | recorder samples (default: last 30 minutes) |

Arguments: `markdown=true` returns a markdown rendering instead of JSON;
`recorder_minutes` (1-1440) widens the recorder window; `include_object_census=true`
adds a census of all live Python objects by type — note that this walks the entire
heap and can stall the server for several seconds on large installations.

Every report is also written to the `profiler` folder inside the server data
directory as `report.json` and `report.md`.

## Interpreting results

- **`event_loop.lag_max_ms` above ~100ms**: something blocks the loop — check
  `cpu_profile.top_functions` for synchronous work (`tsub_s` is time spent in the
  function itself) and `asyncio_tasks.top_by_location` for what was running.
- **`rss_mb` climbing steadily in the flight recorder**: memory leak — re-run with the
  tracemalloc option enabled and compare `growth_since_previous_report` between two
  reports taken some time apart; `include_object_census=true` shows which object types
  accumulate.
- **High `events_per_s`**: something is flooding the event bus — see `events.per_type_top`.
- **Growing `asyncio_tasks`/`tracked_tasks`**: task leak — the locations in
  `asyncio_tasks.top_by_location` show where the leaked tasks are suspended.
- **`ffmpeg_processes` when nothing plays**: orphaned streaming processes.
