"""Tests for the Teufel Raumfeld setup flow."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from music_assistant.constants import CONF_IP_ADDRESS
from music_assistant.providers.raumfeld import setup_flow

_HOST_TARGET = "urn:schemas-raumfeld-com:device:ConfigDevice:1"
_HOST_LOCATION = "http://192.168.1.41:55800/bd7bd34d.xml"

Responder = Callable[[dict[str, str]], Awaitable[None]]


def _search_answering(*responses: dict[str, str]) -> AsyncMock:
    """Build an SSDP search that hands the given responses to its callback."""

    async def search(on_response: Responder, **_kwargs: Any) -> None:
        for response in responses:
            await on_response(response)

    return AsyncMock(side_effect=search)


async def test_the_host_is_found_on_the_network() -> None:
    """The host's address is taken from its ConfigDevice announcement."""
    search = _search_answering({"st": _HOST_TARGET, "location": _HOST_LOCATION})
    with patch.object(setup_flow, "async_search", search):
        assert await setup_flow._discover_host() == "192.168.1.41"
    assert search.call_args.kwargs["search_target"] == _HOST_TARGET


async def test_devices_answering_every_search_are_ignored() -> None:
    """A device that answers whatever is searched for is not taken for the host."""
    search = _search_answering(
        {"st": "upnp:rootdevice", "location": "http://192.168.1.67:80/description.xml"},
    )
    with patch.object(setup_flow, "async_search", search):
        assert await setup_flow._discover_host() is None


async def test_a_failing_search_finds_nothing() -> None:
    """A network that cannot be searched leaves the address to the user."""
    with patch.object(setup_flow, "async_search", AsyncMock(side_effect=OSError)):
        assert await setup_flow._discover_host() is None


def _session(setup_data: dict[str, Any]) -> MagicMock:
    """Build a setup session that submits its form unchanged."""
    session = MagicMock()
    session.context.setup_data = setup_data

    async def form(entries: list[Any], **_kwargs: Any) -> dict[str, Any]:
        return {entry.key: entry.value for entry in entries}

    session.form = AsyncMock(side_effect=form)
    session.finish = AsyncMock()
    return session


async def test_the_found_host_is_offered_in_the_form() -> None:
    """The form opens with the found host's address filled in."""
    session = _session({})
    with (
        patch.object(setup_flow, "_discover_host", AsyncMock(return_value="192.168.1.41")),
        patch.object(setup_flow, "_host_reachable", AsyncMock(return_value=True)),
    ):
        await setup_flow.run_setup(session)
    entries = session.form.call_args.args[0]
    assert next(e.value for e in entries if e.key == CONF_IP_ADDRESS) == "192.168.1.41"
    assert session.finish.call_args.args[0][CONF_IP_ADDRESS] == "192.168.1.41"


async def test_a_known_address_is_not_replaced_by_discovery() -> None:
    """Reconfiguring keeps the address already set and does not search."""
    session = _session({CONF_IP_ADDRESS: "192.168.1.99"})
    discover = AsyncMock(return_value="192.168.1.41")
    with (
        patch.object(setup_flow, "_discover_host", discover),
        patch.object(setup_flow, "_host_reachable", AsyncMock(return_value=True)),
    ):
        await setup_flow.run_setup(session)
    discover.assert_not_awaited()
    assert session.finish.call_args.args[0][CONF_IP_ADDRESS] == "192.168.1.99"
