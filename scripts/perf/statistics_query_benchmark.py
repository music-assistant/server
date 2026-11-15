"""Measure statistics query latency on synthetic completed play history."""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import statistics
import sys
import tempfile
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

# ruff: noqa: T201

try:
    import aiohttp
    from aiohttp import web
except ImportError as err:
    sys.exit(
        f"Missing benchmark dependency: {err}.\n"
        "Install the dev/test extras first: uv pip install -e '.[test]'"
    )

try:
    from scripts.perf.run_benchmark import (
        FULL_PARAMS,
        QUICK_PARAMS,
        BenchmarkError,
        ControlClient,
        ServerHandle,
        start_server,
    )
    from scripts.perf.ws_client import PerfWsClient
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from scripts.perf.run_benchmark import (
        FULL_PARAMS,
        QUICK_PARAMS,
        BenchmarkError,
        ControlClient,
        ServerHandle,
        start_server,
    )
    from scripts.perf.ws_client import PerfWsClient


def _fingerprint(value: Any) -> str:
    """Serialize model results deterministically for repeatability checks."""
    if isinstance(value, list):
        value = [item.to_dict() if hasattr(item, "to_dict") else item for item in value]
    return json.dumps(value, sort_keys=True, default=str)


def _summarize(samples: list[float]) -> dict[str, float]:
    """Return median and nearest-rank p95 latency in milliseconds."""
    ordered = sorted(samples)
    p95_index = min(math.ceil(len(ordered) * 0.95) - 1, len(ordered) - 1)
    return {
        "median_ms": round(statistics.median(ordered), 3),
        "p95_ms": round(ordered[p95_index], 3),
    }


async def _measure(call: Callable[[], Awaitable[Any]]) -> tuple[float, Any]:
    started = time.perf_counter()
    result = await call()
    return (time.perf_counter() - started) * 1000, result


async def _seed_play_history(
    database: Any,
    user_id: str,
    play_count: int,
    track_count: int,
    artist_credits: str | None,
) -> None:
    """Insert synthetic completed library plays efficiently in bounded batches."""
    from music_assistant_models.enums import MediaType  # noqa: PLC0415

    from music_assistant.constants import DB_TABLE_PLAY_HISTORY, DB_TABLE_TRACKS  # noqa: PLC0415

    batch_size = 1_000
    async with database.deferred_commit():
        for batch_start in range(0, play_count, batch_size):
            batch_end = min(batch_start + batch_size, play_count)
            await database.execute(
                f"""
                WITH RECURSIVE
                indexed_tracks AS (
                    SELECT
                        item_id,
                        name,
                        ROW_NUMBER() OVER (ORDER BY item_id) - 1 AS track_index
                    FROM {DB_TABLE_TRACKS}
                ),
                play_indices(play_index) AS (
                    SELECT :batch_start
                    UNION ALL
                    SELECT play_index + 1 FROM play_indices WHERE play_index + 1 < :batch_end
                )
                INSERT INTO {DB_TABLE_PLAY_HISTORY}(
                    item_id, provider, media_type, userid, timestamp, name, artists
                )
                SELECT
                    CAST(indexed_tracks.item_id AS TEXT),
                    'library',
                    :media_type,
                    :user_id,
                    1600000000 + (play_indices.play_index % 8760) * 3600,
                    indexed_tracks.name,
                    :artists
                FROM play_indices
                INNER JOIN indexed_tracks
                    ON indexed_tracks.track_index = play_indices.play_index % :track_count
                """,
                {
                    "batch_start": batch_start,
                    "batch_end": batch_end,
                    "media_type": MediaType.TRACK.value,
                    "user_id": user_id,
                    "artists": artist_credits,
                    "track_count": track_count,
                },
            )


