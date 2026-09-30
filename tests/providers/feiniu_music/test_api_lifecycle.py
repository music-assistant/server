"""Real MA command execution during instance unload, without a WebSocket transport."""

import asyncio
import json
import logging
import threading
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from music_assistant_models.api import CommandMessage, ErrorResultMessage, SuccessResultMessage
from music_assistant_models.auth import User, UserRole
from music_assistant_models.errors import ResourceTemporarilyUnavailable
from music_assistant_models.provider import ProviderManifest

from music_assistant.controllers.discovery.controller import DiscoveryController
from music_assistant.controllers.music.controller import MusicController
from music_assistant.controllers.tasks.controller import TasksController
from music_assistant.controllers.webserver.websocket_client import WebsocketClientHandler
from music_assistant.helpers import aiohttp_client, throttle_retry
from music_assistant.mass import MusicAssistant
from music_assistant.providers.feiniu_music import client as client_module
from music_assistant.providers.feiniu_music import provider as provider_module
from music_assistant.providers.feiniu_music.provider import FeiNiuProvider

from .test_provider import track_data


def make_mass(tmp_path: Path) -> Any:
    """Construct real core controllers with only config/DB boundaries supplied."""
    mass: Any = MusicAssistant(str(tmp_path / "storage"), str(tmp_path / "cache"))
    mass.loop = asyncio.get_running_loop()
    mass.loop_thread_id = threading.get_ident()
    mass.config = SimpleNamespace(
        initialized=False,
        onboard_done=True,
        get=lambda _key, default=None: default,
        get_raw_core_config_value=lambda _domain, _key, default=None: default,
    )
    mass.cache = SimpleNamespace()
    mass.music = MusicController(mass)
    mass.tasks = TasksController(mass)
    mass.discovery = DiscoveryController(mass)
    mass.discovery.config = SimpleNamespace(get_value=lambda *_args: False)
    mass.webserver = SimpleNamespace(
        server_name="Synthetic test",
        base_url="http://localhost",
        external_url=None,
        remote_access=SimpleNamespace(is_enabled=False),
    )
    mass.music.albums.get_library_item_by_prov_id = AsyncMock(return_value=None)
    mass.register_api_command("info", mass.get_server_info, authenticated=False)
    assert mass.command_handlers["music/albums/album_tracks"].target == mass.music.albums.tracks
    return mass


def wire_handler(mass: Any, env: Any) -> None:
    """Record real command result objects without adding a task before the provider."""
    handler: Any = object.__new__(WebsocketClientHandler)
    handler.mass = mass
    handler._logger = logging.getLogger("feiniu-api-lifecycle-test")
    handler._authenticated_user = User(
        user_id="synthetic-admin", username="test", role=UserRole.ADMIN
    )
    handler._current_token = "synthetic-api-token"
    handler._sendspin_player_id = None
    handler.client_id = "synthetic-connection"

    async def send_message(message: Any) -> None:
        if isinstance(message, ErrorResultMessage):
            env.send_started.set()
            if env.hold_error:
                await env.send_release.wait()
        env.messages.append(message)

    handler._send_message = send_message

    def create_task(*args: Any, **kwargs: Any) -> asyncio.Task[Any]:
        task = MusicAssistant.create_task(mass, *args, **kwargs)
        env.tasks.append(task)
        return task

    mass.create_task = create_task

    async def dispatch(command: str, message_id: str, **args: Any) -> asyncio.Task[Any]:
        before = len(env.tasks)
        await handler._handle_command(
            CommandMessage(command=command, message_id=message_id, args=args)
        )
        assert len(env.tasks) == before + 1
        return cast("asyncio.Task[Any]", env.tasks[-1])

    env.mass, env.handler, env.dispatch = mass, handler, dispatch


async def handle_http(env: Any, request: web.Request) -> web.StreamResponse:
    """Serve synthetic native responses with a controllable body wait."""
    env.requests.append(request.path)
    login = request.path.endswith("/user/password-login")
    relation = request.path.endswith("/track/album-detail/list")
    if login:
        name = (await request.json())["username"]
        data: dict[str, Any] = {"userToken": f"synthetic-{name}", "user": {"guid": name}}
        if not env.hold_login:
            return web.json_response({"code": 0, "data": data})
    elif relation:
        assert request.query["albumGUID"] == "album-test"
        if env.expire_session:
            env.expire_session = False
            return web.json_response({"code": 120001})
        data = {"list": [track_data()], "total": 1}
    elif request.path.endswith("/user/me"):
        account = request.cookies["music-token"].removeprefix("synthetic-")
        return web.json_response({"code": 0, "data": {"guid": account}})
    else:
        assert request.path == "/health"
        return web.json_response({"ok": True})
    response = web.StreamResponse(
        status=env.status, headers={"Content-Type": "application/json", "X-Test-Held": "1"}
    )
    await response.prepare(request)
    await response.write(b'{"code":0,"data":')
    env.started.set()
    try:
        await env.release.wait()
        await response.write(json.dumps(data).encode() + b"}")
        await response.write_eof()
    except ConnectionResetError:
        pass
    finally:
        env.finished.set()
    return response


