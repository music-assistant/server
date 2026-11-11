"""Tests for finding the Sendspin clients again that have no attached protocol."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock, MagicMock

import music_assistant.providers.sendspin.provider as provider_module
from music_assistant.constants import CONF_UNDERLYING_PLAYER_ID
from music_assistant.providers.sendspin.provider import SendspinProvider

if TYPE_CHECKING:
    import pytest


def _client(client_id: str, *, connected: bool = True) -> Any:
    return SimpleNamespace(client_id=client_id, is_connected=connected)


def _provider(
    clients: list[Any],
    *,
    attached: tuple[str, ...] = (),
    external: tuple[str, ...] = (),
    unwanted: tuple[str, ...] = (),
    records: dict[str, dict[str, Any]] | None = None,
) -> tuple[SendspinProvider, list[str]]:
    """
    Return a provider whose server knows the given clients, and the ids it replays.

    :param clients: The clients the Sendspin server knows.
    :param attached: Ids of the clients whose protocol is attached.
    :param external: Ids of the clients registered in-process (bridges, lights).
    :param unwanted: Ids of the clients of a disabled player or switched off output.
    :param records: The stored protocol records of the provider, by id.
    """
    records = records or {}
    provider = SendspinProvider.__new__(SendspinProvider)
    provider.mass = MagicMock()
    provider.config = MagicMock(instance_id="sendspin")
    provider.mass.players.get_protocol.side_effect = lambda client_id: (
        MagicMock(provider=provider) if client_id in attached else None
    )
    provider.mass.players.wants_protocol.side_effect = lambda client_id: client_id not in unwanted
    provider.mass.config.get_protocol_ids_of_provider.return_value = list(records)
    provider.mass.config.get_protocol_value.side_effect = lambda protocol_id, key, default=None: (
        records[protocol_id].get(key, default)
    )
    provider.server_api = MagicMock(
        clients=clients,
        is_external_player=lambda client_id: client_id in external,
        get_client=lambda client_id: next(
            (client for client in clients if client.client_id == client_id), None
        ),
    )
    provider._client_event_versions = {}
    provider._client_event_task_counts = {}
    added: list[str] = []
    provider._handle_client_added = MagicMock(  # type: ignore[method-assign]
        side_effect=lambda client_id, _version: added.append(client_id)
    )
    return provider, added


async def test_a_connected_client_without_a_protocol_is_added_again() -> None:
    """A connected client whose player was removed is added again, an attached one is not."""
    provider, added = _provider([_client("attached"), _client("removed")], attached=("attached",))

    await provider.discover_players()

    assert added == ["removed"]


async def test_a_client_registered_in_process_is_added_again() -> None:
    """A bridge or light client has no connection of its own and is added again all the same."""
    provider, added = _provider(
        [_client("bridge_aabbccddeeff", connected=False), _client("gone", connected=False)],
        external=("bridge_aabbccddeeff",),
    )

    await provider.discover_players()

    assert added == ["bridge_aabbccddeeff"]


async def test_a_client_whose_event_is_being_handled_is_left_to_it() -> None:
    """A client still being added, updated or removed is not added a second time."""
    provider, added = _provider([_client("busy")])
    provider._client_event_versions["busy"] = 1

    await provider.discover_players()

    assert added == []


async def test_nothing_is_set_up_for_a_client_the_server_does_not_want() -> None:
    """A client of a disabled player gets no protocol object, so nothing is sent to it."""
    provider, added = _provider([_client("disabled")], unwanted=("disabled",))

    await provider.discover_players()

    assert added == []
    cast("MagicMock", provider.mass).create_task.assert_not_called()


async def test_a_bridge_switched_on_again_is_set_up_by_the_manager_of_its_base(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bridge output switched on again has no client yet: its base protocol sets it up."""
    evaluate = AsyncMock()
    monkeypatch.setattr(provider_module, "evaluate_bridge_of", evaluate)
    provider, added = _provider(
        [],
        records={
            "bridge_aabbccddeeff": {CONF_UNDERLYING_PLAYER_ID: "apaabbccddeeff"},
            "bridge_off": {CONF_UNDERLYING_PLAYER_ID: "apoff"},
            "esp32": {},
        },
        unwanted=("bridge_off",),
    )

    await provider.discover_players()

    evaluate.assert_awaited_once_with(provider.mass, "apaabbccddeeff")
    assert added == []
