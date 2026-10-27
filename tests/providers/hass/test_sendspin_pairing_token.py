"""Tests for the Sendspin pairing support Home Assistant offers for its devices."""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
from hass_client.exceptions import FailedCommand, NotConnected

from music_assistant.providers.hass import HomeAssistantProvider

CLIENT_ID = "client-id"


def _provider(result: Any = None, error: Exception | None = None) -> HomeAssistantProvider:
    provider = HomeAssistantProvider.__new__(HomeAssistantProvider)
    provider.hass = SimpleNamespace(
        send_command=AsyncMock(return_value=result, side_effect=error),
        subscribe=AsyncMock(return_value=Mock(), side_effect=error),
    )
    provider.logger = logging.getLogger("test.hass")
    return provider


async def test_returns_the_token() -> None:
    """The token of a device handing one out is returned."""
    provider = _provider({"token": "SP:1TOKEN"})
    assert await provider.get_sendspin_pairing_token(CLIENT_ID) == "SP:1TOKEN"
    provider.hass.send_command.assert_awaited_once_with(
        "sendspin/get_pairing_token", client_id=CLIENT_ID
    )


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(FailedCommand("not found"), id="unknown_client"),
        pytest.param(FailedCommand("test hands out no pairing token: not ready"), id="no_token"),
        pytest.param(NotConnected(), id="connection_lost"),
    ],
)
async def test_returns_none_without_a_token(error: Exception) -> None:
    """Whatever keeps Home Assistant from handing out a token yields None."""
    provider = _provider(error=error)
    assert await provider.get_sendspin_pairing_token(CLIENT_ID) is None


async def test_subscribes_to_the_client_ids() -> None:
    """Each snapshot Home Assistant sends is passed on as a plain list of client ids."""
    provider = _provider()
    snapshots: list[list[str]] = []
    unsubscribe = await provider.subscribe_sendspin_clients(snapshots.append)
    assert unsubscribe is provider.hass.subscribe.return_value
    on_message, command = provider.hass.subscribe.await_args.args
    assert command == "sendspin/subscribe_clients"

    on_message({"id": 1, "type": "event", "event": {"client_ids": [CLIENT_ID, "other"]}})
    on_message({"id": 1, "type": "event", "event": {"client_ids": []}})
    assert snapshots == [[CLIENT_ID, "other"], []]


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(FailedCommand("Unknown command."), id="old_home_assistant"),
        pytest.param(NotConnected(), id="connection_lost"),
    ],
)
async def test_subscription_failures_yield_no_subscription(error: Exception) -> None:
    """A Home Assistant that cannot be followed yields no subscription instead of an error."""
    provider = _provider(error=error)
    assert await provider.subscribe_sendspin_clients(Mock()) is None
