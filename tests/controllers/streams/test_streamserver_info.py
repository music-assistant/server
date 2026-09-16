"""Tests for the streamserver's published-address report and its reachability probe."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import aiohttp
from aiohttp.test_utils import unused_port
from music_assistant_models.auth import Scope

from music_assistant.constants import CONF_BIND_IP, CONF_BIND_PORT, CONF_PUBLISH_IP, CONF_VALUE_AUTO
from music_assistant.controllers.streams.controller import StreamsController, StreamServerInfo

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant

LOOPBACK_IP = "127.0.0.1"
# a publish IP does not have to exist on this host at all (NAT/port forward setups)
EXTERNAL_PUBLISH_IP = "203.0.113.9"


async def _setup_streams(
    controller: StreamsController,
    mass: MusicAssistant,
    port: int,
    publish_ip: str = CONF_VALUE_AUTO,
) -> None:
    """
    Run the streamserver's setup bound to loopback.

    :param controller: The StreamsController to set up.
    :param mass: The MusicAssistant instance owning the controller.
    :param port: Port to bind to.
    :param publish_ip: Address to configure as publish IP ("auto" to have it resolved).
    """
    config = await mass.config.get_core_config(controller.domain)
    config.update({CONF_BIND_IP: LOOPBACK_IP, CONF_PUBLISH_IP: publish_ip, CONF_BIND_PORT: port})
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


async def test_info_reports_the_published_address(
    streams_controller: StreamsController, mass_minimal: MusicAssistant
) -> None:
    """The report carries the address players are handed, as its parts and as the URL."""
    port = unused_port()
    await _setup_streams(streams_controller, mass_minimal, port, publish_ip=EXTERNAL_PUBLISH_IP)

    info = streams_controller.get_streamserver_info()

    assert info == StreamServerInfo(
        base_url=f"http://{EXTERNAL_PUBLISH_IP}:{port}", publish_ip=EXTERNAL_PUBLISH_IP, port=port
    )
    # what the api hands a client
    assert info.to_dict() == {
        "base_url": f"http://{EXTERNAL_PUBLISH_IP}:{port}",
        "publish_ip": EXTERNAL_PUBLISH_IP,
        "port": port,
    }


async def test_info_command_is_registered_read_only(mass: MusicAssistant) -> None:
    """The report is an api command anyone who may read the core config can call."""
    handler = mass.command_handlers["streams/info"]

    assert handler.required_scope == Scope.CONFIG_CORE_READ


async def test_info_route_answers_any_origin_with_the_server_id(
    streams_controller: StreamsController, mass_minimal: MusicAssistant
) -> None:
    """A probe from a browser gets the server id back, whichever origin it comes from."""
    await _setup_streams(streams_controller, mass_minimal, unused_port())

    async with (
        aiohttp.ClientSession() as session,
        session.get(f"{streams_controller.base_url}/info") as response,
    ):
        assert response.status == 200
        assert response.headers["Access-Control-Allow-Origin"] == "*"
        assert await response.json() == {"server_id": mass_minimal.server_id}
