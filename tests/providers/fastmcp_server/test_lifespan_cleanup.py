"""Regression tests for ASGI lifespan task cleanup on startup failure.

Copilot review on upstream PR music-assistant/server#3858 surfaced that
``_start_asgi_lifespan`` leaked the background lifespan task on two
non-success paths: a startup-ack timeout and an unexpected event type.
Both paths now cancel + drain the task before re-raising.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from music_assistant.providers.fastmcp_server.http_bridge import _start_asgi_lifespan


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
