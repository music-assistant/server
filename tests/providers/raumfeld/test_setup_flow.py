"""Tests for the Teufel Raumfeld setup flow."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from music_assistant.constants import CONF_IP_ADDRESS, CONF_PORT
from music_assistant.models.setup_flow import SetupFlowError
from music_assistant.providers.raumfeld import DEFAULT_PORT, helpers, setup_flow

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
    with patch.object(helpers, "async_search", search):
        assert await setup_flow._discover_host() == "192.168.1.41"
    assert search.call_args.kwargs["search_target"] == _HOST_TARGET


async def test_devices_answering_every_search_are_ignored() -> None:
    """A device that answers whatever is searched for is not taken for the host."""
    search = _search_answering(
        {"st": "upnp:rootdevice", "location": "http://192.168.1.67:80/description.xml"},
    )
    with patch.object(helpers, "async_search", search):
        assert await setup_flow._discover_host() is None


async def test_a_failing_search_finds_nothing() -> None:
    """A network that cannot be searched leaves the address to the user."""
    with patch.object(helpers, "async_search", AsyncMock(side_effect=OSError)):
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


async def test_an_address_where_no_host_answers_is_asked_again() -> None:
    """The form comes back with an error until a host answers at the address given."""
    session = _session({})
    answers = [{CONF_IP_ADDRESS: "192.168.1.99"}, {CONF_IP_ADDRESS: "192.168.1.41"}]
    session.form = AsyncMock(side_effect=answers)
    reachable = AsyncMock(side_effect=[False, True])
    with (
        patch.object(setup_flow, "_discover_host", AsyncMock(return_value=None)),
        patch.object(setup_flow, "_host_reachable", reachable),
    ):
        await setup_flow.run_setup(session)
    assert session.form.await_count == 2
    assert session.form.await_args_list[0].kwargs["errors"] is None
    assert session.form.await_args_list[1].kwargs["errors"] == {CONF_IP_ADDRESS: "no_host_found"}
    session.finish.assert_awaited_once()
    assert session.finish.call_args.args[0][CONF_IP_ADDRESS] == "192.168.1.41"


async def test_a_setup_that_cannot_be_saved_shows_why() -> None:
    """A refused finish brings the form back with the reason."""
    session = _session({CONF_IP_ADDRESS: "192.168.1.41"})
    refused = SetupFlowError("already_configured")
    session.finish = AsyncMock(side_effect=[refused, None])
    with patch.object(setup_flow, "_host_reachable", AsyncMock(return_value=True)):
        await setup_flow.run_setup(session)
    assert session.form.await_args_list[1].kwargs["errors"] == {"base": refused}
    assert session.finish.await_count == 2


async def test_the_address_and_port_are_stored_as_checked() -> None:
    """Spaces around the address are dropped, and an empty port falls back to the default."""
    session = _session({})
    session.form = AsyncMock(return_value={CONF_IP_ADDRESS: " 192.168.1.41 ", CONF_PORT: None})
    reachable = AsyncMock(return_value=True)
    with (
        patch.object(setup_flow, "_discover_host", AsyncMock(return_value=None)),
        patch.object(setup_flow, "_host_reachable", reachable),
    ):
        await setup_flow.run_setup(session)
    assert reachable.call_args.args[1:] == ("192.168.1.41", DEFAULT_PORT)
    stored = session.finish.call_args.args[0]
    assert stored[CONF_IP_ADDRESS] == "192.168.1.41"
    assert stored[CONF_PORT] == DEFAULT_PORT


async def test_the_first_of_several_hosts_is_offered() -> None:
    """With more than one host on the network, the first one found is filled in."""
    with patch.object(
        setup_flow, "discover_hosts", AsyncMock(return_value=["192.168.1.41", "192.168.1.77"])
    ):
        assert await setup_flow._discover_host() == "192.168.1.41"


async def test_the_host_is_checked_over_the_servers_own_connection() -> None:
    """The reachability check asks the host through Music Assistant's HTTP session."""
    session = _session({})
    check = AsyncMock(return_value=True)
    with patch.object(setup_flow, "host_is_valid", check):
        assert await setup_flow._host_reachable(session, "192.168.1.41", 47365)
    check.assert_awaited_once_with(session.mass.http_session, "192.168.1.41", 47365)
