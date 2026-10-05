"""Cookie and resource isolation through real HTTP, without a NAS or real credentials."""

import asyncio
import socket
import time
import traceback
from collections.abc import AsyncGenerator
from contextlib import aclosing, asynccontextmanager
from copy import copy
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from types import SimpleNamespace
from typing import Any

import aiohttp
import pytest
from aiohttp import web
from aiohttp.abc import AbstractResolver, ResolveResult
from aiohttp.test_utils import TestServer
from music_assistant_models.enums import MediaType
from yarl import URL

from music_assistant.helpers import aiohttp_client, throttle_retry
from music_assistant.helpers.throttle_retry import MAX_RETRY_AFTER
from music_assistant.mass import MusicAssistant
from music_assistant.providers.feiniu_music.client import (
    AuthenticationError,
    NetworkError,
    RateLimitError,
)

from .test_provider import provider as provider  # noqa: PLC0414
from .test_provider import track_data

AUDIO = b"ID3" + bytes(4093)


class LoopbackResolver(AbstractResolver):
    """Resolve a hostname locally so the default jar accepts host cookies."""

    async def resolve(
        self, host: str, port: int = 0, family: int = socket.AF_INET
    ) -> list[ResolveResult]:
        """Resolve only the synthetic music host."""
        assert host == "feiniu.test"
        return [
            ResolveResult(
                hostname=host,
                host="127.0.0.1",
                port=port,
                family=socket.AF_INET,
                proto=0,
                flags=0,
            )
        ]

    async def close(self) -> None:
        """No resolver resources are allocated."""


