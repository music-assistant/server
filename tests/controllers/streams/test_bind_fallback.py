"""Tests for the streamserver adopting the address it actually bound to."""

from unittest.mock import AsyncMock, patch

from aiohttp.test_utils import unused_port

from music_assistant.constants import CONF_BIND_IP, CONF_BIND_PORT
from music_assistant.controllers.streams.controller import StreamsController
from music_assistant.mass import MusicAssistant

# TEST-NET-3 (RFC 5737) is never assigned to a host, so binding it always fails
UNBINDABLE_IP = "203.0.113.7"
ALL_ADDRESSES = ("192.168.1.10", "fd00::10")


async def _setup_with_bind_ip(
    controller: StreamsController, mass: MusicAssistant, bind_ip: str
) -> None:
    """
    Run the streamserver's setup against a config that binds the given address.

    :param controller: The StreamsController to set up.
    :param mass: The MusicAssistant instance owning the controller.
    :param bind_ip: Address to configure as bind IP.
    """
    config = await mass.config.get_core_config(controller.domain)
    config.update({CONF_BIND_IP: bind_ip, CONF_BIND_PORT: unused_port()})
    with (
        patch(
            "music_assistant.controllers.streams.controller.get_publish_ip_candidates",
            AsyncMock(return_value=ALL_ADDRESSES),
        ),
        patch(
            "music_assistant.controllers.streams.controller.check_ffmpeg_version",
            AsyncMock(),
        ),
    ):
        await controller.setup(config)


async def test_unavailable_bind_ip_publishes_dialable_addresses(
    streams_controller: StreamsController, mass_minimal: MusicAssistant
) -> None:
    """An address that cannot be bound leaves the streamserver advertising reachable addresses."""
    await _setup_with_bind_ip(streams_controller, mass_minimal, UNBINDABLE_IP)

    assert streams_controller.bind_ip == "0.0.0.0"
    assert streams_controller._publish_addresses == list(ALL_ADDRESSES)


async def test_available_bind_ip_publishes_only_that_address(
    streams_controller: StreamsController, mass_minimal: MusicAssistant
) -> None:
    """A bind that succeeded advertises exactly the interface the streamserver is pinned to."""
    await _setup_with_bind_ip(streams_controller, mass_minimal, "127.0.0.1")

    assert streams_controller.bind_ip == "127.0.0.1"
    assert streams_controller._publish_addresses == ["127.0.0.1"]
