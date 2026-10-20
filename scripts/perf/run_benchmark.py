"""
Repeatable performance benchmark suite for the Music Assistant server.

Boots a hermetic server (see perf_server.py for the isolation guarantees), runs a
fixed scenario suite against it and emits one self-describing JSON document with
all metrics (units embedded in the key names), designed to be diffed by a tool or
pasted into an LLM.

Methodology: two passes. Pass 1 runs every scenario unprofiled for accurate
wall/CPU/RSS metrics; pass 2 boots a fresh server with yappi enabled and repeats
the CPU-relevant scenarios to capture per-scenario hotspot attribution
(yappi_top), so profiler overhead never pollutes the timing metrics.

Usage (from the repo root, with the repo venv):
  .venv/bin/python scripts/perf/run_benchmark.py [--quick] [--out report.json]
      [--save-baseline] [--compare [baseline.json]] [--markdown] [--keep-data]

Baselines are machine-specific, so they are stored per machine (and per mode) in
~/.musicassistant-perf/ rather than in the repo: run once with --save-baseline,
then use a bare --compare to check for regressions against it.

Exit code is non-zero when --compare detects a regression beyond the thresholds
(see report.py) or when the suite itself fails.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import importlib.util
import json
import os
import platform
import re
import select
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, Any

# ruff: noqa: T201

try:
    import aiohttp
    import psutil
except ImportError as err:
    sys.exit(
        f"Missing benchmark dependency: {err}.\n"
        "Install the dev/test extras first: uv pip install -e '.[test]'"
    )

try:
    from scripts.perf.report import compare_reports, render_markdown
    from scripts.perf.ws_client import PerfWsClient, summarize_latencies
except ImportError:  # direct file execution: python scripts/perf/run_benchmark.py
    from report import compare_reports, render_markdown  # type: ignore[no-redef]
    from ws_client import PerfWsClient, summarize_latencies  # type: ignore[no-redef]

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parents[1]
PERF_SERVER = SCRIPT_DIR / "perf_server.py"
BASELINE_DIR = Path.home() / ".musicassistant-perf"

SCHEMA_VERSION = 1
SERVER_READY_TIMEOUT = 120
SYNC_TIMEOUT = 1800
PLAYBACK_READY_TIMEOUT = 30
SYNC_POLL_INTERVAL = 0.25
# control-channel handlers are near-instant; a bounded per-request timeout keeps a
# stuck server from hanging the suite past its scenario deadlines
CONTROL_REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=60)
# 44.1 kHz / 16 bit / stereo WAV is 176400 bytes/s; limiting curl to ~1x realtime
# makes the stream consumption deterministic and realistic for the whole window
STREAM_RATE_BYTES_PER_SEC = 176 * 1024
STREAM_RATE_LIMIT = f"{STREAM_RATE_BYTES_PER_SEC // 1024}k"
STREAMING_PLAYERS = 2
MEDIA_ITEM_EVENTS_SETTLE_SECONDS = 2.0


@dataclass(frozen=True)
class SuiteParams:
    """Sizing parameters for one benchmark mode."""

    quick: bool
    artists: int
    albums: int
    tracks: int
    podcasts: int
    audiobooks: int
    players: int
    api_iterations: int
    streaming_seconds: int
    memory_requests_per_round: int
    profiled_api_iterations: int = 2
    profiled_streaming_seconds: int = 10

    @property
    def total_tracks(self) -> int:
        """Total number of generated tracks in the test library."""
        return self.artists * self.albums * self.tracks


FULL_PARAMS = SuiteParams(
    quick=False,
    artists=50,
    albums=10,
    tracks=20,
    podcasts=10,
    audiobooks=10,
    players=4,
    api_iterations=5,
    streaming_seconds=30,
    memory_requests_per_round=60,
)
QUICK_PARAMS = SuiteParams(
    quick=True,
    artists=25,
    albums=8,
    tracks=5,
    podcasts=2,
    audiobooks=2,
    players=4,
    api_iterations=3,
    streaming_seconds=12,
    memory_requests_per_round=20,
)


class BenchmarkError(RuntimeError):
    """Raised when the benchmark cannot produce valid results."""


class ServerHandle:
    """A running hermetic perf server subprocess."""

    def __init__(self, proc: subprocess.Popen[str], data_dir: Path, log_file: IO[str]) -> None:
        """Wrap a spawned perf_server process; call wait_ready() before use."""
        self.proc = proc
        self.data_dir = data_dir
        self.ps = psutil.Process(proc.pid)
        self.port = 0
        self.control_port = 0
        self.stream_port = 0
        self.boot_wall_seconds = 0.0
        self.boot_cpu_seconds = 0.0
        self.boot_rss_mb = 0.0
        self._log_file = log_file
        self._spawned_at = time.perf_counter()

    def wait_ready(self) -> None:
        """Block until the server prints its READY line (or fail with the log tail)."""
        assert self.proc.stdout is not None
        deadline = time.monotonic() + SERVER_READY_TIMEOUT
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise BenchmarkError(
                    f"perf server exited during boot (rc={self.proc.returncode}), "
                    f"see {self.data_dir / 'server.log'}"
                )
            readable, _, _ = select.select([self.proc.stdout], [], [], 1.0)
            if not readable:
                continue
            line = self.proc.stdout.readline()
            if not line.startswith("PERF_SERVER_READY"):
                continue
            self.boot_wall_seconds = round(time.perf_counter() - self._spawned_at, 2)
            fields = dict(part.split("=") for part in line.split()[1:])
            self.port = int(fields["port"])
            self.control_port = int(fields["control_port"])
            self.stream_port = int(fields["stream_port"])
            self.boot_rss_mb = float(fields["rss_mb"])
            self.boot_cpu_seconds = round(cpu_seconds(self.ps), 2)
            # keep draining stdout so the pipe can never fill up and block the server
            threading.Thread(target=self._drain_stdout, daemon=True).start()
            return
        raise BenchmarkError(f"perf server not ready within {SERVER_READY_TIMEOUT}s")

    def stop(self) -> None:
        """Terminate the server subprocess (SIGTERM, then SIGKILL after a grace period)."""
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=10)
        self._log_file.close()

    def token(self) -> str:
        """Return the admin API token created by the server."""
        return (self.data_dir / "token.txt").read_text().strip()

    def library_db_mb(self) -> float:
        """Return the on-disk size of library.db (incl. WAL) in MB."""
        total = 0
        for suffix in ("", "-wal", "-shm"):
            path = self.data_dir / f"library.db{suffix}"
            if path.is_file():
                total += path.stat().st_size
        return round(total / 1024 / 1024, 1)

    def _drain_stdout(self) -> None:
        """Forward any further stdout lines to the server log file."""
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            with contextlib.suppress(ValueError):
                self._log_file.write(line)


class ControlClient:
    """Client for the perf server's loopback control channel."""

    def __init__(self, session: aiohttp.ClientSession, control_port: int) -> None:
        """Initialize with a shared aiohttp session and the server's control port."""
        self._session = session
        self._base_url = f"http://127.0.0.1:{control_port}"

    async def get(self, path: str) -> dict[str, Any]:
        """Perform a GET request against the control channel."""
        async with self._session.get(
            f"{self._base_url}{path}", timeout=CONTROL_REQUEST_TIMEOUT
        ) as resp:
            resp.raise_for_status()
            return await resp.json()

    async def post(self, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        """Perform a POST request against the control channel."""
        async with self._session.post(
            f"{self._base_url}{path}", json=body, timeout=CONTROL_REQUEST_TIMEOUT
        ) as resp:
            resp.raise_for_status()
            return await resp.json()

    async def yappi_start(self) -> None:
        """Start CPU profiling in the server process."""
        await self.post("/yappi/start")

    async def yappi_stop(self) -> list[dict[str, Any]]:
        """Stop CPU profiling and return the top hotspot rows."""
        return (await self.post("/yappi/stop"))["rows"]

    async def wait_sync_active(self, timeout: float = 60.0) -> None:
        """Wait until a provider sync task is pending or running."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if (await self.get("/sync_active"))["active"]:
                return
            await asyncio.sleep(SYNC_POLL_INTERVAL)
        raise BenchmarkError("sync did not start within timeout")

    async def wait_sync_idle(self, timeout: float = SYNC_TIMEOUT) -> float:
        """
        Wait until no provider sync task is pending or running.

        :return: perf_counter timestamp of the first idle observation.
        """
        deadline = time.monotonic() + timeout
        first_idle: float | None = None
        idle_streak = 0
        while time.monotonic() < deadline:
            if (await self.get("/sync_active"))["active"]:
                first_idle = None
                idle_streak = 0
            else:
                first_idle = first_idle or time.perf_counter()
                idle_streak += 1
                # require a few consecutive idle samples so a gap between two queued
                # sync tasks is not mistaken for completion
                if idle_streak >= 3:
                    return first_idle
            await asyncio.sleep(SYNC_POLL_INTERVAL)
        raise BenchmarkError(f"sync did not complete within {timeout}s")


class PeakRssSampler:
    """Track the peak RSS of a process while active."""

    def __init__(self, ps: psutil.Process) -> None:
        """Initialize the sampler for the given process."""
        self._ps = ps
        self.peak_mb = 0.0
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        """Start sampling."""
        self.peak_mb = self.current_mb()
        self._task = asyncio.create_task(self._run())

    def stop(self) -> float:
        """Stop sampling and return the observed peak RSS in MB."""
        if self._task:
            self._task.cancel()
        return round(max(self.peak_mb, self.current_mb()), 1)

    def current_mb(self) -> float:
        """Return the current RSS in MB."""
        return self._ps.memory_info().rss / 1024 / 1024

    async def _run(self) -> None:
        while True:
            self.peak_mb = max(self.peak_mb, self.current_mb())
            await asyncio.sleep(0.2)


class FfmpegCpuSampler:
    """Accumulate CPU seconds of all ffmpeg child processes of the server."""

    def __init__(self, ps: psutil.Process) -> None:
        """Initialize the sampler for the given (server) process."""
        self._ps = ps
        self._seen: dict[int, float] = {}
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        """Start sampling."""
        self._task = asyncio.create_task(self._run())

    def stop(self) -> float:
        """Stop sampling and return the summed ffmpeg CPU seconds."""
        if self._task:
            self._task.cancel()
        self._sample()
        return round(sum(self._seen.values()), 2)

    def _sample(self) -> None:
        with contextlib.suppress(psutil.Error):
            for child in self._ps.children(recursive=True):
                with contextlib.suppress(psutil.Error):
                    if "ffmpeg" not in child.name():
                        continue
                    times = child.cpu_times()
                    self._seen[child.pid] = max(
                        self._seen.get(child.pid, 0.0), times.user + times.system
                    )

    async def _run(self) -> None:
        while True:
            self._sample()
            await asyncio.sleep(0.5)


def cpu_seconds(ps: psutil.Process) -> float:
    """Return the total (user+system) CPU seconds consumed by the process."""
    times = ps.cpu_times()
    return times.user + times.system


async def rss_checkpoint_mb(server: ServerHandle, control: ControlClient) -> float:
    """Return the server RSS in MB after a full garbage collection."""
    await control.post("/gc")
    return round(server.ps.memory_info().rss / 1024 / 1024, 1)


def start_server(data_dir: Path, *, yappi_boot: bool = False) -> ServerHandle:
    """
    Spawn the hermetic perf server and wait until it is ready.

    :param data_dir: The (isolated) data directory for this server instance.
    :param yappi_boot: Start yappi before the music_assistant import for boot profiling.
    """
    port, control_port, stream_port = _free_ports()
    env = os.environ.copy()
    if yappi_boot:
        env["PERF_YAPPI_BOOT"] = "1"
    log_file = (data_dir / "server.log").open("a")
    proc = subprocess.Popen(  # noqa: S603
        [
            sys.executable,
            str(PERF_SERVER),
            "--data-dir",
            str(data_dir),
            "--port",
            str(port),
            "--control-port",
            str(control_port),
            "--stream-port",
            str(stream_port),
        ],
        stdout=subprocess.PIPE,
        stderr=log_file,
        text=True,
        cwd=REPO_ROOT,
        env=env,
    )
    handle = ServerHandle(proc, data_dir, log_file)
    try:
        handle.wait_ready()
    except BaseException:
        handle.stop()
        raise
    return handle


# --------------------------------------------------------------------------------------
# scenarios
# --------------------------------------------------------------------------------------


def scenario_import_time() -> dict[str, Any]:
    """Measure the import cost of music_assistant.mass in a fresh interpreter."""
    print("scenario: startup / import time ...", file=sys.stderr)
    code = (
        "import resource, time\n"
        "t0 = time.perf_counter(); c0 = time.process_time()\n"
        "import music_assistant.mass\n"
        "wall = time.perf_counter() - t0; cpu = time.process_time() - c0\n"
        "rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss\n"
        "print(f'IMPORT_RESULT wall={wall:.3f} cpu={cpu:.3f} maxrss={rss}')\n"
    )
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-X", "importtime", "-c", code],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        check=True,
        timeout=300,
    )
    match = re.search(r"IMPORT_RESULT wall=([\d.]+) cpu=([\d.]+) maxrss=(\d+)", result.stdout)
    if not match:
        raise BenchmarkError(f"importtime probe produced no result: {result.stdout[-500:]}")
    # ru_maxrss is bytes on macOS, kilobytes on Linux
    maxrss = int(match.group(3))
    maxrss_mb = maxrss / 1024 / 1024 if sys.platform == "darwin" else maxrss / 1024
    rows: list[tuple[int, int, str]] = []
    for line in result.stderr.splitlines():
        if parsed := re.match(r"import time:\s+(\d+)\s+\|\s+(\d+)\s+\|\s+(.+)$", line):
            rows.append((int(parsed.group(1)), int(parsed.group(2)), parsed.group(3).strip()))
    rows.sort(key=lambda row: -row[0])
    return {
        "import_wall_seconds": float(match.group(1)),
        "import_cpu_seconds": float(match.group(2)),
        "import_peak_rss_mb": round(maxrss_mb, 1),
        "importtime_top": [
            {
                "module": module,
                "self_ms": round(self_us / 1000, 1),
                "cumulative_ms": round(cum_us / 1000, 1),
            }
            for self_us, cum_us, module in rows[:10]
        ],
    }


async def configure_demo_players(ws: PerfWsClient, params: SuiteParams) -> list[str]:
    """Configure the demo player provider and wait for its players to register."""
    await ws.command(
        "config/providers/save",
        {
            "provider_domain": "_demo_player_provider",
            "values": {"number_of_players": params.players},
        },
    )
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        players = (await ws.command("players/all")).result or []
        demo_ids = sorted(p["player_id"] for p in players if p["player_id"].startswith("demo_"))
        if len(demo_ids) >= params.players:
            return demo_ids
        await asyncio.sleep(0.5)
    raise BenchmarkError("demo players did not register within timeout")


async def scenario_initial_sync(
    server: ServerHandle, ws: PerfWsClient, control: ControlClient, params: SuiteParams
) -> dict[str, Any]:
    """Configure the test music provider and measure its first full library sync."""
    print(f"scenario: initial_sync ({params.total_tracks} tracks) ...", file=sys.stderr)
    rss_sampler = PeakRssSampler(server.ps)
    rss_sampler.start()
    cpu_start = cpu_seconds(server.ps)
    wall_start = time.perf_counter()
    await ws.command(
        "config/providers/save",
        {
            "provider_domain": "test",
            "values": {
                "num_artists": params.artists,
                "num_albums": params.albums,
                "num_tracks": params.tracks,
                "num_podcasts": params.podcasts,
                "num_audiobooks": params.audiobooks,
            },
        },
    )
    await control.wait_sync_active()
    wall_end = await control.wait_sync_idle()
    cpu_end = cpu_seconds(server.ps)
    peak_rss_mb = rss_sampler.stop()
    return {
        "wall_seconds": round(wall_end - wall_start, 2),
        "cpu_seconds": round(cpu_end - cpu_start, 2),
        "peak_rss_mb": peak_rss_mb,
        "library_db_mb": server.library_db_mb(),
        "library_tracks": params.total_tracks,
    }


async def scenario_noop_resync(
    server: ServerHandle, ws: PerfWsClient, control: ControlClient
) -> dict[str, Any]:
    """Measure a full re-sync of the already-synced library (the no-change case)."""
    print("scenario: noop_resync ...", file=sys.stderr)
    cpu_start = cpu_seconds(server.ps)
    wall_start = time.perf_counter()
    await ws.command("music/sync")
    wall_end = await control.wait_sync_idle()
    cpu_end = cpu_seconds(server.ps)
    return {
        "wall_seconds": round(wall_end - wall_start, 2),
        "cpu_seconds": round(cpu_end - cpu_start, 2),
    }


def _api_suite(params: SuiteParams) -> list[tuple[str, str, dict[str, Any] | None]]:
    """Return the fixed API command suite as (metric_key, command, args) tuples."""
    deep_offset = max(params.total_tracks - 500, 0)
    return [
        ("artists_page_500", "music/artists/library_items", {"limit": 500}),
        ("albums_page_500", "music/albums/library_items", {"limit": 500}),
        ("tracks_page_500", "music/tracks/library_items", {"limit": 500}),
        (
            "tracks_page_deep_offset",
            "music/tracks/library_items",
            {"limit": 500, "offset": deep_offset},
        ),
        ("tracks_search", "music/tracks/library_items", {"search": "Test Track 1", "limit": 100}),
        (
            "tracks_ordered_by_name",
            "music/tracks/library_items",
            {"order_by": "name", "limit": 200},
        ),
        ("players_all", "players/all", None),
        ("player_queues_all", "player_queues/all", None),
        ("providers_list", "providers", None),
        ("browse_root", "music/browse", None),
    ]


async def scenario_api_bench(
    ws: PerfWsClient, params: SuiteParams, iterations: int
) -> dict[str, Any]:
    """Measure latency and payload size for the fixed API command suite."""
    print(f"scenario: api_bench ({iterations} iterations) ...", file=sys.stderr)
    results: dict[str, Any] = {}
    for key, command, args in _api_suite(params):
        durations_ms: list[float] = []
        payload_bytes = 0
        items = 0
        for _ in range(iterations):
            result = await ws.command(command, args)
            durations_ms.append(result.duration_seconds * 1000)
            payload_bytes = result.payload_bytes
            items = result.items
        if "library_items" in command and items == 0:
            raise BenchmarkError(f"api_bench sanity check failed: {key} returned 0 items")
        median_ms, p95_ms = summarize_latencies(durations_ms)
        results[key] = {
            "median_ms": median_ms,
            "p95_ms": p95_ms,
            "payload_kb": round(payload_bytes / 1024, 1),
            "items": items,
        }
    return results


async def start_flow_playback(
    ws: PerfWsClient, control: ControlClient, player_ids: list[str], server: ServerHandle
) -> list[str]:
    """
    Start flow-mode playback on the given players and return their stream URLs.

    Players are switched to flow mode with WAV output so consumption at the WAV
    byterate equals realtime playback.
    """
    for player_id in player_ids:
        await ws.command(
            "config/players/save",
            {"player_id": player_id, "values": {"flow_mode": True, "output_codec": "wav"}},
        )
    # allow the config change to be applied to the registered players
    await asyncio.sleep(1)
    # a few tracks so the flow stream comfortably outlasts the capture window
    tracks = (await ws.command("music/tracks/library_items", {"limit": 3})).result
    track_uris = [track["uri"] for track in tracks]
    for player_id in player_ids:
        await ws.command(
            "player_queues/play_media",
            {"queue_id": player_id, "media": track_uris},
        )
    deadline = time.monotonic() + PLAYBACK_READY_TIMEOUT
    while time.monotonic() < deadline:
        players = {p["player_id"]: p for p in (await ws.command("players/all")).result or []}
        if all(
            players.get(pid, {}).get("playback_state") == "playing"
            and players.get(pid, {}).get("current_media")
            for pid in player_ids
        ):
            break
        await asyncio.sleep(0.5)
    else:
        raise BenchmarkError("players did not reach playing state within timeout")
    urls: list[str] = []
    for player_id in player_ids:
        url = (await control.post("/resolve_stream_url", {"player_id": player_id}))["url"]
        if "/flow/" not in url:
            raise BenchmarkError(f"expected a flow stream URL for {player_id}, got {url}")
        if f":{server.stream_port}/" not in url:
            raise BenchmarkError(
                f"stream URL {url} does not use this instance's stream port "
                f"{server.stream_port}; refusing to fetch from a foreign server"
            )
        urls.append(url)
    return urls


async def stop_playback(ws: PerfWsClient, player_ids: list[str]) -> None:
    """Stop playback on the given players."""
    for player_id in player_ids:
        await ws.command("players/cmd/stop", {"player_id": player_id})


async def _consume_stream(url: str, duration: int) -> None:
    """Consume a stream URL rate-limited to ~realtime for the given duration."""
    started = time.perf_counter()
    proc = await asyncio.create_subprocess_exec(
        "curl",
        "-sS",
        "--fail",
        "-N",
        "--limit-rate",
        STREAM_RATE_LIMIT,
        "--max-time",
        str(duration),
        "-o",
        "/dev/null",
        "-w",
        "%{http_code} %{size_download}",
        url,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await proc.communicate()
    finally:
        # on cancellation (ctrl-c or a sibling stream failing) curl is not
        # killed automatically and would keep downloading until --max-time
        if proc.returncode is None:
            with contextlib.suppress(OSError):
                proc.kill()
            with contextlib.suppress(asyncio.CancelledError):
                await proc.wait()
    elapsed = time.perf_counter() - started
    # 28 = --max-time reached (expected), 18 = partial transfer on close
    if proc.returncode not in (0, 18, 28):
        raise BenchmarkError(f"curl failed (rc={proc.returncode}): {stderr.decode()[:300]}")
    if elapsed < duration * 0.8:
        raise BenchmarkError(
            f"stream ended prematurely after {elapsed:.1f}s "
            f"(rc={proc.returncode}, http/bytes={stdout.decode()}): {stderr.decode()[:300]}"
        )
    # a connection that stays open but stops sending data would otherwise pass
    http_code, _, downloaded = stdout.decode().strip().partition(" ")
    min_bytes = int(STREAM_RATE_BYTES_PER_SEC * duration * 0.5)
    if http_code != "200" or int(downloaded or 0) < min_bytes:
        raise BenchmarkError(
            f"stream stalled (http={http_code}, downloaded {downloaded} bytes, "
            f"expected >={min_bytes}): {stderr.decode()[:300]}"
        )


async def scenario_streaming(
    server: ServerHandle,
    ws: PerfWsClient,
    control: ControlClient,
    player_ids: list[str],
    duration: int,
) -> dict[str, Any]:
    """Measure CPU cost and loop lag while serving concurrent flow streams."""
    print(f"scenario: streaming ({len(player_ids)} flow streams, {duration}s) ...", file=sys.stderr)
    urls = await start_flow_playback(ws, control, player_ids, server)
    ffmpeg_sampler = FfmpegCpuSampler(server.ps)
    await control.post("/lag/reset")
    cpu_start = cpu_seconds(server.ps)
    ffmpeg_sampler.start()
    wall_start = time.perf_counter()
    # TaskGroup cancels the sibling streams as soon as one fails
    async with asyncio.TaskGroup() as tg:
        for url in urls:
            tg.create_task(_consume_stream(url, duration))
    wall_seconds = time.perf_counter() - wall_start
    cpu_end = cpu_seconds(server.ps)
    ffmpeg_cpu = ffmpeg_sampler.stop()
    max_lag_ms = (await control.get("/lag"))["max_lag_ms"]
    await stop_playback(ws, player_ids)
    return {
        "streams": len(player_ids),
        "capture_seconds": round(wall_seconds, 1),
        "python_cpu_seconds": round(cpu_end - cpu_start, 2),
        "ffmpeg_cpu_seconds": ffmpeg_cpu,
        "max_loop_lag_ms": max_lag_ms,
    }


async def scenario_memory(
    server: ServerHandle,
    ws: PerfWsClient,
    control: ControlClient,
    params: SuiteParams,
    rss_post_boot_mb: float,
    rss_post_sync_mb: float,
) -> dict[str, Any]:
    """
    Measure RSS growth under sustained heavy listing load.

    Two identical rounds of heavy listing requests are performed; the second round
    hitting an already-ratcheted allocator should leave RSS approximately flat -
    growth in round two indicates a leak.
    """
    print(
        f"scenario: memory (2 rounds x {params.memory_requests_per_round} listings) ...",
        file=sys.stderr,
    )
    listing_commands = [
        ("music/tracks/library_items", {"limit": 500}),
        ("music/albums/library_items", {"limit": 500}),
        ("music/artists/library_items", {"limit": 500}),
    ]

    async def _round() -> float:
        for i in range(params.memory_requests_per_round):
            command, args = listing_commands[i % len(listing_commands)]
            await ws.command(command, args)
        await asyncio.sleep(MEDIA_ITEM_EVENTS_SETTLE_SECONDS)
        return await rss_checkpoint_mb(server, control)

    rss_round1 = await _round()
    rss_round2 = await _round()
    return {
        "rss_post_boot_mb": rss_post_boot_mb,
        "rss_post_sync_mb": rss_post_sync_mb,
        "rss_after_round1_mb": rss_round1,
        "rss_after_round2_mb": rss_round2,
        "round2_growth_mb": round(rss_round2 - rss_round1, 1),
        "requests_per_round": params.memory_requests_per_round,
    }


# --------------------------------------------------------------------------------------
# passes
# --------------------------------------------------------------------------------------


async def run_metrics_pass(data_dir: Path, params: SuiteParams) -> dict[str, Any]:
    """Run pass 1: all scenarios unprofiled, on a fresh server, for accurate metrics."""
    scenarios: dict[str, Any] = {}
    print("pass 1/2: metrics (unprofiled) - booting hermetic server ...", file=sys.stderr)
    server = start_server(data_dir)
    try:
        async with aiohttp.ClientSession() as session:
            control = ControlClient(session, server.control_port)
            ws = PerfWsClient(server.port, server.token())
            await ws.connect()
            try:
                startup = {
                    "cold_boot_wall_seconds": server.boot_wall_seconds,
                    "cold_boot_cpu_seconds": server.boot_cpu_seconds,
                    "cold_boot_rss_mb": server.boot_rss_mb,
                }
                demo_players = await configure_demo_players(ws, params)
                scenarios["initial_sync"] = await scenario_initial_sync(server, ws, control, params)
                rss_post_sync_mb = await rss_checkpoint_mb(server, control)
                scenarios["noop_resync"] = await scenario_noop_resync(server, ws, control)
                scenarios["api_bench"] = await scenario_api_bench(ws, params, params.api_iterations)
                scenarios["streaming"] = await scenario_streaming(
                    server,
                    ws,
                    control,
                    demo_players[:STREAMING_PLAYERS],
                    params.streaming_seconds,
                )
                scenarios["memory"] = await scenario_memory(
                    server, ws, control, params, server.boot_rss_mb, rss_post_sync_mb
                )
            finally:
                await ws.close()
    finally:
        server.stop()

    # warm boot: restart on the now-populated data dir (the realistic restart case)
    print("pass 1/2: warm boot on populated library ...", file=sys.stderr)
    server = start_server(data_dir)
    try:
        startup["warm_boot_wall_seconds"] = server.boot_wall_seconds
        startup["warm_boot_cpu_seconds"] = server.boot_cpu_seconds
        startup["warm_boot_rss_mb"] = server.boot_rss_mb
    finally:
        server.stop()

    scenarios["startup"] = {**scenario_import_time(), **startup}
    # fixed scenario order in the report
    order = ["startup", "initial_sync", "noop_resync", "api_bench", "streaming", "memory"]
    return {name: scenarios[name] for name in order}


async def run_profiled_pass(data_dir: Path, params: SuiteParams) -> dict[str, Any]:
    """Run pass 2: fresh server with yappi, capturing per-scenario hotspot attribution."""
    yappi_top: dict[str, Any] = {}
    print("pass 2/2: yappi attribution - booting fresh profiled server ...", file=sys.stderr)
    server = start_server(data_dir, yappi_boot=True)
    try:
        async with aiohttp.ClientSession() as session:
            control = ControlClient(session, server.control_port)
            yappi_top["startup_boot"] = await control.yappi_stop()
            ws = PerfWsClient(server.port, server.token())
            await ws.connect()
            try:
                demo_players = await configure_demo_players(ws, params)

                await control.yappi_start()
                await scenario_initial_sync(server, ws, control, params)
                yappi_top["initial_sync"] = await control.yappi_stop()

                await control.yappi_start()
                await scenario_noop_resync(server, ws, control)
                yappi_top["noop_resync"] = await control.yappi_stop()

                await control.yappi_start()
                await scenario_api_bench(ws, params, params.profiled_api_iterations)
                yappi_top["api_bench"] = await control.yappi_stop()

                player_ids = demo_players[:STREAMING_PLAYERS]
                urls = await start_flow_playback(ws, control, player_ids, server)
                await control.yappi_start()
                async with asyncio.TaskGroup() as tg:
                    for url in urls:
                        tg.create_task(_consume_stream(url, params.profiled_streaming_seconds))
                yappi_top["streaming"] = await control.yappi_stop()
                await stop_playback(ws, player_ids)
            finally:
                await ws.close()
    finally:
        server.stop()
    return yappi_top


# --------------------------------------------------------------------------------------
# env & main
# --------------------------------------------------------------------------------------


def collect_env() -> dict[str, Any]:
    """Collect environment/context info for the report header."""

    def _git(*args: str) -> str:
        return subprocess.run(  # noqa: S603
            ["git", *args],  # noqa: S607
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
            check=True,
        ).stdout.strip()

    if sys.platform == "darwin":
        cpu = subprocess.run(
            ["sysctl", "-n", "machdep.cpu.brand_string"],  # noqa: S607
            capture_output=True,
            text=True,
            check=False,
        ).stdout.strip()
    else:
        cpu = platform.processor() or platform.machine()
        with contextlib.suppress(OSError):
            for line in Path("/proc/cpuinfo").read_text().splitlines():
                if line.lower().startswith("model name"):
                    cpu = line.split(":", 1)[1].strip()
                    break
    ffmpeg = subprocess.run(
        ["ffmpeg", "-version"],  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
    ).stdout.splitlines()
    return {
        "git_sha": _git("rev-parse", "HEAD"),
        "dirty": bool(_git("status", "--porcelain", "--untracked-files=no")),
        "python": platform.python_version(),
        "platform": platform.platform(),
        "cpu": cpu,
        "cpu_count": os.cpu_count(),
        "ffmpeg": ffmpeg[0].split(" version ")[1].split(" ")[0] if ffmpeg else "unknown",
    }


def _free_ports() -> tuple[int, int, int]:
    """
    Pick three distinct currently-free loopback TCP ports.

    The ports are not reserved after this returns; the tiny window until the server
    binds them is an accepted race — a collision makes the server exit and the boot
    fail loudly.
    """
    with socket.socket() as sock1, socket.socket() as sock2, socket.socket() as sock3:
        sock1.bind(("127.0.0.1", 0))
        sock2.bind(("127.0.0.1", 0))
        sock3.bind(("127.0.0.1", 0))
        return sock1.getsockname()[1], sock2.getsockname()[1], sock3.getsockname()[1]


def _check_prerequisites() -> None:
    """Fail fast with a clear message when a required tool or package is missing."""
    if importlib.util.find_spec("yappi") is None:
        sys.exit(
            "The benchmark requires yappi (dev/test extra). "
            "Install with: uv pip install -e '.[test]'"
        )
    for tool in ("curl", "ffmpeg", "git"):
        if shutil.which(tool) is None:
            sys.exit(f"The benchmark requires `{tool}` on PATH.")
    if importlib.util.find_spec("music_assistant") is None:
        sys.exit("music_assistant is not importable; run from the repo venv.")


async def run_suite(params: SuiteParams, keep_data: bool) -> dict[str, Any]:
    """Run the complete two-pass benchmark suite and return the report document."""
    suite_start = time.perf_counter()
    tmp_root = Path(tempfile.mkdtemp(prefix="ma-perf-"))
    try:
        metrics_dir = tmp_root / "metrics"
        profiled_dir = tmp_root / "profiled"
        metrics_dir.mkdir()
        profiled_dir.mkdir()
        scenarios = await run_metrics_pass(metrics_dir, params)
        yappi_top = await run_profiled_pass(profiled_dir, params)
    finally:
        if keep_data:
            print(f"benchmark data kept at {tmp_root}", file=sys.stderr)
        else:
            shutil.rmtree(tmp_root, ignore_errors=True)
    return {
        "meta": {
            "schema_version": SCHEMA_VERSION,
            "quick": params.quick,
            "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "suite_runtime_seconds": round(time.perf_counter() - suite_start, 1),
        },
        "env": collect_env(),
        "scenarios": scenarios,
        "yappi_top": yappi_top,
    }


def main() -> None:
    """Run the benchmark suite CLI."""
    parser = argparse.ArgumentParser(description="Music Assistant performance benchmark suite")
    parser.add_argument("--quick", action="store_true", help="small library, short scenarios")
    parser.add_argument("--out", type=Path, help="write the JSON report to this file")
    parser.add_argument(
        "--save-baseline",
        action="store_true",
        help="save this report as the machine-local baseline for future --compare runs",
    )
    parser.add_argument(
        "--compare",
        nargs="?",
        const="",
        metavar="BASELINE",
        help="compare against a baseline report (default: the machine-local baseline)",
    )
    parser.add_argument(
        "--markdown", action="store_true", help="print a human-readable markdown report"
    )
    parser.add_argument(
        "--keep-data", action="store_true", help="keep the temporary server data dirs"
    )
    args = parser.parse_args()
    _check_prerequisites()

    # resolve and read the baseline upfront: fail before the suite runs, and
    # read the old baseline before --save-baseline may overwrite it
    baseline: dict[str, Any] | None = None
    if args.compare is not None:
        baseline_path = Path(args.compare) if args.compare else _baseline_path(args.quick)
        if not baseline_path.is_file():
            sys.exit(
                f"no baseline found at {baseline_path} - generate one first with --save-baseline"
            )
        baseline = json.loads(baseline_path.read_text())
        if baseline.get("meta", {}).get("quick") != args.quick:
            print(
                "warning: comparing a quick report against a full baseline (or vice versa)",
                file=sys.stderr,
            )

    params = QUICK_PARAMS if args.quick else FULL_PARAMS
    report = asyncio.run(run_suite(params, args.keep_data))

    if args.out:
        args.out.write_text(json.dumps(report, indent=2) + "\n")
        print(f"report written to {args.out}", file=sys.stderr)
    if args.markdown:
        print(render_markdown(report))
    elif not args.out:
        print(json.dumps(report, indent=2))

    if args.save_baseline:
        path = _baseline_path(args.quick)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report, indent=2) + "\n")
        print(f"baseline saved to {path}", file=sys.stderr)

    if baseline is not None:
        lines, regressions = compare_reports(baseline, report)
        print("\n".join(lines))
        if regressions:
            sys.exit(1)


def _baseline_path(quick: bool) -> Path:
    """Return the machine-local baseline path for the given suite mode."""
    return BASELINE_DIR / ("baseline-quick.json" if quick else "baseline-full.json")


if __name__ == "__main__":
    main()
