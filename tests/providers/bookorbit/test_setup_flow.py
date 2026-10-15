"""Tests for the BookOrbit setup flow."""

from __future__ import annotations

import asyncio
import time
from typing import Any
from unittest import mock

import aiohttp
import pytest
from aiohttp import web
from music_assistant_models.enums import FlowStepType

from music_assistant.models.setup_flow import SetupFlowContext, SetupFlowError, SetupSession
from music_assistant.providers.bookorbit.constants import (
    CONF_PASSWORD,
    CONF_URL,
    CONF_USERNAME,
    CONF_VERIFY_SSL,
    DOMAIN,
)
from music_assistant.providers.bookorbit.setup_flow import _validate_connection, run_setup


def _patch_create_clientsession() -> Any:
    """Return a patch that builds a plain ClientSession (tests do not need MA connectors)."""

    def _factory(_mass: Any, **kwargs: Any) -> aiohttp.ClientSession:
        return aiohttp.ClientSession(
            cookie_jar=kwargs.get("cookie_jar") or aiohttp.DummyCookieJar(),
            timeout=kwargs.get("timeout"),
        )

    return mock.patch(
        "music_assistant.providers.bookorbit.setup_flow.create_clientsession",
        side_effect=_factory,
    )


def _make_session(
    finish_handler: Any,
    mass: Any,
    *,
    setup_data: dict[str, Any] | None = None,
) -> SetupSession:
    context = SetupFlowContext(
        kind="reconfigure" if setup_data else "setup",
        reason="user",
        domain=DOMAIN,
        setup_data=setup_data or {},
    )
    return SetupSession(mass, "flow-test", context, finish_handler)


async def _wait_for(predicate: Any, timeout: float = 5.0) -> Any:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if result := predicate():
            return result
        await asyncio.sleep(0.01)
    raise AssertionError("condition not met within timeout")


async def _wait_for_form(session: SetupSession, *, with_errors: bool = False) -> Any:
    return await _wait_for(
        lambda: (
            session.current_step
            if session.current_step
            and session.current_step.type == FlowStepType.FORM
            and (session.current_step.errors if with_errors else True)
            else None
        )
    )


@pytest.mark.asyncio
async def test_validate_connection_login_and_libraries(aiohttp_server: Any) -> None:
    """Validation performs login and lists libraries."""

    async def login(request: web.Request) -> web.Response:
        assert (await request.json()) == {"username": "u", "password": "p"}
        resp = web.json_response({"accessToken": "tok"})
        resp.set_cookie("refresh_token", "ref", path="/")
        return resp

    async def libraries(request: web.Request) -> web.Response:
        assert request.headers.get("Authorization") == "Bearer tok"
        return web.json_response([{"id": 1, "name": "Audiobooks"}])

    app = web.Application()
    app.router.add_post("/api/v1/auth/login", login)
    app.router.add_get("/api/v1/libraries", libraries)
    server = await aiohttp_server(app)
    base = str(server.make_url("/")).rstrip("/")
    mass = mock.Mock()
    session = _make_session(lambda *_a, **_k: {"instance_id": "x"}, mass)

    with _patch_create_clientsession():
        await _validate_connection(
            session,
            {
                CONF_URL: base,
                CONF_USERNAME: "u",
                CONF_PASSWORD: "p",
                CONF_VERIFY_SSL: True,
            },
        )


@pytest.mark.asyncio
async def test_run_setup_finishes_after_successful_validation(aiohttp_server: Any) -> None:
    """A successful login lets the flow finish and persist setup data."""
    collected: dict[str, Any] = {}

    async def login(_request: web.Request) -> web.Response:
        resp = web.json_response({"accessToken": "tok"})
        resp.set_cookie("refresh_token", "ref", path="/")
        return resp

    async def libraries(_request: web.Request) -> web.Response:
        return web.json_response([])

    app = web.Application()
    app.router.add_post("/api/v1/auth/login", login)
    app.router.add_get("/api/v1/libraries", libraries)
    server = await aiohttp_server(app)
    base = str(server.make_url("/")).rstrip("/")

    async def finish(_session: SetupSession, values: dict[str, Any]) -> dict[str, str]:
        collected.update(values)
        return {"instance_id": "bookorbit--test"}

    mass = mock.Mock()
    async with aiohttp.ClientSession() as http:
        mass.http_session = http
        session = _make_session(finish, mass)
        with _patch_create_clientsession():
            task = asyncio.create_task(run_setup(session))
            await _wait_for_form(session)
            session.handle_submit(
                {
                    CONF_URL: base,
                    CONF_USERNAME: "u",
                    CONF_PASSWORD: "p",
                    CONF_VERIFY_SSL: True,
                }
            )
            await _wait_for(lambda: session.finished)
            await task

        assert collected[CONF_USERNAME] == "u"
    assert collected[CONF_PASSWORD] == "p"


