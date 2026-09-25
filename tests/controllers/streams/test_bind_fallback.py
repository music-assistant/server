"""Tests for the streamserver adopting the address it actually bound to."""

from unittest.mock import AsyncMock

from music_assistant.controllers.streams.controller import StreamsController
from music_assistant.mass import MusicAssistant

from .conftest import setup_streams

# TEST-NET-3 (RFC 5737) represents an unavailable configured address
UNBINDABLE_IP = "203.0.113.7"
ALL_ADDRESSES = ("192.168.1.10", "fd00::10")
FALLBACK_PORT = 8097


async def test_unavailable_bind_ip_publishes_dialable_addresses(
    streams_controller: StreamsController,
    mass_minimal: MusicAssistant,
    streamserver_fallback: AsyncMock,
) -> None:
    """An address that cannot be bound leaves the streamserver advertising reachable addresses."""
    await setup_streams(
        streams_controller,
        mass_minimal,
        bind_ip=UNBINDABLE_IP,
        all_ip_addresses=ALL_ADDRESSES,
        bind_port=FALLBACK_PORT,
    )

    streamserver_fallback.assert_awaited_once()
    assert (setup_call := streamserver_fallback.await_args) is not None
    assert setup_call.kwargs["bind_ip"] == UNBINDABLE_IP
    assert setup_call.kwargs["bind_port"] == FALLBACK_PORT
    assert streams_controller.bind_ip == "0.0.0.0"
    assert streams_controller._publish_addresses == list(ALL_ADDRESSES)


async def test_available_bind_ip_publishes_only_that_address(
    streams_controller: StreamsController, mass_minimal: MusicAssistant
) -> None:
    """A bind that succeeded advertises exactly the interface the streamserver is pinned to."""
    await setup_streams(
        streams_controller, mass_minimal, bind_ip="127.0.0.1", all_ip_addresses=ALL_ADDRESSES
    )

    assert streams_controller.bind_ip == "127.0.0.1"
    assert streams_controller._publish_addresses == ["127.0.0.1"]
