# Performance benchmark suite

A repeatable performance benchmark for the Music Assistant server. It boots a
fully hermetic server instance (no network access, no discovery, no real
players), runs a fixed scenario suite against it and emits a single
self-describing JSON report designed to be diffed by a tool or pasted into an
LLM for analysis.

## Running

```shell
# from the repo root, with the repo venv (scripts/setup.sh)
.venv/bin/python scripts/perf/run_benchmark.py --out report.json

# small library / short scenarios (~2 min instead of ~7 min)
.venv/bin/python scripts/perf/run_benchmark.py --quick

# human-readable output
.venv/bin/python scripts/perf/run_benchmark.py --markdown

# regression check: save a baseline once, compare against it later
.venv/bin/python scripts/perf/run_benchmark.py --save-baseline
.venv/bin/python scripts/perf/run_benchmark.py --compare   # exits non-zero on regression
```

Requirements: the `test` extras (`uv pip install -e '.[test]'` — provides
`psutil` and `yappi`), plus `ffmpeg` and `curl` on `PATH`. The suite fails
early with a clear message when anything is missing.

## What it measures

The suite runs two passes: pass 1 executes every scenario **unprofiled** for
accurate wall/CPU/RSS numbers; pass 2 boots a fresh server **with yappi** and
repeats the CPU-relevant scenarios to attribute the CPU time to functions
(`yappi_top`, top-15 rows per scenario). Profiling inflates CPU time
significantly, so the two concerns are never mixed.

| Scenario | Metrics | Meaning |
|---|---|---|
| `startup` | `import_*` | wall/CPU/peak-RSS of `import music_assistant.mass` in a fresh interpreter, plus the 10 slowest imports (`python -X importtime`) |
| | `cold_boot_*` | spawn → fully READY on an empty data dir (wall/CPU/RSS) |
| | `warm_boot_*` | same, restarting on the already-populated data dir (the realistic restart case) |
| `initial_sync` | `wall_seconds`, `cpu_seconds`, `peak_rss_mb`, `library_db_mb` | first full sync of the test-provider library (10k tracks, `--quick`: 1k) into an empty database |
| `noop_resync` | `wall_seconds`, `cpu_seconds` | full re-sync when nothing changed — the key sync-efficiency regression metric |
| `api_bench` | per command: `median_ms`, `p95_ms`, `payload_kb`, `items` | websocket API latency/payload for library pages, deep offset, search, players |
| `streaming` | `python_cpu_seconds`, `ffmpeg_cpu_seconds`, `max_loop_lag_ms` | 2 concurrent flow-mode WAV streams consumed at ~realtime for 30s (`--quick`: 12s) |
| `memory` | `rss_*_mb`, `round2_growth_mb` | RSS checkpoints; two identical rounds of heavy listing requests — round 2 must be ~flat, growth there indicates a leak |

All metric keys carry their unit (`_seconds`, `_ms`, `_mb`, `_kb`) and for
every metric a higher value is worse.

## The hermetic server

`perf_server.py` boots a real `MusicAssistant` instance with strict isolation:

- webserver and stream server bound to `127.0.0.1` only
- zeroconf and SSDP discovery replaced by mocks (nothing is announced on the
  LAN, nothing is discovered — real devices can otherwise connect to the test
  instance, which skews every number)
- only an allowlist of builtin providers is loaded; notably **sendspin** is
  stripped before load (it runs its own aiosendspin server with its own mDNS)
  and the network metadata providers (musicbrainz, fanart.tv, ...) are stripped
  so the suite is offline-deterministic
- the library is populated by the `test` provider (generated data, silence
  audio), players are `_demo_player_provider` instances

## Baseline & comparing

**Absolute numbers are machine-specific** — comparisons are only valid between
runs on the same machine (and ideally the same power/thermal state), so no
baseline is committed to the repo. Instead, `--save-baseline` stores the report
as your machine-local baseline in `~/.musicassistant-perf/` (`baseline-full.json`
or `baseline-quick.json`, so the two modes never get mixed up) and a bare
`--compare` checks against it. Passing an explicit path
(`--compare some-report.json`) still works, e.g. to diff two saved reports from
the same machine.

Re-run `--save-baseline` after intentional performance changes to accept the
new numbers as the reference.

`--compare` flags a metric when it exceeds the baseline by more than 15%
(relative), with per-metric overrides and small absolute floors to suppress
noise on near-zero metrics — see `report.py` (`PER_METRIC_REL_THRESHOLDS`,
`ABS_FLOORS`).

### Observed run-to-run variance

Two consecutive full runs on the reference machine (Apple M4 Pro) showed
CPU-seconds metrics within ~3% of each other and latency medians within ~3%.
Wall-clock, p95, RSS (~10%) and loop-lag metrics are noisier (hence their
higher thresholds/floors). Treat single-run differences below ~10% as noise;
for borderline results run the suite twice.

### CI

Automatic per-PR performance checks are handled by **CodSpeed**
(`.github/workflows/codspeed.yml` + `tests/benchmarks/`): it measures CPU
instruction counts under valgrind, which is deterministic on shared runners,
and comments on PRs that touch core code. Use this suite instead for local
deep-dives: it measures real wall/CPU time, RSS, streaming and loop lag on a
full server process — things CodSpeed's in-process model cannot see. Runner
variance on shared GitHub-hosted runners is far larger than the regressions
the timing-based metrics are meant to catch, so this suite has no CI job of
its own.
