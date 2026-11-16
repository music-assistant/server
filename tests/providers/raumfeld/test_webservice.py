"""Tests for the commands to the Teufel Raumfeld host's web service."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from music_assistant.providers.raumfeld import webservice

_SESSION_PATH = "/497a8e9a-8116-4d9a-b253-fda16904bbc1"
_ROOM = "uuid:67b9ba87-63cf-4d2f-8920-38c8bc0ec106"
_OTHER_ROOM = "uuid:97aa3953-11f8-4f65-86a1-613d60e27160"
_ZONE = "uuid:aa3ff5a8-2eb4-4a52-a4b7-8e4e8bbd5b10"


class _Host:
    """A web service that answers like the host: a redirect to a session path first."""

    def __init__(self, status: int = 200) -> None:
        """Answer every command with the given status."""
        self.status = status
        self.requests: list[tuple[str, dict[str, str]]] = []

    async def handle(self, request: web.Request) -> web.StreamResponse:
        """Redirect a request outside the session path, record and answer one inside it."""
        if not request.path.startswith(_SESSION_PATH):
            raise web.HTTPTemporaryRedirect(f"{_SESSION_PATH}{request.path_qs}")
        self.requests.append((request.path.removeprefix(f"{_SESSION_PATH}/"), dict(request.query)))
        return web.Response(status=self.status)


@pytest.fixture
async def serve() -> AsyncIterator[Callable[[_Host], Awaitable[str]]]:
    """Return a function that serves a host and gives its base URL."""
    servers: list[TestServer] = []

    async def start(host: _Host) -> str:
        app = web.Application()
        app.router.add_route("GET", "/{tail:.*}", host.handle)
        server = TestServer(app)
        await server.start_server()
        servers.append(server)
        return str(server.make_url("")).rstrip("/")

    yield start
    for server in servers:
        await server.close()


async def test_commands_reach_the_host_past_its_redirect(
    serve: Callable[[_Host], Awaitable[str]],
) -> None:
    """Each command is sent with the room and zone the host expects, after its redirect."""
    host = _Host()
    url = await serve(host)
    async with aiohttp.ClientSession() as session:
        await webservice.connect_rooms_to_zone(session, url, [_ROOM, _OTHER_ROOM])
        await webservice.connect_room_to_zone(session, url, _ROOM, _ZONE)
        await webservice.drop_room(session, url, _ROOM)
        await webservice.enter_manual_standby(session, url, _ROOM)
        await webservice.leave_standby(session, url, _ROOM)
    assert host.requests == [
        ("connectRoomsToZone", {"roomUDNs": f"{_ROOM},{_OTHER_ROOM}"}),
        ("connectRoomToZone", {"roomUDN": _ROOM, "zoneUDN": _ZONE}),
        ("dropRoomJob", {"roomUDN": _ROOM}),
        ("enterManualStandby", {"roomUDN": _ROOM}),
        ("leaveStandby", {"roomUDN": _ROOM}),
    ]


async def test_a_command_the_host_refuses_fails(serve: Callable[[_Host], Awaitable[str]]) -> None:
    """An error status is raised, so a command the host did not take is not reported as done."""
    url = await serve(_Host(status=500))
    async with aiohttp.ClientSession() as session:
        with pytest.raises(aiohttp.ClientResponseError):
            await webservice.leave_standby(session, url, _ROOM)


async def test_a_zone_without_rooms_is_never_asked_for(
    serve: Callable[[_Host], Awaitable[str]],
) -> None:
    """Without rooms the host would put every room in the zone, so nothing is sent."""
    host = _Host()
    url = await serve(host)
    async with aiohttp.ClientSession() as session:
        with pytest.raises(ValueError, match="at least one room"):
            await webservice.connect_rooms_to_zone(session, url, [])
    assert host.requests == []