@pytest.fixture
async def http_environment(  # noqa: PLR0915
    provider: Any, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> AsyncGenerator[Any]:
    """Run real provider initialization against a cookie-setting loopback service."""
    requests: list[tuple[str, dict[str, str]]] = []
    finish_stream = asyncio.Event()
    release_login = asyncio.Event()
    login_started = asyncio.Event()
    api_started = asyncio.Event()
    request_times: list[tuple[str, float]] = []
    rate_limits: dict[str, dict[str, str]] = {}
    set_cookies = getattr(request, "param", True)

    async def handle(request: web.Request) -> web.StreamResponse:
        requests.append((request.path, dict(request.cookies)))
        request_times.append((request.path, time.monotonic()))
        if request.path in rate_limits:
            return web.Response(status=429, headers=rate_limits.pop(request.path))
        if request.path.endswith("/user/password-login"):
            body = await request.json()
            name = body["username"]
            if name == "delayed":
                login_started.set()
                await release_login.wait()
            response = (
                web.json_response(
                    {"code": 0, "data": {"userToken": f"token-{name}", "user": {"guid": name}}}
                )
                if name != "rejected"
                else web.json_response({"code": 120001})
            )
        elif request.path.endswith("/track/metadata"):
            row = {**track_data(), "guid": request.query["guid"]}
            response = web.json_response({"code": 0, "data": {"track": row}})
        elif request.path.endswith("/track/stream"):
            if request.query["guid"] == "held":
                stream = web.StreamResponse(headers={"Content-Type": "audio/mpeg"})
                stream.set_cookie("music-token", "server-cookie")
                await stream.prepare(request)
                await stream.write(AUDIO)
                await finish_stream.wait()
                return stream
            response = web.Response(body=AUDIO, content_type="audio/mpeg")
        elif request.path.endswith("/held-api"):
            stream = web.StreamResponse(headers={"Content-Type": "application/json"})
            await stream.prepare(request)
            await stream.write(b'{"code":0,')
            api_started.set()
            await finish_stream.wait()
            return stream
        elif request.path.endswith("/static/cover"):
            response = web.Response(body=b"\xff\xd8\xff" + bytes(32), content_type="image/jpeg")
        else:
            response = web.json_response({"code": 0, "data": {}})
        if set_cookies:
            response.set_cookie("music-token", "server-cookie")
        return response

    app = web.Application()
    app.router.add_route("*", "/{path:.*}", handle)
    monkeypatch.setattr(aiohttp_client, "_get_resolver", lambda _mass: LoopbackResolver())
    mass: Any = object.__new__(MusicAssistant)
    mass.version, mass._http_session = "test", None
    mass.cache, mass.create_task = provider.mass.cache, provider.mass.create_task
    instances = []
    async with TestServer(app) as server:
        url = f"http://feiniu.test:{server.port}"
        shared = mass.http_session
        shared.cookie_jar.update_cookies(
            {"music-token": "global-cookie", "unrelated": "global-unrelated"}, URL(url)
        )

        def make_provider(name: str) -> Any:
            instance = copy(provider)
            instance.mass = mass
            instance.config = SimpleNamespace(instance_id=f"feiniu-{name}")
            values = {
                "url": url,
                "username": name,
                "password": "synthetic-secret",
                "device_id": "a" * 32,
            }
            instance.get_setup_value = values.get
            instances.append(instance)
            return instance

        try:
            yield SimpleNamespace(
                make_provider=make_provider,
                mass=mass,
                shared=shared,
                url=url,
                requests=requests,
                finish_stream=finish_stream,
                set_cookies=set_cookies,
                release_login=release_login,
                login_started=login_started,
                api_started=api_started,
                request_times=request_times,
                rate_limits=rate_limits,
            )
        finally:
            finish_stream.set()
            release_login.set()
            for instance in instances:
                await instance.unload()
            await shared.close()


@pytest.mark.parametrize("http_environment", [False, True], indirect=True)
async def test_provider_cookie_and_session_isolation(http_environment: Any) -> None:
    """Accounts share MA's session but neutralize foreign cookie values on every request."""
    env = http_environment
    shared = env.shared
    assert isinstance(shared.cookie_jar, aiohttp.CookieJar)
    global_headers = dict(shared.headers)
    global_connector = shared.connector
    first, second = env.make_provider("first"), env.make_provider("second")
    await asyncio.gather(first.handle_async_init(), second.handle_async_init())
    sessions = [first._client._session, second._client._session]
    assert sessions[0] is sessions[1] is shared
    empty_cookies = {"music-token": "", "unrelated": ""}
    assert env.requests == [("/music/api/v1/user/password-login", empty_cookies)] * 2

    before = shared.cookie_jar.filter_cookies(URL(env.url))
    assert first._client._request_cookies(env.url) == {
        "music-token": "token-first",
        "unrelated": "",
    }
    assert shared.cookie_jar.filter_cookies(URL(env.url)) == before
    await asyncio.gather(first._client.current_user(), second._client.current_user())
    assert {cookies["music-token"] for _, cookies in env.requests[-2:]} == {
        "token-first",
        "token-second",
    }
    assert all(cookies["unrelated"] == "" for _, cookies in env.requests[-2:])

    for instance, session, name in zip((first, second), sessions, ("first", "second"), strict=True):
        expected = {"music-token": f"token-{name}", "unrelated": ""}
        await instance._client.current_user()
        assert env.requests[-1] == ("/music/api/v1/user/me", expected)
        details = await instance.get_stream_details("track-test", MediaType.TRACK)
        assert b"".join([chunk async for chunk in instance.get_audio_stream(details)]) == AUDIO
        assert env.requests[-1] == ("/music/api/v1/track/stream", expected)
        await instance._client._request("GET", "/sys/config", authenticated=False)
        assert env.requests[-1][1] == empty_cookies
        instance._client._token = None
        await instance._client.system_config()
        assert env.requests[-1][1] == empty_cookies
        request_count = len(env.requests)
        with pytest.raises(AuthenticationError):
            await anext(instance._client.audio_stream("track-test"))
        assert len(env.requests) == request_count
        await instance._login()
        assert env.requests[-1][1] == empty_cookies
        assert instance._client._session is session

    assert dict(shared.headers) == global_headers
    assert shared.connector is global_connector
    jar_token = "server-cookie" if env.set_cookies else "global-cookie"
    assert shared.cookie_jar.filter_cookies(URL(env.url))["music-token"].value == jar_token
    assert shared.cookie_jar.filter_cookies(URL(env.url))["unrelated"].value == "global-unrelated"
    assert not global_connector.closed


async def test_unload_preserves_shared_session(http_environment: Any) -> None:
    """Unloading one account leaves both MA's transport and the other account usable."""
    env = http_environment
    first, second = env.make_provider("first"), env.make_provider("second")
    await asyncio.gather(first.handle_async_init(), second.handle_async_init())
    shared = env.shared
    connector = shared.connector
    assert connector is not None
    await first.unload()
    await first.unload()
    assert not shared.closed
    assert not connector.closed
    assert first._client._session is None
    assert first._client._token is None
    await second._client.current_user()
    assert env.requests[-1][1] == {"music-token": "token-second", "unrelated": ""}
    assert second._client._session is shared
    async with shared.get(env.url + "/global") as response:
        await response.read()
    assert env.requests[-1][1] == {"music-token": "server-cookie", "unrelated": "global-unrelated"}
    assert not shared.closed
    assert not connector.closed


async def test_http_initialization_failure_preserves_shared_session(http_environment: Any) -> None:
    """A rejected login keeps its traceback without closing another account's transport."""
    env = http_environment
    other = env.make_provider("other")
    await other.handle_async_init()
    session, connector = env.shared, env.shared.connector
    failed = env.make_provider("rejected")
    with pytest.raises(AuthenticationError) as raised:
        await failed.handle_async_init()
    assert "_json" in {frame.name for frame in traceback.extract_tb(raised.value.__traceback__)}
    assert not session.closed
    assert connector is not None
    assert not connector.closed
    assert failed._client._session is None
    assert failed._client._token is None
    assert failed._client._closed
    assert not failed._client._operations
    assert not failed._client._responses
    await failed.unload()
    await failed.unload()
    await other._client.current_user()
    assert env.requests[-1][1] == {"music-token": "token-other", "unrelated": ""}
    assert not other._client._session.closed
    assert not env.shared.closed


@pytest.mark.parametrize("ending", ["complete", "close", "cancel"])
async def test_provider_http_stream_releases_connection(http_environment: Any, ending: str) -> None:
    """Completion, outer-generator close and cancellation release the real HTTP connection."""
    env = http_environment
    instance = env.make_provider("stream")
    await instance.handle_async_init()
    item_id = "track-test" if ending == "complete" else "held"
    details = await instance.get_stream_details(item_id, MediaType.TRACK)
    stream = instance.get_audio_stream(details)
    connector = instance._client._session.connector
    task = None
    try:
        assert await anext(stream) == AUDIO
        if ending == "complete":
            with pytest.raises(StopAsyncIteration):
                await anext(stream)
        else:
            assert len(connector._acquired) == 1
            if ending == "close":
                await stream.aclose()
            else:
                reading = asyncio.Event()

                async def read_next() -> bytes:
                    reading.set()
                    return await anext(stream)

                task = asyncio.create_task(read_next())
                await reading.wait()
                assert not task.done()
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
        assert not connector._acquired
        assert not instance._client._session.closed
    finally:
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await stream.aclose()
        env.finish_stream.set()


async def test_unload_releases_stream_without_consumer_close(http_environment: Any) -> None:
    """Unload closes A's held response without cancelling the consumer or B's stream."""
    env = http_environment
    first, second = env.make_provider("first"), env.make_provider("second")
    await asyncio.gather(first.handle_async_init(), second.handle_async_init())
    first_details = await first.get_stream_details("held", MediaType.TRACK)
    second_details = await second.get_stream_details("held", MediaType.TRACK)
    first_stream = first.get_audio_stream(first_details)
    second_stream = second.get_audio_stream(second_details)
    connector = env.shared.connector
    try:
        assert await anext(first_stream) == AUDIO
        assert await anext(second_stream) == AUDIO
        response = next(iter(first._client._responses))
        sibling_response = next(iter(second._client._responses))
        assert len(connector._acquired) == 2
        await first.unload()
        await first.unload()
        assert response.closed
        assert not sibling_response.closed
        assert len(connector._acquired) == 1
        assert not first._client._responses
        assert not first._client._operations
        assert first._client._token is None
        assert not env.shared.closed
        await second._client.current_user()
        async with env.shared.get(env.url + "/global") as general:
            assert general.status == 200
            await general.read()
        # Resource assertions above precede any consumer-driven generator cleanup.
    finally:
        await first_stream.aclose()
        await second_stream.aclose()


@pytest.mark.parametrize("wait", ["throttle", "cooldown"])
@pytest.mark.parametrize("path", ["api", "audio"])
async def test_unload_cancels_waiters_without_late_requests(
    http_environment: Any, monkeypatch: pytest.MonkeyPatch, wait: str, path: str
) -> None:
    """Pending admission is cancelled promptly and cannot use a cleared session later."""
    env = http_environment
    instance = env.make_provider("waiting")
    await instance.handle_async_init()
    client = instance._client
    manager = client._throttler
    entered = asyncio.Event()
    acquire = manager.acquire

    @asynccontextmanager
    async def observed_acquire() -> AsyncGenerator[float]:
        entered.set()
        async with acquire() as delay:
            yield delay

    monkeypatch.setattr(manager, "acquire", observed_acquire)
    if wait == "cooldown":
        # Stay below MA's fail-fast limit; older helpers have no MAX_WAIT_TIME.
        manager.set_cooldown(min(getattr(throttle_retry, "MAX_WAIT_TIME", 10) / 2, 5))
    else:
        manager.throttler.period = 3600
    stream = client.audio_stream("held")
    task = asyncio.create_task(client.current_user() if path == "api" else anext(stream))
    await entered.wait()
    count = len(env.requests)
    assert not task.done()
    async with asyncio.timeout(1):
        await instance.unload()
    with pytest.raises(NetworkError, match="unloading"):
        await task
    assert task.cancelling() == 0
    assert len(env.requests) == count
    assert not client._operations
    assert not env.shared.closed
    with pytest.raises(NetworkError, match="closed"):
        await client.current_user()
    assert len(env.requests) == count
    await stream.aclose()


async def test_unload_cancels_delayed_and_queued_login(http_environment: Any) -> None:
    """Neither a delayed response nor a same-server login waiter can restore A's token."""
    env = http_environment
    instance = env.make_provider("delayed")
    initializing = asyncio.create_task(instance.handle_async_init())
    await env.login_started.wait()
    client = instance._client
    queued_started = asyncio.Event()

    async def queued_login() -> Any:
        queued_started.set()
        return await client.login("delayed", "synthetic-secret", "b" * 32)

    queued = asyncio.create_task(queued_login())
    await queued_started.wait()
    assert not queued.done()
    async with asyncio.timeout(1):
        await instance.unload()
    env.release_login.set()
    for task in (initializing, queued):
        with pytest.raises(NetworkError, match="unloading"):
            await task
        assert task.cancelling() == 0
    assert len(env.requests) == 1
    assert client._token is None
    assert client._session is None
    assert client._closed
    assert not client._operations
    assert not env.shared.connector._acquired
    assert not env.shared.closed
    await instance.unload()


@pytest.mark.parametrize("ending", ["unload", "external_first", "unload_first", "unknown"])
async def test_nested_operation_cancellation_ownership(  # noqa: PLR0915
    http_environment: Any, monkeypatch: pytest.MonkeyPatch, ending: str
) -> None:
    """Nested scopes defer ownership to the outer exit and never consume external cancels."""
    env = http_environment
    instance = env.make_provider("nested")
    await instance.handle_async_init()
    client = instance._client
    entered, interrupted, release, gathering = (asyncio.Event() for _ in range(4))
    external = object()
    outer_markers = []

    async def operation() -> None:
        async with client._operation():
            try:
                async with client._operation():
                    entered.set()
                    try:
                        await asyncio.Event().wait()
                    except asyncio.CancelledError as err:
                        interrupted.set()
                        if ending == "unknown":
                            raise asyncio.CancelledError(external) from err
                        await release.wait()
                        raise
            finally:
                outer_markers.append(asyncio.current_task() in client._unload_cancellations)

    task = asyncio.create_task(operation())
    await entered.wait()
    gather = asyncio.gather

    def observe_gather(*aws: Any, **kwargs: Any) -> Any:
        if any(aw is task for aw in aws):
            gathering.set()
        return gather(*aws, **kwargs)

    monkeypatch.setattr(asyncio, "gather", observe_gather)
    unloading = None
    try:
        if ending == "external_first":
            task.cancel(external)
            await interrupted.wait()
        unloading = asyncio.create_task(instance.unload())
        await gathering.wait()
        if ending == "unload_first":
            await interrupted.wait()
            assert task in client._unload_cancellations
            task.cancel(external)
        release.set()
        async with asyncio.timeout(1):
            await unloading
        if ending == "unload":
            with pytest.raises(NetworkError, match="unloading") as failure:
                await task
            assert failure.value.backoff_time == 30
            assert isinstance(failure.value.__cause__, asyncio.CancelledError)
            assert task.cancelling() == 0
        else:
            with pytest.raises(asyncio.CancelledError) as cancelled:
                await task
            assert cancelled.value.args == (external,)
            assert task.cancelling() == (2 if ending == "unload_first" else 1)
        assert outer_markers == [ending != "external_first"]
        assert not client._unload_cancellations
        assert not client._operations
        await instance.unload()
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await gather(task, *([unloading] if unloading else []), return_exceptions=True)


async def test_operation_preserves_parent_timeout(http_environment: Any) -> None:
    """A caller's timeout remains a timeout and can consume its own cancellation."""
    instance = http_environment.make_provider("timeout")
    await instance.handle_async_init()
    entered = asyncio.Event()
    timeout = asyncio.timeout(None)

    async def operation() -> None:
        async with timeout, instance._client._operation():
            entered.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(operation())
    await entered.wait()
    timeout.reschedule(asyncio.get_running_loop().time())
    with pytest.raises(TimeoutError):
        await task
    assert task.cancelling() == 0
    assert not instance._client._closed
    assert not instance._client._operations
    assert not instance._client._unload_cancellations


async def test_unload_from_current_operation_does_not_cancel_itself(http_environment: Any) -> None:
    """The task performing unload is excluded even when it owns an outer operation."""
    instance = http_environment.make_provider("self-unload")
    await instance.handle_async_init()
    task = asyncio.current_task()
    assert task is not None
    cancellations = task.cancelling()
    async with instance._client._operation():
        await instance.unload()
        await instance.unload()
    assert task.cancelling() == cancellations
    assert not instance._client._operations
    assert not instance._client._unload_cancellations


async def read_audio(client: Any) -> bytes:
    """Consume one synthetic prefix and close the generator deterministically."""
    async with aclosing(client.audio_stream("track-test")) as stream:
        return await anext(stream)


async def test_unload_cancels_pending_api_body(http_environment: Any) -> None:
    """Unload cancels an actual HTTP body read and releases its acquired connection."""
    env = http_environment
    instance = env.make_provider("pending")
    await instance.handle_async_init()
    task = asyncio.create_task(instance._client._request("GET", "/held-api"))
    await env.api_started.wait()
    assert len(env.shared.connector._acquired) == 1
    async with asyncio.timeout(1):
        await instance.unload()
    with pytest.raises(NetworkError, match="unloading"):
        await task
    assert task.cancelling() == 0
    assert not instance._client._operations
    assert not env.shared.connector._acquired
    assert not env.shared.closed


async def test_unload_interrupts_pending_audio_read(http_environment: Any) -> None:
    """Closing A's response wakes a blocked decoder without restarting or relogging in."""
    env = http_environment
    instance = env.make_provider("pending-audio")
    await instance.handle_async_init()
    details = await instance.get_stream_details("held", MediaType.TRACK)
    async with aclosing(instance.get_audio_stream(details)) as stream:
        assert await anext(stream) == AUDIO
        reading = asyncio.Event()

        async def read_next() -> bytes:
            reading.set()
            return await anext(stream)

        task = asyncio.create_task(read_next())
        await reading.wait()
        assert not task.done()
        count = len(env.requests)
        await instance.unload()
        with pytest.raises(NetworkError):
            async with asyncio.timeout(1):
                await task
        assert len(env.requests) == count
        assert not env.shared.connector._acquired
        assert not env.shared.closed


@pytest.mark.parametrize("limited_path", ["/user/me", "/static/cover", "/track/stream"])
async def test_rate_limit_gates_all_request_paths(http_environment: Any, limited_path: str) -> None:
    """One 429 stops queued JSON, artwork and audio calls without retrying the failed call."""
    env = http_environment
    instance, sibling = env.make_provider("limited"), env.make_provider("sibling")
    await asyncio.gather(instance.handle_async_init(), sibling.handle_async_init())
    client = instance._client
    calls = {
        "/user/me": client.current_user,
        "/static/cover": lambda: client.cover("synthetic-cover"),
        "/track/stream": lambda: read_audio(client),
    }
    endpoint = "/music/api/v1" + limited_path
    env.rate_limits[endpoint] = {"Retry-After": "1"}
    # These calls queue before the rate-limit response is processed.
    tasks = [asyncio.create_task(calls[limited_path]())]
    tasks.extend(asyncio.create_task(call()) for call in calls.values())
    try:
        with pytest.raises(RateLimitError) as raised:
            await tasks[0]
        assert raised.value.backoff_time == 1
        limited_at = next(when for path, when in env.request_times if path == endpoint)
        start_index = len(env.request_times)
        assert all(not task.done() for task in tasks[1:])
        await asyncio.gather(*tasks[1:])
        sent = env.request_times[start_index:]
        assert len(sent) == 3
        assert {path for path, _ in sent} == {"/music/api/v1" + path for path in calls}
        assert all(when - limited_at >= 1 for _, when in sent)
        # Exactly one failing operation, followed by the three explicitly issued calls.
        assert len(env.requests) == 6  # Two logins plus four requests.
        assert not client._operations
        assert not client._responses
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.parametrize("header", [None, "invalid", "date", "999999"])
async def test_retry_after_and_instance_isolation(
    http_environment: Any, header: str | None
) -> None:
    """Date/fallback/cap feed the same gate and backoff; another account stays usable."""
    env = http_environment
    first, second = env.make_provider("limited"), env.make_provider("sibling")
    await asyncio.gather(first.handle_async_init(), second.handle_async_init())
    if header == "date":
        header = format_datetime(datetime.now(UTC) + timedelta(seconds=120), usegmt=True)
    env.rate_limits["/music/api/v1/user/me"] = {"Retry-After": header} if header else {}
    with pytest.raises(RateLimitError) as raised:
        await first._client.current_user()
    delay = raised.value.backoff_time
    if header and header.endswith("GMT"):
        assert 118 <= delay <= 120
    else:
        assert delay == (MAX_RETRY_AFTER if header == "999999" else 60)
    remaining = first._client._throttler._cooldown_until - time.monotonic()
    assert delay - 1 <= remaining <= delay
    async with asyncio.timeout(2):
        await second._client.current_user()
        async with env.shared.get(env.url + "/global") as response:
            assert response.status == 200
            await response.read()
    assert first._client._throttler._cooldown_until > time.monotonic()
    assert second._client._throttler._cooldown_until == 0
