"""
Websocket API client for the Music Assistant performance benchmark.

A minimal asyncio client for the MA websocket API with a single reader task that
routes command results to their waiters and fans events out to subscribers, so
events are never lost while a command result is being awaited.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import statistics
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

import aiohttp

COMMAND_TIMEOUT = 300
MAX_WS_MSG_SIZE = 64 * 1024 * 1024


@dataclass
class CommandResult:
    """Outcome of a single websocket API command."""

    result: Any
    duration_seconds: float
    payload_bytes: int
    items: int


@dataclass
class _PendingCommand:
    """Bookkeeping for an in-flight command awaiting its (partial) result(s)."""

    future: asyncio.Future[CommandResult]
    sent_at: float
    payload_bytes: int = 0
    items: int = 0
    partial_results: list[Any] = field(default_factory=list)


class PerfWsClient:
    """Websocket API client with command latency/payload accounting and event capture."""

    def __init__(self, port: int, token: str) -> None:
        """
        Initialize the client.

        :param port: The (loopback) webserver port of the benchmark server.
        :param token: A long-lived auth token for an admin user.
        """
        self.url = f"ws://127.0.0.1:{port}/ws"
        self.token = token
        self._session: aiohttp.ClientSession | None = None
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._pending: dict[str, _PendingCommand] = {}
        self._event_waiters: list[tuple[str, asyncio.Future[dict[str, Any]]]] = []

    async def connect(self) -> None:
        """Connect, consume the server-info message and authenticate."""
        self._session = aiohttp.ClientSession()
        self._ws = await self._session.ws_connect(self.url, max_msg_size=MAX_WS_MSG_SIZE)
        # first message is the server info
        await self._ws.receive_json()
        self._reader_task = asyncio.create_task(self._reader())
        result = await self.command("auth", {"token": self.token})
        if not (isinstance(result.result, dict) and result.result.get("authenticated")):
            raise RuntimeError(f"websocket auth failed: {result.result}")

    async def close(self) -> None:
        """Close the websocket connection."""
        if self._reader_task:
            self._reader_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reader_task
        if self._ws:
            await self._ws.close()
        if self._session:
            await self._session.close()

    async def command(
        self, command: str, args: dict[str, Any] | None = None, timeout: float = COMMAND_TIMEOUT
    ) -> CommandResult:
        """
        Send an API command and wait for its full (possibly chunked) result.

        :param command: The API command to execute (e.g. music/tracks/library_items).
        :param args: Optional arguments for the command.
        :param timeout: Maximum seconds to wait for the (final) result.
        """
        assert self._ws is not None
        message_id = uuid.uuid4().hex
        payload: dict[str, Any] = {"message_id": message_id, "command": command}
        if args:
            payload["args"] = args
        pending = _PendingCommand(
            future=asyncio.get_running_loop().create_future(), sent_at=time.perf_counter()
        )
        self._pending[message_id] = pending
        try:
            await self._ws.send_str(json.dumps(payload))
            return await asyncio.wait_for(pending.future, timeout=timeout)
        finally:
            self._pending.pop(message_id, None)

    async def wait_for_event(self, event: str, timeout: float) -> dict[str, Any]:
        """
        Wait until the server pushes the given event type.

        :param event: The event type value to wait for (e.g. music_sync_completed).
        :param timeout: Maximum seconds to wait.
        """
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        waiter = (event, future)
        self._event_waiters.append(waiter)
        try:
            return await asyncio.wait_for(future, timeout=timeout)
        finally:
            if waiter in self._event_waiters:
                self._event_waiters.remove(waiter)

    async def _reader(self) -> None:
        """Route incoming messages to pending commands and event waiters."""
        assert self._ws is not None
        try:
            async for msg in self._ws:
                if msg.type != aiohttp.WSMsgType.TEXT:
                    break
                self._handle_message(json.loads(msg.data), len(msg.data.encode()))
        finally:
            # fail fast on a closed/errored connection instead of letting callers
            # wait out their full command timeouts
            error = ConnectionError("websocket connection closed")
            for pending in self._pending.values():
                if not pending.future.done():
                    pending.future.set_exception(error)
            for _, future in self._event_waiters:
                if not future.done():
                    future.set_exception(error)

    def _handle_message(self, data: dict[str, Any], size_bytes: int) -> None:
        """Dispatch a single incoming message to its pending command or event waiters."""
        if event := data.get("event"):
            for waiter_event, future in list(self._event_waiters):
                if waiter_event == event and not future.done():
                    future.set_result(data)
            return
        message_id = data.get("message_id")
        if not message_id or not (pending := self._pending.get(message_id)):
            return
        pending.payload_bytes += size_bytes
        if "error_code" in data:
            if not pending.future.done():
                pending.future.set_exception(
                    RuntimeError(f"command failed: {data.get('details') or data['error_code']}")
                )
            return
        result = data.get("result")
        if isinstance(result, list):
            pending.items += len(result)
            pending.partial_results.extend(result)
        if data.get("partial"):
            return
        if not pending.future.done():
            final_result = result
            if pending.partial_results and isinstance(result, list):
                final_result = pending.partial_results
            pending.future.set_result(
                CommandResult(
                    result=final_result,
                    duration_seconds=time.perf_counter() - pending.sent_at,
                    payload_bytes=pending.payload_bytes,
                    items=pending.items,
                )
            )


def summarize_latencies(durations_ms: list[float]) -> tuple[float, float]:
    """Return (median, p95) for a list of latency samples in milliseconds."""
    median = statistics.median(durations_ms)
    if len(durations_ms) >= 20:
        p95 = statistics.quantiles(durations_ms, n=20)[18]
    else:
        p95 = max(durations_ms)
    return round(median, 1), round(p95, 1)
