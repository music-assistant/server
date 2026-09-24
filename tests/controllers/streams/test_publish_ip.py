"""Tests for the streamserver publish IP resolution."""

from unittest.mock import AsyncMock, patch

import pytest
from aiohttp.test_utils import unused_port

from music_assistant.constants import (
    CONF_BIND_IP,
    CONF_BIND_PORT,
    CONF_PUBLISH_IP,
    CONF_VALUE_AUTO,
)
from music_assistant.controllers.streams.controller import StreamsController
from music_assistant.mass import MusicAssistant

from .conftest import DEFAULT_PUBLISH_CANDIDATES as ALL_ADDRESSES
from .conftest import setup_streams

# a hostname typed in where an IP address is expected
HOSTNAME_PUBLISH_IP = "homeassistant.local"

# TEST-NET-3 (RFC 5737) represents an unavailable configured address
UNBINDABLE_IP = "203.0.113.7"
# a publish IP does not have to exist on this host at all (NAT/port forward setups)
EXTERNAL_PUBLISH_IP = "203.0.113.9"
LOOPBACK_IP = "127.0.0.1"
FALLBACK_PORT = 8097


async def test_specific_bind_ip_is_published(
    streams_controller: StreamsController, mass_minimal: MusicAssistant
) -> None:
    """A streamserver pinned to one interface hands players that same address."""
    await setup_streams(streams_controller, mass_minimal, bind_ip=LOOPBACK_IP)

    assert streams_controller.publish_ip == LOOPBACK_IP
    assert streams_controller.base_url == f"http://{LOOPBACK_IP}:{streams_controller.publish_port}"


async def test_wildcard_bind_publishes_primary_address(
    streams_controller: StreamsController, mass_minimal: MusicAssistant
) -> None:
    """A streamserver on all interfaces advertises the primary detected address."""
    await setup_streams(streams_controller, mass_minimal, bind_ip="0.0.0.0")

    assert streams_controller.publish_ip == ALL_ADDRESSES[0]
    assert (
        streams_controller.base_url
        == f"http://{ALL_ADDRESSES[0]}:{streams_controller.publish_port}"
    )


async def test_configured_publish_ip_outranks_bind_ip(
    streams_controller: StreamsController, mass_minimal: MusicAssistant
) -> None:
    """An explicitly configured publish IP stays authoritative over the bind address."""
    await setup_streams(
        streams_controller, mass_minimal, bind_ip=LOOPBACK_IP, publish_ip=EXTERNAL_PUBLISH_IP
    )

    assert streams_controller.publish_ip == EXTERNAL_PUBLISH_IP
    assert (
        streams_controller.base_url
        == f"http://{EXTERNAL_PUBLISH_IP}:{streams_controller.publish_port}"
    )


async def test_unavailable_bind_ip_publishes_dialable_address(
    streams_controller: StreamsController,
    mass_minimal: MusicAssistant,
    streamserver_fallback: AsyncMock,
) -> None:
    """A bind that fell back to all interfaces advertises a reachable address, not the failed one."""
    await setup_streams(
        streams_controller,
        mass_minimal,
        bind_ip=UNBINDABLE_IP,
        bind_port=FALLBACK_PORT,
    )

    streamserver_fallback.assert_awaited_once()
    assert (setup_call := streamserver_fallback.await_args) is not None
    assert setup_call.kwargs["bind_ip"] == UNBINDABLE_IP
    assert setup_call.kwargs["bind_port"] == FALLBACK_PORT
    assert streams_controller.bind_ip == "0.0.0.0"
    assert streams_controller.publish_ip == ALL_ADDRESSES[0]
    assert (
        streams_controller.base_url
        == f"http://{ALL_ADDRESSES[0]}:{streams_controller.publish_port}"
    )


async def test_os_assigned_port_lands_in_base_url(
    streams_controller: StreamsController, mass_minimal: MusicAssistant
) -> None:
    """A port the OS only assigns at bind time ends up in the URLs handed to players."""
    await setup_streams(streams_controller, mass_minimal, bind_ip=LOOPBACK_IP, bind_port=0)

    assert streams_controller.publish_port != 0
    assert streams_controller.base_url == f"http://{LOOPBACK_IP}:{streams_controller.publish_port}"


async def test_ipv6_publish_ip_is_bracketed_in_base_url(
    streams_controller: StreamsController, mass_minimal: MusicAssistant
) -> None:
    """An IPv6 publish IP is bracketed in the stream URLs handed to players."""
    await setup_streams(
        streams_controller, mass_minimal, bind_ip="::", all_ip_addresses=("fd00::10",)
    )

    assert streams_controller.publish_ip == "fd00::10"
    assert streams_controller.base_url == f"http://[fd00::10]:{streams_controller.publish_port}"


async def test_stored_hostname_publish_ip_is_reset_with_warning(
    streams_controller: StreamsController,
    mass_minimal: MusicAssistant,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A stored hostname is reset to auto with a warning and the auto-detected address is used."""
    # stored raw, since config.update() rejects a hostname
    mass_minimal.config.set_raw_core_config_value(
        StreamsController.domain, CONF_PUBLISH_IP, HOSTNAME_PUBLISH_IP
    )
    config = await mass_minimal.config.get_core_config(StreamsController.domain)
    config.update({CONF_BIND_IP: "0.0.0.0", CONF_BIND_PORT: unused_port()})

    with (
        patch(
            "music_assistant.controllers.streams.controller.get_publish_ip_candidates",
            AsyncMock(return_value=ALL_ADDRESSES),
        ),
        patch(
            "music_assistant.controllers.streams.controller.check_ffmpeg_version",
            AsyncMock(),
        ),
        caplog.at_level("WARNING", logger=streams_controller.logger.name),
    ):
        await streams_controller.setup(config)

    assert streams_controller.publish_ip == ALL_ADDRESSES[0]
    assert any("not an IP address" in record.message for record in caplog.records)
    assert (
        mass_minimal.config.get_raw_core_config_value(StreamsController.domain, CONF_PUBLISH_IP)
        == CONF_VALUE_AUTO
    )


@pytest.mark.parametrize(
    ("value", "rejected"),
    [
        (CONF_VALUE_AUTO, False),
        ("", False),
        ("192.168.1.10", False),
        ("fd00::10", False),
        (HOSTNAME_PUBLISH_IP, True),
    ],
)
async def test_publish_ip_entry_rejects_hostnames(
    streams_controller: StreamsController,
    mass_minimal: MusicAssistant,
    value: str,
    rejected: bool,
) -> None:
    """The publish IP entry accepts auto/empty/IP literals and rejects a hostname."""
    config = await mass_minimal.config.get_core_config(streams_controller.domain)

    if rejected:
        with pytest.raises(ValueError, match="not a valid value"):
            config.update({CONF_PUBLISH_IP: value})
    else:
        config.update({CONF_PUBLISH_IP: value})