async def _benchmark_user_cases(
    mass: Any,
    user: Any,
    tracks: list[Any],
    iterations: int,
) -> dict[str, Any]:
    """Measure first-call and steady-state latency for each statistics command."""
    from music_assistant_models.enums import MediaType  # noqa: PLC0415

    from music_assistant.controllers.webserver.helpers.auth_middleware import (  # noqa: PLC0415
        set_current_user,
    )

    set_current_user(user)
    try:
        track_id = str(tracks[0]["item_id"])
        cases: dict[str, Callable[[], Awaitable[Any]]] = {
            "top_items_tracks": lambda: mass.statistics.get_top_items(
                MediaType.TRACK, period="all_time"
            ),
            "top_items_albums": lambda: mass.statistics.get_top_items(
                MediaType.ALBUM, period="all_time"
            ),
            "plays_over_time": lambda: mass.statistics.get_plays_over_time(
                period="all_time", granularity="day"
            ),
            "listening_activity": lambda: mass.statistics.get_listening_activity(period="all_time"),
            "listening_time": lambda: mass.statistics.get_listening_time(
                period="all_time", group_by="artist"
            ),
            "play_history": lambda: mass.statistics.get_play_history(
                limit=50, media_types=[MediaType.TRACK]
            ),
            "play_count": lambda track_id=track_id: mass.statistics.get_play_count(
                track_id, "library", MediaType.TRACK
            ),
        }
        results: dict[str, Any] = {}
        for name, call in cases.items():
            first_request_ms, reference = await _measure(call)
            expected = _fingerprint(reference)
            samples: list[float] = []
            for _ in range(iterations):
                elapsed_ms, result = await _measure(call)
                samples.append(elapsed_ms)
                if _fingerprint(result) != expected:
                    raise web.HTTPInternalServerError(
                        text=f"repeated query result mismatch for {name}"
                    )

            results[name] = {
                "result_count": len(reference) if isinstance(reference, list) else int(reference),
                "first_request_ms": round(first_request_ms, 3),
                "steady_state": _summarize(samples),
            }
        return results
    finally:
        set_current_user(None)


async def benchmark_statistics_queries(mass: Any, body: dict[str, Any]) -> dict[str, Any]:
    """Seed isolated synthetic history and measure each statistics command."""
    from music_assistant_models.auth import UserRole  # noqa: PLC0415

    from music_assistant.constants import (  # noqa: PLC0415
        DB_TABLE_ARTISTS,
        DB_TABLE_TRACKS,
    )
    from music_assistant.helpers.json import serialize_to_json  # noqa: PLC0415

    play_counts = body.get("play_counts")
    iterations = body.get("iterations", 20)
    if (
        not isinstance(play_counts, list)
        or not play_counts
        or len(play_counts) > 6
        or any(type(count) is not int or count < 1 or count > 1_000_000 for count in play_counts)
        or sum(play_counts) > 2_000_000
        or type(iterations) is not int
        or iterations < 3
        or iterations > 100
    ):
        raise web.HTTPBadRequest(
            text="play_counts must contain 1-6 values totaling at most 2M; "
            "each must be 1-1M and iterations must be 3-100"
        )

    database = mass.music.database
    tracks = await database.get_rows_from_query(
        f"SELECT item_id, name FROM {DB_TABLE_TRACKS} ORDER BY item_id", {}, limit=0
    )
    artists = await database.get_rows_from_query(
        f"SELECT item_id, name FROM {DB_TABLE_ARTISTS} ORDER BY item_id", {}, limit=1
    )
    if not tracks:
        raise web.HTTPConflict(text="The test provider library is empty; sync it first")

    artist_credits = (
        serialize_to_json(
            [
                {
                    "item_id": str(artists[0]["item_id"]),
                    "provider": "library",
                    "name": artists[0]["name"],
                }
            ]
        )
        if artists
        else None
    )
    results: list[dict[str, Any]] = []
    for dataset_index, play_count in enumerate(play_counts):
        seed_started = time.perf_counter()
        user = await mass.webserver.auth.create_user(
            f"statistics-benchmark-{dataset_index}", role=UserRole.ADMIN
        )
        await _seed_play_history(database, user.user_id, play_count, len(tracks), artist_credits)
        seed_seconds = time.perf_counter() - seed_started
        results.append(
            {
                "play_history_rows": play_count,
                "library_tracks": len(tracks),
                "seed_seconds": round(seed_seconds, 3),
                "iterations": iterations,
                "cases": await _benchmark_user_cases(mass, user, tracks, iterations),
            }
        )
    return {"datasets": results}


