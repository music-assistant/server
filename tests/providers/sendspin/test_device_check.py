"""Tests for the check of a Sendspin client that the players controller asks for."""

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


def _connected_client(*, external: bool = False) -> tuple[SendspinBasePlayer, SendspinClient]:
    """
    Return the protocol of a connected client and the client, as the server keeps it.

    :param external: Whether the client runs in the server, as a bridge or a virtual player does.
    """
    server = MagicMock()
    server.is_external_player.return_value = external
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
async def test_a_client_whose_connection_dropped_reads_as_before_without_a_check(
    goodbye: GoodbyeReason | None,
) -> None:
    """A client away after its connection dropped keeps its player available while unchecked."""
    protocol, client = _connected_client()

    client.detach_connection(goodbye)

    assert protocol.available is True
    cast("MagicMock", protocol.update_state).assert_not_called()


async def test_a_check_of_a_connected_client_answers_and_changes_nothing() -> None:
    """A connected client answers a check and stays available."""
    protocol, _ = _connected_client()

    assert (await protocol.check_device(), protocol.available) == (True, True)
    cast("MagicMock", protocol.update_state).assert_not_called()


async def test_a_check_of_a_client_that_is_away_reads_it_unavailable_until_it_is_back() -> None:
    """A client a check finds away reads unavailable until it connects again, then as before."""
    protocol, client = _connected_client()
    client.detach_connection(None)

    assert (await protocol.check_device(), protocol.available) == (False, False)
    cast("MagicMock", protocol.update_state).assert_called_once_with()
    # asked again while it is still away, nothing changes
    assert (await protocol.check_device(), protocol.available) == (False, False)
    cast("MagicMock", protocol.update_state).assert_called_once_with()

    await _connect_again(protocol, client)

    assert protocol.available is True
    assert cast("MagicMock", protocol.update_state).call_count == 2
    # a later drop of its connection, without a check, reads as before
    client.detach_connection(None)
    assert protocol.available is True


async def test_a_client_that_runs_in_the_server_is_not_checked() -> None:
    """A bridge or a virtual player connects when it plays: it can not be checked."""
    protocol, client = _connected_client(external=True)
    client.detach_connection(None)

    assert (await protocol.check_device(), protocol.available) == (None, True)
