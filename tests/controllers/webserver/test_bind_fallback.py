"""Tests for the fallback to all interfaces when the configured bind IP is unavailable."""

import logging
from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING, cast
from unittest.mock import MagicMock

import aiohttp
import pytest
from aiohttp.test_utils import unused_port

from music_assistant.controllers.webserver.controller import WebserverController
from music_assistant.helpers.webserver import Webserver

if TYPE_CHECKING:
    from music_assistant_models.config_entries import CoreConfig

# TEST-NET-3 (RFC 5737) is never assigned to a host, so binding it always fails
UNBINDABLE_IP = "203.0.113.7"
ALL_ADDRESSES = ("192.168.1.10", "fd00::10")


@pytest.fixture
async def server() -> AsyncGenerator[Webserver]:
    """Yield a bare Webserver, guaranteeing its socket is released afterwards."""
    webserver = Webserver(logging.getLogger(__name__))
    try:
        yield webserver
    finally:
        await webserver.close()


@pytest.fixture
def controller() -> WebserverController:
    """Create a WebserverController carrying the state that setup() resolves."""
    mass = MagicMock()
    mass.config.get_raw_core_config_value.return_value = "GLOBAL"
    webserver = WebserverController(mass)
    config = MagicMock()
    config.get_value.return_value = False
    webserver.config = cast("CoreConfig", config)
    webserver.publish_port = 8095
    return webserver


async def test_unavailable_bind_ip_falls_back_to_all_interfaces(server: Webserver) -> None:
    """An address that cannot be bound starts the server on all interfaces instead."""
    port = unused_port()

    await server.setup(
        bind_ip=UNBINDABLE_IP, bind_port=port, base_url=f"http://{UNBINDABLE_IP}:{port}"
    )

    assert server.bind_ip is None
    assert server.port == port
    # no routes are registered on a bare Webserver, so a 404 proves it is really serving
    async with (
        aiohttp.ClientSession() as session,
        session.get(f"http://127.0.0.1:{port}/info") as response,
    ):
        assert response.status == 404


async def test_available_bind_ip_is_reported(server: Webserver) -> None:
    """A bind that succeeded reports the address it is pinned to."""
    port = unused_port()

    await server.setup(bind_ip="127.0.0.1", bind_port=port, base_url=f"http://127.0.0.1:{port}")

    assert server.bind_ip == "127.0.0.1"
    assert server.port == port


async def test_wildcard_bind_ip_is_reported_as_all_interfaces(server: Webserver) -> None:
    """A configured wildcard is reported the same way as a fallback: no pinned address."""
    port = unused_port()

    await server.setup(bind_ip="0.0.0.0", bind_port=port, base_url=f"http://0.0.0.0:{port}")

    assert server.bind_ip is None


def test_fallback_publishes_only_dialable_addresses(controller: WebserverController) -> None:
    """After a fallback, the un-bindable address is neither advertised nor dialed."""
    # what setup() resolves from the configured address, before the bind is attempted
    controller._resolve_publish_state(UNBINDABLE_IP, ALL_ADDRESSES, "http")
    assert controller.base_url == f"http://{UNBINDABLE_IP}:8095"

    # ...and what it resolves once the webserver reports it fell back to all interfaces
    controller._resolve_publish_state(None, ALL_ADDRESSES, "http")

    assert controller.bind_ip is None
    assert controller.internal_base_url == "http://127.0.0.1:8095"
    assert controller.publish_addresses == ["192.168.1.10", "fd00::10"]
    assert controller.base_url == "http://192.168.1.10:8095"


def test_successful_bind_pins_to_the_configured_address(controller: WebserverController) -> None:
    """A bind that succeeded keeps advertising exactly the address it is pinned to."""
    controller._resolve_publish_state("192.168.1.10", ALL_ADDRESSES, "http")

    assert controller.bind_ip == "192.168.1.10"
    assert controller.publish_ip == "192.168.1.10"
    assert controller.publish_addresses == ["192.168.1.10"]
    assert controller.base_url == "http://192.168.1.10:8095"
    assert controller.internal_base_url == "http://192.168.1.10:8095"