@pytest.fixture
async def api_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> AsyncGenerator[Any]:
    """Wire real dispatch, controllers and clients to an Event-gated HTTP service."""
    env = SimpleNamespace(
        status=200,
        hold_login=False,
        expire_session=False,
        started=asyncio.Event(),
        release=asyncio.Event(),
        finished=asyncio.Event(),
        headers=asyncio.Event(),
        requests=[],
        responses=[],
        messages=[],
        tasks=[],
        send_started=asyncio.Event(),
        send_release=asyncio.Event(),
        hold_error=False,
    )

    async def capture_headers(_session: Any, _context: Any, params: Any) -> None:
        if params.response.headers.get("X-Test-Held") == "1":
            env.responses.append(params.response)
            env.headers.set()

    mass = make_mass(tmp_path)
    wire_handler(mass, env)
    trace = aiohttp.TraceConfig()
    trace.on_request_end.append(capture_headers)
    # Numeric loopback targets do not need MA's running mDNS service.
    monkeypatch.setattr(aiohttp_client, "_get_resolver", lambda _mass: aiohttp.ThreadedResolver())
    mass._http_session = aiohttp_client.create_clientsession(mass, trace_configs=[trace])
    env.shared = mass.http_session
    app = web.Application()
    app.router.add_route("*", "/{path:.*}", partial(handle_http, env))
    async with TestServer(app) as server:
        env.server = server
        try:
            for name in ("a", "b"):
                manifest = ProviderManifest.from_dict(
                    json.loads(
                        Path(provider_module.__file__).with_name("manifest.json").read_text()
                    )
                )
                config: Any = SimpleNamespace(
                    instance_id=f"feiniu-{name}", name=name, get_value=lambda *_: None
                )
                instance: Any = FeiNiuProvider(mass, manifest, config)
                values = {
                    "url": str(server.make_url("/music/")),
                    "username": name,
                    "password": "synthetic-secret",
                    "device_id": "a" * 32,
                }
                instance.get_setup_value = values.get
                await instance.handle_async_init()
                instance.available = True
                instance.initialized.set()
                mass._providers[instance.instance_id] = instance
                setattr(env, name, instance)
            assert env.a._client._session is env.b._client._session is env.shared
            yield env
        finally:
            env.release.set()
            env.send_release.set()
            for task in env.tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*env.tasks, return_exceptions=True)
            for instance in list(mass.providers):
                await mass.unload_provider(instance.instance_id)
            await env.shared.close()
            assert not mass._tracked_timers


async def prepare_wait(
    env: Any, monkeypatch: pytest.MonkeyPatch, mode: str
) -> tuple[asyncio.Event, asyncio.Lock | None]:
    """Select the HTTP, login, or admission wait without changing task ownership."""
    client = env.a._client
    entered = asyncio.Event()
    lock = None
    if mode in {"throttle", "cooldown"}:
        manager = client._throttler
        acquire = manager.acquire

        @asynccontextmanager
        async def observe_wait() -> AsyncGenerator[float]:
            entered.set()
            async with acquire() as delay:
                yield delay

        monkeypatch.setattr(manager, "acquire", observe_wait)
        if mode == "cooldown":
            # Stay below MA's fail-fast limit; older helpers have no MAX_WAIT_TIME.
            manager.set_cooldown(min(getattr(throttle_retry, "MAX_WAIT_TIME", 10) / 2, 5))
        else:
            manager.throttler.period = 3600
    if mode in {"login_lock", "login_body"}:
        env.expire_session = True
        env.hold_login = True
        if mode == "login_lock":
            lock = client_module._LOGIN_LOCKS[(asyncio.get_running_loop(), client._origin)] = (
                asyncio.Lock()
            )
            await lock.acquire()
            login = client.login

            async def observe_login(*args: Any) -> Any:
                entered.set()
                return await login(*args)

            monkeypatch.setattr(client, "login", observe_login)
    if mode == "provider_error":
        env.status = 403
    env.hold_error = mode == "send_wait"
    return entered, lock


