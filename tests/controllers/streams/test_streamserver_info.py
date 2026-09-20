"""Tests for the streamserver's published-address report and its reachability probe."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import aiohttp
from music_assistant_models.auth import Scope

from music_assistant.constants import CONF_BIND_IP, CONF_BIND_PORT, CONF_PUBLISH_IP, CONF_VALUE_AUTO
from music_assistant.controllers.streams.controller import StreamsController, StreamServerInfo

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant

LOOPBACK_IP = "127.0.0.1"
# a publish IP does not have to exist on this host at all (NAT/port forward setups)
EXTERNAL_PUBLISH_IP = "203.0.113.9"
# a page on another device of the local network, which is where a probe comes from
PROBE_ORIGIN = "http://192.168.1.20:8095"


async def _setup_streams(
    controller: StreamsController, mass: MusicAssistant, publish_ip: str = CONF_VALUE_AUTO
) -> int:
    """
    Run the streamserver's setup bound to loopback on an OS-assigned port, and return that port.

    :param controller: The StreamsController to set up.
    :param mass: The MusicAssistant instance owning the controller.
    :param publish_ip: Address to configure as publish IP ("auto" to have it resolved).
    """
    config = await mass.config.get_core_config(controller.domain)
    config.update({CONF_BIND_IP: LOOPBACK_IP, CONF_PUBLISH_IP: publish_ip, CONF_BIND_PORT: 0})
    with (
        patch(
            "music_assistant.controllers.streams.controller.get_publish_ip_candidates",
            AsyncMock(return_value=(LOOPBACK_IP,)),
        ),
        patch(
            "music_assistant.controllers.streams.controller.check_ffmpeg_version",
            AsyncMock(),
        ),
    ):
        await controller.setup(config)
    port = controller.publish_port
    assert isinstance(port, int)
    assert port != 0
    return port


async def test_info_reports_the_published_address(
    streams_controller: StreamsController, mass_minimal: MusicAssistant
) -> None:
    """The report carries the URL players are handed, on the port the server actually bound."""
    port = await _setup_streams(streams_controller, mass_minimal, publish_ip=EXTERNAL_PUBLISH_IP)

    info = streams_controller.get_streamserver_info()

    assert info == StreamServerInfo(base_url=f"http://{EXTERNAL_PUBLISH_IP}:{port}")
    # what the api hands a client
    assert info.to_dict() == {"base_url": f"http://{EXTERNAL_PUBLISH_IP}:{port}"}


async def test_info_command_is_registered_read_only(mass: MusicAssistant) -> None:
    """The report is an api command anyone who may read the core config can call."""
    handler = mass.command_handlers["streams/info"]

    # registered off the streams controller itself, not off another object's attribute
    assert getattr(handler.target, "__self__", None) is mass.streams
    assert getattr(handler.target, "__name__", None) == "get_streamserver_info"
    assert handler.authenticated
    assert handler.required_scope == Scope.CONFIG_CORE_READ


async def test_info_route_answers_a_cross_origin_probe_with_the_server_id(
    streams_controller: StreamsController, mass_minimal: MusicAssistant
) -> None:
    """A probe from a page on another device gets the server id back."""
    await _setup_streams(streams_controller, mass_minimal)

    async with (
        aiohttp.ClientSession() as session,
        session.get(
            f"{streams_controller.base_url}/info", headers={"Origin": PROBE_ORIGIN}
        ) as response,
    ):
        assert response.status == 200
        assert response.headers["Access-Control-Allow-Origin"] == "*"
        assert await response.json() == {"server_id": mass_minimal.server_id}


async def test_info_route_answers_the_cors_preflight(
    streams_controller: StreamsController, mass_minimal: MusicAssistant
) -> None:
    """A browser that asks first, before it probes, is allowed through."""
    await _setup_streams(streams_controller, mass_minimal)

    async with (
        aiohttp.ClientSession() as session,
        session.options(
            f"{streams_controller.base_url}/info",
            headers={"Origin": PROBE_ORIGIN, "Access-Control-Request-Method": "GET"},
        ) as response,
    ):
        assert response.status == 204
        assert response.headers["Access-Control-Allow-Origin"] == "*"
        assert "GET" in response.headers["Access-Control-Allow-Methods"]
