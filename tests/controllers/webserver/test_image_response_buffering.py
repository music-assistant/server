"""Tests for bounded WebSocket image command responses."""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, Mock

import pytest
from aiohttp import WSMsgType
from aiohttp.test_utils import make_mocked_request
from music_assistant_models.api import CommandMessage
from music_assistant_models.auth import User, UserRole
from music_assistant_models.errors import AuthenticationRequired, ResourceBusyError

from music_assistant.controllers.webserver import websocket_client
from music_assistant.controllers.webserver.websocket_client import WebsocketClientHandler
from music_assistant.helpers.api import APICommandHandler

if TYPE_CHECKING:
    from aiohttp import web

    from music_assistant.controllers.webserver import WebserverController


def create_client(target: AsyncMock) -> WebsocketClientHandler:
    """Create an image-capable client without a real socket."""
    handler = APICommandHandler.parse("metadata/get_image", target)
    # AsyncMock's variadic signature is not an API command signature.
    handler.signature = inspect.Signature()
    handler.type_hints = {}
    mass = SimpleNamespace(
        command_handlers={"metadata/get_image": handler},
        create_task=asyncio.create_task,
        metadata=SimpleNamespace(compute_image_id=lambda *_args: "image"),
        translations=SimpleNamespace(get_translation=lambda *_args, **_kwargs: None),
    )
    client = WebsocketClientHandler(
        cast("WebserverController", SimpleNamespace(mass=mass, logger=logging.getLogger(__name__))),
        make_mocked_request("GET", "/ws"),
    )
    client._authenticated_user = User(user_id="test", username="test", role=UserRole.ADMIN)
    return client


def command(message_id: int = 1) -> CommandMessage:
    """Create an image request."""
    return CommandMessage(message_id=str(message_id), command="metadata/get_image", args={})


def mock_socket(send: object) -> web.WebSocketResponse:
    """Create a socket test double with the writer's required attributes."""
    return cast("web.WebSocketResponse", SimpleNamespace(closed=False, send_str=send))


async def wait_for_queue(client: WebsocketClientHandler, count: int) -> None:
    """Wait for executor serialization to finish."""
    async with asyncio.timeout(3):
        while client._to_write.qsize() < count:
            await asyncio.sleep(0.001)


async def stop_client(client: WebsocketClientHandler) -> None:
    """Cancel and await owned tasks."""
    tasks = list(client._image_tasks)
    client.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    if client._writer_task:
        await client._writer_task


