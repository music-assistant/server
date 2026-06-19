"""
Regression tests for ASGI lifespan task cleanup.

Two failure shapes are pinned here:

1. ``_start_asgi_lifespan`` must not leak the background lifespan task on
   non-success paths (startup-ack timeout, unexpected event type). Both
   paths now cancel + drain the task before re-raising. Originally
   surfaced by Copilot review on upstream PR music-assistant/server#3858.

2. The unmount returned by ``mount_into_mass`` must fully drain the
   lifespan before returning — not schedule the shutdown as a
   fire-and-forget task. Previously a fire-and-forget ``create_task``
   inside the unmount closure raced restarts (and could be GC'd before
   running), leaking the FastMCP session-manager task group on every
   permission hot-swap that rebuilt the runtime.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import MagicMock

import pytest

from music_assistant.providers.fastmcp_server.http_bridge import (
    _start_asgi_lifespan,
    mount_into_mass,
)


async def _silent_asgi(scope: dict[str, Any], receive: Any, _send: Any) -> None:
    """ASGI app that consumes the lifespan startup message but never acks."""
    if scope.get("type") != "lifespan":
        return
    await receive()
    # Stay alive until cancelled by the caller's cleanup.
    await asyncio.Event().wait()


async def _weird_event_asgi(scope: dict[str, Any], receive: Any, send: Any) -> None:
    """ASGI app that sends a non-standard event in place of startup.complete."""
    if scope.get("type") != "lifespan":
        return
    await receive()
    await send({"type": "lifespan.something_unexpected"})
    await asyncio.Event().wait()


async def test_startup_timeout_does_not_leak_lifespan_task() -> None:
    """A silent ASGI app must not leave a runaway lifespan task on timeout."""
    tasks_before = asyncio.all_tasks()

    with pytest.raises(asyncio.TimeoutError):
        await _start_asgi_lifespan(_silent_asgi, startup_timeout=0.05)

    tasks_after = asyncio.all_tasks() - {asyncio.current_task()}
    leaked = tasks_after - tasks_before
    assert not leaked, f"Lifespan task leaked after startup timeout: {leaked!r}"


async def test_unexpected_event_does_not_leak_lifespan_task() -> None:
    """An unrecognised lifespan event must not leave a runaway task either."""
    tasks_before = asyncio.all_tasks()

    with pytest.raises(RuntimeError, match="Unexpected ASGI lifespan event"):
        await _start_asgi_lifespan(_weird_event_asgi, startup_timeout=5)

    tasks_after = asyncio.all_tasks() - {asyncio.current_task()}
    leaked = tasks_after - tasks_before
    assert not leaked, f"Lifespan task leaked after unexpected event: {leaked!r}"


async def _well_behaved_asgi(scope: dict[str, Any], receive: Any, send: Any) -> None:
    """ASGI app that acks startup and shutdown promptly, like a real server."""
    if scope.get("type") != "lifespan":
        return
    while True:
        message = await receive()
        if message["type"] == "lifespan.startup":
            await send({"type": "lifespan.startup.complete"})
        elif message["type"] == "lifespan.shutdown":
            await send({"type": "lifespan.shutdown.complete"})
            return


async def test_unmount_drains_lifespan_before_returning() -> None:
    """
    ``unmount()`` must await shutdown — not schedule it fire-and-forget.

    Previously, ``mount_into_mass`` returned a sync closure that wrapped
    ``_stop_asgi_lifespan`` in ``asyncio.create_task`` and returned
    immediately, so ``MCPServerRuntime.stop`` returned while the lifespan
    was still running. On the next ``start()`` (e.g. a resource-toggle
    hot-swap) two FastMCP instances raced over the same mount, and the
    orphan task could be GC'd before it completed.
    """
    mass = MagicMock()
    mass.webserver.base_url = "http://localhost:8095"
    mass.webserver.publish_ip = "127.0.0.1"
    mass.webserver.register_dynamic_route = MagicMock(return_value=lambda: None)

    mcp = MagicMock()
    mcp.http_app = MagicMock(return_value=_well_behaved_asgi)

    tasks_before = asyncio.all_tasks()
    unmount = await mount_into_mass(mass, mcp, mount_path="/mcp/v1")
    await unmount()

    # No intervening ``await asyncio.sleep(...)`` — if the unmount actually
    # awaited the shutdown, the lifespan task must be done by now.
    tasks_after = asyncio.all_tasks() - {asyncio.current_task()}
    leaked = tasks_after - tasks_before
    assert not leaked, (
        f"unmount() returned with the lifespan still running: {leaked!r}. "
        f"This is the fire-and-forget create_task regression."
    )