@pytest.mark.asyncio
async def test_run_setup_shows_login_error_and_retries(aiohttp_server: Any) -> None:
    """Failed login re-shows the form with a localized error."""
    attempts = {"login": 0}

    async def login(_request: web.Request) -> web.Response:
        attempts["login"] += 1
        if attempts["login"] == 1:
            return web.Response(status=401)
        resp = web.json_response({"accessToken": "tok"})
        resp.set_cookie("refresh_token", "ref", path="/")
        return resp

    async def libraries(_request: web.Request) -> web.Response:
        return web.json_response([])

    app = web.Application()
    app.router.add_post("/api/v1/auth/login", login)
    app.router.add_get("/api/v1/libraries", libraries)
    server = await aiohttp_server(app)
    base = str(server.make_url("/")).rstrip("/")

    async def finish(_session: SetupSession, _values: dict[str, Any]) -> dict[str, str]:
        return {"instance_id": "bookorbit--test"}

    mass = mock.Mock()
    async with aiohttp.ClientSession() as http:
        mass.http_session = http
        session = _make_session(finish, mass)
        with _patch_create_clientsession():
            task = asyncio.create_task(run_setup(session))
            await _wait_for_form(session)
            session.handle_submit(
                {CONF_URL: base, CONF_USERNAME: "u", CONF_PASSWORD: "wrong", CONF_VERIFY_SSL: True}
            )
            error_form = await _wait_for_form(session, with_errors=True)
            assert error_form.error_translations["base"].key == "login_failed"
            session.handle_submit(
                {CONF_URL: base, CONF_USERNAME: "u", CONF_PASSWORD: "right", CONF_VERIFY_SSL: True}
            )
            await _wait_for(lambda: session.finished)
            await task

        assert attempts["login"] == 2


@pytest.mark.asyncio
async def test_validate_connection_raises_setup_flow_error_on_unreachable_host() -> None:
    """Unreachable hosts surface as connection_failed."""
    mass = mock.Mock()
    session = _make_session(lambda *_a, **_k: {"instance_id": "x"}, mass)
    with _patch_create_clientsession(), pytest.raises(SetupFlowError) as exc:
        await _validate_connection(
            session,
            {
                CONF_URL: "http://127.0.0.1:1",
                CONF_USERNAME: "u",
                CONF_PASSWORD: "p",
                CONF_VERIFY_SSL: True,
            },
        )
    assert exc.value.translation_key == "connection_failed"


@pytest.mark.asyncio
async def test_validate_connection_raises_setup_flow_error_on_empty_password() -> None:
    """First setup without a password surfaces password_required, not KeyError."""
    mass = mock.Mock()
    session = _make_session(lambda *_a, **_k: {"instance_id": "x"}, mass)
    with _patch_create_clientsession(), pytest.raises(SetupFlowError) as exc:
        await _validate_connection(
            session,
            {
                CONF_URL: "http://127.0.0.1:9",
                CONF_USERNAME: "u",
                CONF_VERIFY_SSL: True,
            },
        )
    assert exc.value.translation_key == "password_required"


@pytest.mark.asyncio
async def test_run_setup_reconfigure_blank_password_keeps_stored_and_validates(
    aiohttp_server: Any,
) -> None:
    """Reconfigure with unchanged username and blank password reuses the saved password."""
    collected: dict[str, Any] = {}

    async def login(request: web.Request) -> web.Response:
        assert (await request.json()) == {"username": "u", "password": "stored-secret"}
        resp = web.json_response({"accessToken": "tok"})
        resp.set_cookie("refresh_token", "ref", path="/")
        return resp

    async def libraries(_request: web.Request) -> web.Response:
        return web.json_response([])

    app = web.Application()
    app.router.add_post("/api/v1/auth/login", login)
    app.router.add_get("/api/v1/libraries", libraries)
    server = await aiohttp_server(app)
    base = str(server.make_url("/")).rstrip("/")

    async def finish(_session: SetupSession, values: dict[str, Any]) -> dict[str, str]:
        collected.update(values)
        return {"instance_id": "bookorbit--test"}

    mass = mock.Mock()
    async with aiohttp.ClientSession() as http:
        mass.http_session = http
        session = _make_session(
            finish,
            mass,
            setup_data={
                CONF_URL: base,
                CONF_USERNAME: "u",
                CONF_PASSWORD: "stored-secret",
                CONF_VERIFY_SSL: True,
            },
        )
        with _patch_create_clientsession():
            task = asyncio.create_task(run_setup(session))
            await _wait_for_form(session)
            session.handle_submit(
                {
                    CONF_URL: base,
                    CONF_USERNAME: "u",
                    CONF_PASSWORD: None,
                    CONF_VERIFY_SSL: True,
                }
            )
            await _wait_for(lambda: session.finished)
            await task

        assert collected[CONF_PASSWORD] == "stored-secret"
