"""Tests for a Sendspin client that is away while the server still lists it."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import MagicMock

import pytest
from aiosendspin.models.types import GoodbyeReason
from aiosendspin.server import ClientConnectedEvent
from aiosendspin.server.client import SendspinClient

from music_assistant.providers.sendspin.player import SendspinBasePlayer

from .test_held_elsewhere import _HeldServerApi, _make_provider, _settle

if TYPE_CHECKING:
    from aiosendspin.server import SendspinServer

CLIENT_ID = "c1"


def _connected_client() -> tuple[SendspinBasePlayer, SendspinClient]:
    """Return the protocol of a connected client and the client, as the server keeps it."""
    server = MagicMock()
    client = SendspinClient(server, CLIENT_ID)
    client._connection = MagicMock()
    protocol = SendspinBasePlayer.__new__(SendspinBasePlayer)
    protocol._protocol_id = CLIENT_ID
    protocol._player = None
    protocol._provider = MagicMock(server_api=server)
    protocol.api = client
    protocol.update_state = MagicMock()  # type: ignore[misc, method-assign]
    return protocol, client


async def _connect_again(protocol: SendspinBasePlayer, client: SendspinClient) -> None:
    """Let the client connect again, as the server reports it to the provider."""
    client._connection = MagicMock()
    provider, _ = _make_provider(_HeldServerApi())
    cast("Any", provider.mass).players = SimpleNamespace(get_protocol=lambda _id: protocol)
    provider.event_cb(cast("SendspinServer", None), ClientConnectedEvent(CLIENT_ID))
    await _settle()


@pytest.mark.parametrize("goodbye", [None, GoodbyeReason.RESTART])
async def test_a_client_whose_connection_dropped_stays_available(
    goodbye: GoodbyeReason | None,
) -> None:
    """A client whose connection dropped keeps its player available while the server lists it."""
    protocol, client = _connected_client()

    client.detach_connection(goodbye)

    assert protocol.available is True
    cast("MagicMock", protocol.update_state).assert_not_called()


async def test_a_client_that_counts_as_away_reads_unavailable_until_it_connects_again() -> None:
    """A client that counts as away reads unavailable until it connects again, then as before."""
    protocol, client = _connected_client()
    client.detach_connection(None)
    protocol._away_on_check = True
    available_while_away = protocol.available

    await _connect_again(protocol, client)

    assert (available_while_away, protocol.available) == (False, True)
    # a later drop of its connection reads as before
    client.detach_connection(None)
    assert protocol.available is True