async def finish_command(
    env: Any,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    command: asyncio.Task[Any],
    responses: list[Any],
) -> tuple[Any, object]:
    """Finish or cancel through the real MA unload path and controlled ordering."""
    client = env.a._client
    external = object()
    unload = None
    if mode == "external":
        command.cancel(external)
    elif mode in {"success", "provider_error"}:
        env.release.set()
    else:
        if mode == "external_first":
            command.cancel(external)
        if mode in {"external_first", "unload_first"}:
            # Queue unload first and the second action synchronously at its gather
            # boundary, before command can unwind. No wall-clock race or child caller.
            gather = asyncio.gather

            def observed_gather(*aws: Any, **kwargs: Any) -> Any:
                if any(aw is command for aw in aws) and mode == "unload_first":
                    command.cancel(external)
                return gather(*aws, **kwargs)

            monkeypatch.setattr(asyncio, "gather", observed_gather)
        unload = env.mass.create_task(env.mass.unload_provider(env.a.instance_id))
        if mode == "send_wait":
            async with asyncio.timeout(5):
                await env.send_started.wait()
            assert not unload.done()
            assert not command.done()
            assert not client._operations
            assert all(response.closed for response, _ in responses)
            # Sending the API error only waits on the message sink, not this provider.
            env.send_release.set()
        async with asyncio.timeout(5):
            await unload
    return unload, external


def assert_terminal(env: Any, message_id: str, mode: str) -> list[Any]:
    """Require an error for provider unload, never a success or a missing reply."""
    terminal = [msg for msg in env.messages if msg.message_id == message_id]
    if mode in {"external", "external_first", "unload_first"}:
        assert terminal == []
    elif mode == "success":
        assert len(terminal) == 1
        assert isinstance(terminal[0], SuccessResultMessage)
        assert not terminal[0].partial
        assert len(terminal[0].result) == 1
    else:
        assert len(terminal) == 1
        assert isinstance(terminal[0], ErrorResultMessage)
        if mode == "provider_error":
            assert terminal[0].error_code == 18
            assert terminal[0].details == "Operation denied"
        else:
            assert terminal[0].error_code == ResourceTemporarilyUnavailable.error_code
            assert terminal[0].details == "FeiNiu client is unloading"
    return terminal


async def assert_other_requests(env: Any, mode: str) -> None:
    """Verify the same command handler, sibling and shared session remain usable."""
    info = await env.dispatch("info", f"{mode}-info")
    await info
    assert isinstance(env.messages[-1], SuccessResultMessage)
    assert env.messages[-1].message_id == f"{mode}-info"
    env.hold_login = False
    assert await env.b._client.current_user() == {"guid": "b"}
    async with env.shared.get(env.server.make_url("/health")) as response:
        assert await response.json() == {"ok": True}
    assert not env.shared.closed


@pytest.mark.parametrize(
    "mode",
    [
        "success",
        "provider_error",
        "unload",
        "external",
        "external_first",
        "unload_first",
        "throttle",
        "cooldown",
        "login_lock",
        "login_body",
        "send_wait",
    ],
)
async def test_api_command_termination(
    api_environment: Any,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    record_property: Callable[[str, object], None],
) -> None:
    """Only this instance's unload becomes an API error; caller cancellation survives."""
    env = api_environment
    client = env.a._client
    entered, lock = await prepare_wait(env, monkeypatch, mode)
    message_id = f"{mode}-album"
    command = await env.dispatch(
        "music/albums/album_tracks",
        message_id,
        item_id="album-test",
        provider_instance_id_or_domain=env.a.instance_id,
    )
    try:
        async with asyncio.timeout(5):
            if mode in {"throttle", "cooldown", "login_lock"}:
                await entered.wait()
            else:
                await env.started.wait()
                await env.headers.wait()
        assert client._operations == {command}
        assert not command.done()
        before_requests = len(env.requests)
        responses = [(response, response.connection) for response in env.responses]
        unload, external = await finish_command(env, monkeypatch, mode, command, responses)
        if mode in {"external", "external_first", "unload_first"}:
            with pytest.raises(asyncio.CancelledError) as cancelled:
                await command
            assert command.cancelled()
            assert command.cancelling() == (2 if mode == "unload_first" else 1)
            if mode != "unload_first":
                assert cancelled.value.args == (external,)
        else:
            async with asyncio.timeout(5):
                await command
            assert not command.cancelled()
            assert command.cancelling() == 0
        assert not client._operations
        assert not client._unload_cancellations
        assert all(response.closed and response.connection is None for response, _ in responses)
        assert all(connection is None or connection.closed for _, connection in responses)
        if unload is not None:
            assert client._token is None
            assert client._session is None
            await env.a.unload()
            assert len(env.requests) == before_requests
        terminal = assert_terminal(env, message_id, mode)
        await assert_other_requests(env, mode)
        record_property(
            "api_result",
            json.dumps(
                {
                    "mode": mode,
                    "task_cancelled": command.cancelled(),
                    "cancelling": command.cancelling(),
                    "message_id": message_id,
                    "terminal": [
                        {"type": type(msg).__name__, "code": getattr(msg, "error_code", None)}
                        for msg in terminal
                    ],
                    "responses_released": True,
                    "operations_empty": True,
                    "same_handler_info": True,
                    "sibling": True,
                    "shared_session": True,
                }
            ),
        )
    finally:
        if lock is not None:
            lock.release()
        env.release.set()
