"""Fixtures for the Alexa Devices provider tests."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import aiohttp
import pytest
from aioamazondevices.structures import AmazonDevice
from aiohttp import web

if TYPE_CHECKING:
    from aiohttp.test_utils import TestServer

SERIAL = "G0911234567890AB"


def amazon_device(
    serial_number: str = SERIAL,
    *,
    capabilities: tuple[str, ...] = ("AUDIO_PLAYER", "MUSIC_SKILL"),
    voice_control_supported: bool = True,
    online: bool = True,
) -> AmazonDevice:
    """Return an aioamazondevices device as the library builds it."""
    return AmazonDevice(
        account_name=f"Echo {serial_number[-2:]}",
        capabilities=list(capabilities),
        device_family="ECHO",
        device_type="A3S5BH2HU6VAYF",
        device_owner_customer_id="CUSTOMER",
        household_device=True,
        device_cluster_members={serial_number: None},
        parent_clusters=[],
        online=online,
        serial_number=serial_number,
        manufacturer="Amazon",
        model="Echo Dot",
        software_version="123456",
        hardware_version="Gen3",
        entity_id=None,
        endpoint_id=None,
        sensors={},
        notifications_supported=False,
        notifications={},
        media_player_supported=True,
        communication_settings={},
        voice_control_supported=voice_control_supported,
    )


@dataclass
class FakeSkillApi:
    """A companion skill API answering with canned responses and recording requests."""

    url: str
    session: aiohttp.ClientSession
    push_answer: str = '{"status": "ok"}'
    control_answer: str = '{"status": "ok", "pageLive": true}'
    control_status: int = 200
    intents: dict[str, Any] = field(default_factory=dict)
    requests: list[tuple[str, dict[str, Any] | None]] = field(default_factory=list)

    @property
    def paths(self) -> list[str]:
        """Return the requested paths, in order."""
        return [path for path, _ in self.requests]


@pytest.fixture
async def skill_api(
    aiohttp_server: Callable[[web.Application], Awaitable[TestServer]],
) -> AsyncIterator[FakeSkillApi]:
    """Run a fake companion skill API."""
    app = web.Application()
    state: dict[str, FakeSkillApi] = {}

    async def push_url(request: web.Request) -> web.Response:
        state["api"].requests.append((request.path, await request.json()))
        return web.Response(text=state["api"].push_answer)

    async def control(request: web.Request) -> web.Response:
        state["api"].requests.append((request.path, await request.json()))
        return web.Response(text=state["api"].control_answer, status=state["api"].control_status)

    async def intents(request: web.Request) -> web.Response:
        state["api"].requests.append((request.path, None))
        return web.json_response(state["api"].intents)

    app.router.add_post("/ma/push-url", push_url)
    app.router.add_post("/ma/control", control)
    app.router.add_get("/alexa/intents", intents)
    server = await aiohttp_server(app)
    async with aiohttp.ClientSession() as session:
        state["api"] = FakeSkillApi(url=str(server.make_url("/")), session=session)
        yield state["api"]
