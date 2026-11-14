"""Tests for how the Sendspin provider registers and unregisters the protocols of its clients."""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from music_assistant_models.enums import ProtocolRole
from music_assistant_models.player import DeviceInfo

import music_assistant.providers.sendspin.player as player_module
from music_assistant.providers.sendspin.player import SendspinPlayer
from music_assistant.providers.sendspin.provider import SendspinProvider

CLIENT_ID = "client-1"


def _provider(*, wanted: bool = True) -> tuple[SendspinProvider, MagicMock]:
    """
    Return a provider whose server knows one connected client with its hello.

    :param wanted: Whether the server wants the protocol of the client attached.
    """
    provider = SendspinProvider.__new__(SendspinProvider)
    mass = MagicMock()
    mass.players.register = AsyncMock()
    mass.players.unregister = AsyncMock()
    mass.players.wants_protocol.return_value = wanted
    provider.mass = mass
    provider.logger = logging.getLogger("test.sendspin.client_events")
    client = MagicMock(info_or_none=SimpleNamespace(device_info=None))
    provider.server_api = MagicMock(get_client=lambda _client_id: client)
    provider._unloading = False
    provider._bridge_identifiers = {}
    provider._pending_unregisters = {}
    provider._client_event_versions = {}
    provider._client_event_task_counts = {}
    provider._await_client_hello = AsyncMock(return_value=True)  # type: ignore[method-assign]
    provider._auto_trust_guest_access = AsyncMock()  # type: ignore[method-assign]
    provider._apply_hass_esphome_enrichment = AsyncMock()  # type: ignore[method-assign]
    return provider, mass


async def test_nothing_is_set_up_for_the_client_of_a_disabled_player() -> None:
    """
    Nothing reaches a client the server does not want: it is neither attached nor approved.

    The server is asked with the protocol object the client would be, which decides whether
    the stored config of the protocol applies to it.
    """
    provider, mass = _provider(wanted=False)
    created = MagicMock(device_info=DeviceInfo())
    provider._create_player = MagicMock(return_value=created)  # type: ignore[method-assign]

    await provider._handle_client_added(CLIENT_ID, provider._begin_client_event(CLIENT_ID))

    mass.players.wants_protocol.assert_called_once_with(CLIENT_ID, created)
    provider._auto_trust_guest_access.assert_not_awaited()  # type: ignore[attr-defined]
    mass.players.register.assert_not_awaited()


async def test_a_reconnected_client_replaces_the_attached_object() -> None:
    """The new object of a client takes the place of the attached one, on the same player."""
    provider, mass = _provider()
    existing = MagicMock(device_info=DeviceInfo())
    created = MagicMock(device_info=DeviceInfo())
    mass.players.get_protocol.return_value = existing
    existing.provider = provider
    provider._create_player = MagicMock(return_value=created)  # type: ignore[method-assign]

    await provider._handle_client_added(CLIENT_ID, provider._begin_client_event(CLIENT_ID))

    mass.players.register.assert_awaited_once_with(created)
    mass.players.unregister.assert_not_awaited()


async def test_a_disconnected_client_is_detached() -> None:
    """A client that went away leaves its player, which stays."""
    provider, mass = _provider()

    await provider._handle_client_removed(CLIENT_ID, provider._begin_client_event(CLIENT_ID))

    mass.players.unregister.assert_awaited_once_with(CLIENT_ID)


@pytest.mark.parametrize(("session_only_after", "moves"), [(False, False), (True, True)])
async def test_a_new_hello_that_changes_what_the_client_is_moves_it(
    session_only_after: bool, moves: bool
) -> None:
    """A client whose new hello makes it a session of its own leaves its player first."""
    provider, mass = _provider()
    existing = SendspinPlayer.__new__(SendspinPlayer)
    existing._protocol_id = CLIENT_ID
    existing._player = None
    existing._attr_device_info = DeviceInfo()
    existing._attr_session_only = False
    existing._attr_underlying_protocol_id = "cast-protocol"
    existing._attr_role = ProtocolRole.AUDIO
    existing._provider = provider

    def _refresh(_client: Any) -> None:
        existing._attr_session_only = session_only_after

    existing._refresh_client_info = _refresh  # type: ignore[method-assign,assignment]
    existing.restore_bridge_identity = MagicMock()  # type: ignore[method-assign]
    mass.players.get_protocol.return_value = existing

    await provider._handle_client_updated(CLIENT_ID, provider._begin_client_event(CLIENT_ID))

    assert mass.players.unregister.await_count == (1 if moves else 0)
    mass.players.register.assert_awaited_once_with(existing)
    if moves:
        assert existing.underlying_protocol_id is None
        assert existing.role is ProtocolRole.NATIVE
    else:
        assert existing.underlying_protocol_id == "cast-protocol"


def _bridge_client(*, wanted: bool) -> tuple[SendspinPlayer, list[Any]]:
    """
    Return the Sendspin protocol of a bridge client, and the tasks it starts.

    :param wanted: Whether the server wants it attached (its output is switched on).
    """
    protocol = SendspinPlayer.__new__(SendspinPlayer)
    protocol._protocol_id = "bridge_aabbccddeeff"
    protocol._player = None
    protocol._attr_underlying_protocol_id = "apaabbccddeeff"
    protocol._on_detach_callbacks = []
    protocol.playback_session = MagicMock(close=AsyncMock())
    protocol.logger = logging.getLogger("test.sendspin.client_events")
    tasks: list[Any] = []
    protocol.mass = MagicMock(create_task=lambda coro, **_kwargs: tasks.append(coro))
    protocol.mass.players.wants_protocol.return_value = wanted
    return protocol, tasks


@pytest.mark.parametrize("wanted", [True, False])
async def test_a_bridge_output_switched_off_takes_its_bridge_along(
    monkeypatch: pytest.MonkeyPatch, wanted: bool
) -> None:
    """A bridge client detached because its output was switched off has its bridge go too."""
    evaluate = MagicMock(return_value="evaluation")
    monkeypatch.setattr(player_module, "evaluate_bridge_of", evaluate)
    protocol, tasks = _bridge_client(wanted=wanted)

    with patch.object(SendspinPlayer, "_unsubscribe_client_callbacks"):
        await protocol.on_detach()

    if wanted:
        evaluate.assert_not_called()
        assert tasks == []
    else:
        evaluate.assert_called_once_with(protocol.mass, "apaabbccddeeff")
        assert tasks == ["evaluation"]


@pytest.mark.parametrize("attached", [True, False])
async def test_a_changed_config_reaches_the_cast_app(attached: bool) -> None:
    """A config change of a Cast bridge client is pushed to its Cast app, not at attach."""
    protocol, tasks = _bridge_client(wanted=True)
    protocol._reported_static_delay = None
    # subscribed to its client once it is attached
    protocol.unsub_event_cb = MagicMock() if attached else None
    bridge = MagicMock(is_cast_app_active=True)
    bridge.push_runtime_config_update = MagicMock(return_value="push")
    manager = MagicMock(get_bridge_by_client_id=MagicMock(return_value=bridge))
    protocol._get_cast_bridge_manager = MagicMock(return_value=manager)  # type: ignore[method-assign]
    protocol._apply_preferred_format = AsyncMock()  # type: ignore[method-assign]
    protocol._apply_static_delay = AsyncMock()  # type: ignore[method-assign]

    await protocol.on_config_updated()

    assert tasks == (["push"] if attached else [])
    if attached:
        manager.get_bridge_by_client_id.assert_called_once_with("bridge_aabbccddeeff")
