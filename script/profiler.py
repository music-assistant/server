"""
Helper to trace memory usage.

https://www.red-gate.com/simple-talk/development/python/memory-profiling-in-python-with-tracemalloc/
"""
import asyncio
import tracemalloc

# ruff: noqa: D103,E501,E741

# list to store memory snapshots
snaps = []


def _take_snapshot():
    snaps.append(tracemalloc.take_snapshot())


async def take_snapshot():
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _take_snapshot)


def _display_stats():
    stats = snaps[0].statistics("filename")
    print("\n*** top 5 stats grouped by filename ***")
    for s in stats[:5]:
        print(s)


async def display_stats():
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _display_stats)


def compare():
    first = snaps[0]
    for snapshot in snaps[1:]:
        stats = snapshot.compare_to(first, "lineno")
        print("\n*** top 10 stats ***")
        for s in stats[:10]:
            print(s)


def print_trace():
    # pick the last saved snapshot, filter noise
    snapshot = snaps[-1].filter_traces(
        (
            tracemalloc.Filter(False, "<frozen importlib._bootstrap>"),
            tracemalloc.Filter(False, "<frozen importlib._bootstrap_external>"),
            tracemalloc.Filter(False, "<unknown>"),
        )
    )
    largest = snapshot.statistics("traceback")[0]

    print(
        f"\n*** Trace for largest memory block - ({largest.count} blocks, {largest.size/1024} Kb) ***"
    )
    for l in largest.traceback.format():
        print(l)