async def run_benchmark(
    control: ControlClient,
    play_counts: list[int],
    iterations: int,
) -> dict[str, Any]:
    """Run the in-process benchmark endpoint and return its measurements."""
    assert control._session is not None
    async with control._session.post(
        f"{control._base_url}/statistics/query_benchmark",
        json={"play_counts": play_counts, "iterations": iterations},
    ) as response:
        if response.status >= 400:
            raise BenchmarkError(
                f"statistics cache benchmark failed ({response.status}): {await response.text()}"
            )
        return await response.json()


async def _setup_test_library(
    ws: PerfWsClient,
    control: ControlClient,
    params: Any,
) -> None:
    """Set up the test provider through its supported flow and wait for sync."""
    step = (await ws.command("config/providers/setup", {"provider_domain": "test"})).result
    instance_id = step.get("result", {}).get("instance_id") if isinstance(step, dict) else None
    if not instance_id:
        raise BenchmarkError(f"test provider setup did not finish: {step}")
    await ws.command(
        "config/providers/save",
        {
            "provider_domain": "test",
            "instance_id": instance_id,
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
    await control.wait_sync_idle()


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--play-counts",
        type=int,
        nargs="+",
        default=[10_000, 100_000],
        help="synthetic history sizes per user (default: 10000 100000; maximum total: 2000000)",
    )
    parser.add_argument(
        "--iterations",
        type=int,
        default=20,
        help="repeated query samples per statistics command (3-100)",
    )
    parser.add_argument(
        "--full-library",
        action="store_true",
        help="sync the full 10000-track test library instead of the quick 1000-track library",
    )
    return parser.parse_args()


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    params = FULL_PARAMS if args.full_library else QUICK_PARAMS
    with tempfile.TemporaryDirectory(prefix="ma-statistics-cache-") as temp_dir:
        data_dir = Path(temp_dir) / "data"
        data_dir.mkdir()
        server: ServerHandle = start_server(data_dir)
        try:
            async with aiohttp.ClientSession() as session:
                control = ControlClient(session, server.control_port)
                ws = PerfWsClient(server.port, server.token())
                await ws.connect()
                try:
                    await _setup_test_library(ws, control, params)
                    report = await run_benchmark(control, args.play_counts, args.iterations)
                    return {
                        "methodology": {
                            "statistics_result_cache": "disabled",
                            "database_cache": "normal SQLite page cache",
                            "timings": "first request plus repeated steady-state calls",
                            "history": "synthetic completed library track plays in isolated temp data",
                            "library_tracks": params.total_tracks,
                        },
                        **report,
                    }
                finally:
                    await ws.close()
        except Exception as err:
            log_path = data_dir / "server.log"
            log_tail = (
                "\n".join(log_path.read_text().splitlines()[-80:]) if log_path.exists() else ""
            )
            raise BenchmarkError(f"{err}\nPerf-server log:\n{log_tail}") from err
        finally:
            server.stop()


def main() -> None:
    """Start an isolated server and print cache benchmark results as JSON."""
    args = _parse_args()
    if (
        not args.play_counts
        or len(args.play_counts) > 6
        or any(count < 1 or count > 1_000_000 for count in args.play_counts)
        or sum(args.play_counts) > 2_000_000
        or not 3 <= args.iterations <= 100
    ):
        raise BenchmarkError(
            "provide 1-6 play counts between 1 and 1M (total at most 2M) and 3-100 iterations"
        )
    print(json.dumps(asyncio.run(_run(args)), indent=2))


if __name__ == "__main__":
    main()
