"""Tests for the streamserver publish IP resolution."""

from unittest.mock import AsyncMock, patch

from aiohttp.test_utils import unused_port

from music_assistant.constants import (
    CONF_BIND_IP,
    CONF_BIND_PORT,
    CONF_PUBLISH_IP,
    CONF_VALUE_AUTO,
)
from music_assistant.controllers.streams.controller import StreamsController
from music_assistant.mass import MusicAssistant

# TEST-NET-3 (RFC 5737) is never assigned to a host, so binding it always fails
UNBINDABLE_IP = "203.0.113.7"
# a publish IP does not have to exist on this host at all (NAT/port forward setups)
EXTERNAL_PUBLISH_IP = "203.0.113.9"
LOOPBACK_IP = "127.0.0.1"
# the address the tests bind is deliberately absent here, so a publish IP that follows
# the bind address can never be mistaken for the one auto-detection would have picked
ALL_ADDRESSES = ("192.168.1.10", "10.0.0.5", "fd00::10")


async def _setup_streams(
    controller: StreamsController,
    mass: MusicAssistant,
    bind_ip: str,
    publish_ip: str = CONF_VALUE_AUTO,
    all_ip_addresses: tuple[str, ...] = ALL_ADDRESSES,
    bind_port: int | None = None,
) -> None:
    """
    Run the streamserver's setup against the given bind and publish configuration.

    :param controller: The StreamsController to set up.
    :param mass: The MusicAssistant instance owning the controller.
    :param bind_ip: Address to configure as bind IP.
    :param publish_ip: Address to configure as publish IP ("auto" to have it resolved).
    :param all_ip_addresses: Host addresses the setup detects, in ranked order.
    :param bind_port: Port to configure as bind port (0 to have the OS assign one).
    """
    config = await mass.config.get_core_config(controller.domain)
    config.update(
        {
            CONF_BIND_IP: bind_ip,
            CONF_PUBLISH_IP: publish_ip,
            CONF_BIND_PORT: unused_port() if bind_port is None else bind_port,
        }
    )
    with (
        patch(
            "music_assistant.controllers.streams.controller.get_publish_ip_candidates",
            AsyncMock(return_value=all_ip_addresses),
        ),
        patch(
            "music_assistant.controllers.streams.controller.check_ffmpeg_version",
            AsyncMock(),
        ),
    ):
        await controller.setup(config)


async def test_specific_bind_ip_is_published(
    streams_controller: StreamsController, mass_minimal: MusicAssistant
) -> None:
    """A streamserver pinned to one interface hands players that same address."""
    await _setup_streams(streams_controller, mass_minimal, bind_ip=LOOPBACK_IP)

    assert streams_controller.publish_ip == LOOPBACK_IP
    assert streams_controller.base_url == f"http://{LOOPBACK_IP}:{streams_controller.publish_port}"


async def test_wildcard_bind_publishes_primary_address(
    streams_controller: StreamsController, mass_minimal: MusicAssistant
) -> None:
    """A streamserver on all interfaces advertises the primary detected address."""
    await _setup_streams(streams_controller, mass_minimal, bind_ip="0.0.0.0")

    assert streams_controller.publish_ip == ALL_ADDRESSES[0]
    assert (
        streams_controller.base_url
        == f"http://{ALL_ADDRESSES[0]}:{streams_controller.publish_port}"
    )


async def test_configured_publish_ip_outranks_bind_ip(
    streams_controller: StreamsController, mass_minimal: MusicAssistant
) -> None:
    """An explicitly configured publish IP stays authoritative over the bind address."""
    await _setup_streams(
        streams_controller, mass_minimal, bind_ip=LOOPBACK_IP, publish_ip=EXTERNAL_PUBLISH_IP
    )

    assert streams_controller.publish_ip == EXTERNAL_PUBLISH_IP
    assert (
        streams_controller.base_url
        == f"http://{EXTERNAL_PUBLISH_IP}:{streams_controller.publish_port}"
    )


async def test_unavailable_bind_ip_publishes_dialable_address(
    streams_controller: StreamsController, mass_minimal: MusicAssistant
) -> None:
    """A bind that fell back to all interfaces advertises a reachable address, not the failed one."""
    await _setup_streams(streams_controller, mass_minimal, bind_ip=UNBINDABLE_IP)

    assert streams_controller.publish_ip == ALL_ADDRESSES[0]
    assert (
        streams_controller.base_url
        == f"http://{ALL_ADDRESSES[0]}:{streams_controller.publish_port}"
    )


async def test_os_assigned_port_lands_in_base_url(
    streams_controller: StreamsController, mass_minimal: MusicAssistant
) -> None:
    """A port the OS only assigns at bind time ends up in the URLs handed to players."""
    await _setup_streams(streams_controller, mass_minimal, bind_ip=LOOPBACK_IP, bind_port=0)

    assert streams_controller.publish_port != 0
    assert streams_controller.base_url == f"http://{LOOPBACK_IP}:{streams_controller.publish_port}"


async def test_ipv6_publish_ip_is_bracketed_in_base_url(
    streams_controller: StreamsController, mass_minimal: MusicAssistant
) -> None:
    """An IPv6 publish IP is bracketed in the stream URLs handed to players."""
    await _setup_streams(
        streams_controller, mass_minimal, bind_ip="::", all_ip_addresses=("fd00::10",)
    )

    assert streams_controller.publish_ip == "fd00::10"
    assert streams_controller.base_url == f"http://[fd00::10]:{streams_controller.publish_port}"