@pytest.mark.parametrize("eager", [False, True])
async def test_image_admission_held_until_write(
    eager: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Slow writes retain both reservations while excess requests get typed errors."""
    target = AsyncMock(return_value={"data": "x" * 100_000})
    client = create_client(target)
    if eager:
        monkeypatch.setattr(
            client.mass,
            "create_task",
            lambda coro: asyncio.Task(coro, loop=asyncio.get_running_loop(), eager_start=True),
        )
    started = asyncio.Event()
    release = asyncio.Event()

    async def send(_message: str) -> None:
        started.set()
        await release.wait()

    client.wsock = mock_socket(send)
    client._writer_task = asyncio.create_task(client._writer())
    try:
        await client._handle_command(command(1))
        await client._handle_command(command(2))
        await started.wait()
        await wait_for_queue(client, 1)
        for message_id in range(3, 13):
            await client._handle_command(command(message_id))
        assert len(client._image_tasks) == 2
        assert target.await_count == 2
        queued = [client._to_write.get_nowait() for _ in range(client._to_write.qsize())]
        for item in queued:
            client._to_write.put_nowait(item)
        errors = [json.loads(item) for item in queued if isinstance(item, str)]
        assert len(errors) == 10
        assert all(item["error_code"] == ResourceBusyError.error_code for item in errors)
        tasks = list(client._image_tasks)
        release.set()
        await asyncio.gather(*tasks)
        assert not client._image_tasks
        await client._handle_command(command(13))
        await asyncio.gather(*client._image_tasks)
        assert target.await_count == 3
    finally:
        await stop_client(client)


async def test_nonimage_response_does_not_wait_for_writer() -> None:
    """Non-image commands keep the existing enqueue-only response behavior."""
    client = create_client(AsyncMock(return_value={"ok": True}))
    handler = client.mass.command_handlers["metadata/get_image"]
    await client._run_handler(handler, CommandMessage("1", "server/info", {}))
    response = client._to_write.get_nowait()
    assert isinstance(response, str)
    assert json.loads(response)["result"] == {"ok": True}
    assert not client._image_tasks


async def test_image_authentication_before_admission() -> None:
    """Unauthenticated image requests neither start a task nor reserve capacity."""
    target = AsyncMock()
    client = create_client(target)
    client._authenticated_user = None
    await client._handle_command(command())
    queued = client._to_write.get_nowait()
    assert isinstance(queued, str)
    response = json.loads(queued)
    assert response["error_code"] == AuthenticationRequired.error_code
    assert not client._image_tasks
    target.assert_not_called()


async def test_disconnect_before_image_task_starts() -> None:
    """The done callback releases admission even if the coroutine never starts."""
    target = AsyncMock(return_value={"data": "image"})
    client = create_client(target)
    await client._handle_command(command())
    assert len(client._image_tasks) == 1
    await stop_client(client)
    assert not client._image_tasks
    target.assert_not_called()


async def test_disconnect_cancels_running_image_commands() -> None:
    """Disconnect cancels fetches and releases reservations."""
    started = asyncio.Event()

    async def fetch() -> None:
        started.set()
        await asyncio.Event().wait()

    client = create_client(AsyncMock(side_effect=fetch))
    await client._handle_command(command())
    await started.wait()
    await stop_client(client)
    assert not client._image_tasks
    await client._handle_command(command(2))
    assert not client._image_tasks


@pytest.mark.parametrize("failure", [ConnectionResetError, RuntimeError])
async def test_writer_failure_releases_images(failure: type[Exception]) -> None:
    """Writer errors cancel image commands and discard buffered payloads."""
    client = create_client(AsyncMock(return_value={"data": "image"}))
    await client._handle_command(command(1))
    await client._handle_command(command(2))
    await wait_for_queue(client, 2)
    tasks = list(client._image_tasks)
    client.wsock = mock_socket(AsyncMock(side_effect=failure))
    await client._writer()
    await asyncio.gather(*tasks, return_exceptions=True)
    assert not client._image_tasks
    assert client._to_write.empty()


async def test_image_write_deadline_disconnects(monkeypatch: pytest.MonkeyPatch) -> None:
    """A stuck writer cannot retain image reservations indefinitely."""
    monkeypatch.setattr(websocket_client, "IMAGE_SEND_TIMEOUT", 0.01)
    client = create_client(AsyncMock(return_value={"data": "image"}))

    async def send(_message: str) -> None:
        await asyncio.Event().wait()

    client.wsock = mock_socket(send)
    client._writer_task = asyncio.create_task(client._writer())
    await client._handle_command(command())
    tasks = list(client._image_tasks)
    await asyncio.gather(*tasks, return_exceptions=True)
    await client._writer_task
    assert client._closing
    assert not client._image_tasks
    assert client._to_write.empty()


async def test_full_queue_releases_image_admission() -> None:
    """A full generic queue closes admission rather than leaving image tasks waiting."""
    client = create_client(AsyncMock(return_value={"data": "image"}))
    client._to_write = asyncio.Queue(maxsize=1)
    client._to_write.put_nowait("event")
    await client._handle_command(command())
    await asyncio.gather(*client._image_tasks, return_exceptions=True)
    assert client._closing
    assert not client._image_tasks


@pytest.mark.parametrize("queue_full", [False, True])
async def test_invalid_json_closes_blocked_image_writer(
    monkeypatch: pytest.MonkeyPatch, queue_full: bool
) -> None:
    """Invalid JSON shuts down a blocked image write even after its deadline is cancelled."""
    monkeypatch.setattr(websocket_client, "IMAGE_SEND_TIMEOUT", 0.05)
    client = create_client(AsyncMock(return_value={"data": "image"}))
    unregister = Mock()
    disconnected = Mock()
    monkeypatch.setattr(client.webserver, "auth", SimpleNamespace(has_users=True), raising=False)
    monkeypatch.setattr(client.webserver, "unregister_websocket_client", unregister, raising=False)
    monkeypatch.setattr(
        client.mass,
        "dashboard",
        SimpleNamespace(handle_client_disconnected=disconnected),
        raising=False,
    )
    monkeypatch.setattr(client.mass, "get_server_info", lambda: command(), raising=False)
    started = asyncio.Event()
    cancelled = asyncio.Event()
    image_tasks: list[asyncio.Task[object]] = []

    async def send(message: str) -> None:
        if "result" not in json.loads(message):
            return
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    async def receive() -> SimpleNamespace:
        if receive_mock.await_count != 1:
            await started.wait()
            image_tasks.extend(client._image_tasks)
            if queue_full:
                while not client._to_write.full():
                    client._to_write.put_nowait("event")
            return SimpleNamespace(type=WSMsgType.TEXT, data="invalid JSON")
        return SimpleNamespace(type=WSMsgType.TEXT, data=command().to_json())

    receive_mock = AsyncMock(side_effect=receive)
    close = AsyncMock()
    client.wsock = cast(
        "web.WebSocketResponse",
        SimpleNamespace(
            closed=False, prepare=AsyncMock(), receive=receive_mock, send_str=send, close=close
        ),
    )
    async with asyncio.timeout(3):
        assert await client.handle_client() is client.wsock
    assert cancelled.is_set()
    assert image_tasks
    assert all(task.done() for task in image_tasks)
    assert not client._image_tasks
    assert client._writer_task is not None
    assert client._writer_task.done()
    assert client._to_write.empty()
    close.assert_awaited_once()
    unregister.assert_called_once_with(client)
    disconnected.assert_called_once_with(client.client_id)


async def test_image_handler_error_waits_for_writer() -> None:
    """Operational errors hold admission until the error response is sent too."""
    client = create_client(AsyncMock(side_effect=ResourceBusyError("Image unavailable")))
    await client._handle_command(command())
    await wait_for_queue(client, 1)
    assert len(client._image_tasks) == 1
    send = AsyncMock()
    client.wsock = mock_socket(send)
    client._writer_task = asyncio.create_task(client._writer())
    await asyncio.gather(*client._image_tasks)
    assert not client._image_tasks
    assert send.call_args is not None
    assert json.loads(send.call_args.args[0])["error_code"] == ResourceBusyError.error_code
    await stop_client(client)
