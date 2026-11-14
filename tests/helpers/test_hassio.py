"""Tests for the Supervisor API helpers."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any

import pytest
from aiohttp import ClientSession, web
from aiohttp.test_utils import TestServer

from music_assistant.helpers import hassio
from music_assistant.helpers.hassio import SupervisorError, supervisor_request, supervisor_token
from music_assistant.mass import MusicAssistant

TOKEN = "app-token"


@pytest.fixture
async def supervisor(
    mass_minimal: MusicAssistant, monkeypatch: pytest.MonkeyPatch
) -> AsyncGenerator[list[dict[str, Any]]]:
    """
    Run a fake Supervisor and point the helpers at it; yields the requests it received.

    :param mass_minimal: The minimal server whose session the helpers use.
    :param monkeypatch: Pytest monkeypatch fixture.
    """
    received: list[dict[str, Any]] = []

    async def mounts(request: web.Request) -> web.Response:
        received.append(
            {
                "method": request.method,
                "authorization": request.headers.get("Authorization"),
                "body": await request.json() if request.can_read_body else None,
            }
        )
        if request.method == "POST":
            return web.json_response(
                {
                    "result": "error",
                    "message": "Mount nas is not reachable. Check the Supervisor logs for details",
                    "error_key": "mount_activation_error",
                },
                status=400,
            )
        return web.json_response({"result": "ok", "data": {"mounts": []}})

    async def forbidden(_request: web.Request) -> web.Response:
        # the security middleware answers in plain text before any handler runs
        raise web.HTTPForbidden

    app = web.Application()
    app.router.add_route("*", "/mounts", mounts)
    app.router.add_get("/forbidden", forbidden)
    server = TestServer(app)
    await server.start_server()
    session = ClientSession()
    mass_minimal._http_session_no_ssl = session
    monkeypatch.setattr(hassio, "SUPERVISOR_URL", str(server.make_url("")).rstrip("/"))
    monkeypatch.setenv("SUPERVISOR_TOKEN", TOKEN)
    try:
        yield received
    finally:
        await session.close()
        await server.close()


async def test_request_returns_the_data(
    mass_minimal: MusicAssistant, supervisor: list[dict[str, Any]]
) -> None:
    """A successful request returns the data of the answer, and sends the app's token."""
    assert await supervisor_request(mass_minimal, "get", "/mounts") == {"mounts": []}
    assert supervisor[0]["authorization"] == f"Bearer {TOKEN}"


@pytest.mark.usefixtures("supervisor")
async def test_error_carries_the_message_and_status(mass_minimal: MusicAssistant) -> None:
    """An error answer raises with the Supervisor's own message and the HTTP status."""
    with pytest.raises(SupervisorError) as exc_info:
        await supervisor_request(mass_minimal, "post", "/mounts", json_data={"name": "nas"})

    assert exc_info.value.status == 400
    assert exc_info.value.message == (
        "Mount nas is not reachable. Check the Supervisor logs for details"
    )


@pytest.mark.usefixtures("supervisor")
async def test_plain_text_error(mass_minimal: MusicAssistant) -> None:
    """A refusal before any handler ran is plain text, and still carries its status."""
    with pytest.raises(SupervisorError) as exc_info:
        await supervisor_request(mass_minimal, "get", "/forbidden")

    assert exc_info.value.status == 403
    assert "Forbidden" in exc_info.value.message


@pytest.mark.usefixtures("supervisor")
async def test_unreachable_supervisor(
    mass_minimal: MusicAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Supervisor that can not be reached raises without a status."""
    monkeypatch.setattr(hassio, "SUPERVISOR_URL", "http://127.0.0.1:1")

    with pytest.raises(SupervisorError) as exc_info:
        await supervisor_request(mass_minimal, "get", "/mounts")

    assert exc_info.value.status is None


async def test_no_supervisor(mass_minimal: MusicAssistant, monkeypatch: pytest.MonkeyPatch) -> None:
    """Without a token nothing is sent."""
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)

    assert supervisor_token() is None
    with pytest.raises(SupervisorError):
        await supervisor_request(mass_minimal, "get", "/mounts")
