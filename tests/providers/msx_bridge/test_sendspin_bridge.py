"""Tests for the MSX Sendspin bridge (spec 0003)."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, Mock, patch

import pytest

# The bridge rides on MA's Sendspin provider; skip the whole module when the
# installed Music Assistant ships no Sendspin provider (as some CI images do).
pytest.importorskip("music_assistant.providers.sendspin")

from music_assistant_models.enums import ConfigEntryType

from music_assistant.providers.msx_bridge.constants import (
    CONF_ENABLE_SENDSPIN_BRIDGE,
)
from music_assistant.providers.msx_bridge.http_server import MSXHTTPServer
from music_assistant.providers.msx_bridge.player import MSXPlayer
from music_assistant.providers.msx_bridge.provider import MSXBridgeProvider
from music_assistant.providers.msx_bridge.sendspin_bridge import (
    MSXSendspinBridge,
    MSXSendspinBridgeManager,
    bridge_client_id_for,
)


def _wire_sendspin(mass_mock: Mock) -> tuple[Mock, Mock]:
    """Wire a mock Sendspin provider + server into mass_mock; return (provider, server)."""
    client = Mock()
    client.is_connected = False
    client.roles_by_family = Mock(return_value=[])
    server = Mock()
    server.register_external_player = Mock(return_value=client)
    server.remove_client = AsyncMock()
    sendspin_prov = Mock()
    sendspin_prov.server_api = server
    mass_mock.get_provider = Mock(
        side_effect=lambda domain: sendspin_prov if domain == "sendspin" else None
    )
    return sendspin_prov, server


# --- Client id derivation ---


def test_bridge_client_id_is_stable() -> None:
    """Client ids are stable and derived from the MSX player id."""
    assert bridge_client_id_for("msx_livingroom_tv") == "spb_msx_livingroom_tv"
    assert bridge_client_id_for("msx_livingroom_tv") == bridge_client_id_for("msx_livingroom_tv")


# --- Registration lifecycle (AC1, AC5, AC7) ---


async def test_bridge_registered_when_enabled(
    provider: MSXBridgeProvider, mass_mock: Mock, player: MSXPlayer
) -> None:
    """AC1: with the option enabled, an MSX player gets an external Sendspin client."""
    sendspin_prov, server = _wire_sendspin(mass_mock)
    mass_mock.players.get_player = Mock(return_value=player)
    provider.sendspin_bridge_enabled = True
    manager = MSXSendspinBridgeManager(provider)

    await manager.evaluate_bridge(player)

    server.register_external_player.assert_called_once()
    hello = server.register_external_player.call_args[0][0]
    assert hello.client_id == "spb_msx_test"
    sendspin_prov.register_bridge_underlying_player.assert_called_once_with(
        "spb_msx_test", "msx_test"
    )
    assert manager.get_bridge("msx_test") is not None


async def test_no_bridge_when_disabled(
    provider: MSXBridgeProvider, mass_mock: Mock, player: MSXPlayer
) -> None:
    """AC5: with the option disabled (default), no Sendspin client is registered."""
    _, server = _wire_sendspin(mass_mock)
    mass_mock.players.get_player = Mock(return_value=player)
    assert provider.sendspin_bridge_enabled is False
    manager = MSXSendspinBridgeManager(provider)

    await manager.evaluate_bridge(player)

    server.register_external_player.assert_not_called()
    assert manager.get_bridge("msx_test") is None


async def test_two_tvs_get_distinct_bridges(provider: MSXBridgeProvider, mass_mock: Mock) -> None:
    """Two registered TVs yield two bridge registrations with stable, distinct ids."""
    _, server = _wire_sendspin(mass_mock)
    players = {
        pid: MSXPlayer(provider, pid, name=pid, output_format="mp3")
        for pid in ("msx_tv1", "msx_tv2")
    }
    for p in players.values():
        p.update_state = Mock()  # type: ignore[misc, method-assign]
    mass_mock.players.get_player = Mock(side_effect=lambda pid, **_kw: players.get(pid))
    provider.sendspin_bridge_enabled = True
    manager = MSXSendspinBridgeManager(provider)

    for p in players.values():
        await manager.evaluate_bridge(p)

    assert server.register_external_player.call_count == 2
    client_ids = {call.args[0].client_id for call in server.register_external_player.call_args_list}
    assert client_ids == {"spb_msx_tv1", "spb_msx_tv2"}


async def test_unregister_removes_bridge_client(
    provider: MSXBridgeProvider, mass_mock: Mock, player: MSXPlayer
) -> None:
    """AC7: unregistering an MSX player removes its bridge client from the server."""
    _, server = _wire_sendspin(mass_mock)
    mass_mock.players.get_player = Mock(return_value=player)
    provider.sendspin_bridge_enabled = True
    provider.bridge_manager = MSXSendspinBridgeManager(provider)
    await provider.bridge_manager.evaluate_bridge(player)
    assert provider.bridge_manager.get_bridge("msx_test") is not None

    await provider._handle_player_unregister("msx_test")

    server.remove_client.assert_awaited_once_with("spb_msx_test")
    assert provider.bridge_manager.get_bridge("msx_test") is None


async def test_unload_closes_bridge_manager(
    provider: MSXBridgeProvider, mass_mock: Mock, player: MSXPlayer
) -> None:
    """Provider unload stops all bridges."""
    _, server = _wire_sendspin(mass_mock)
    mass_mock.players.get_player = Mock(return_value=player)
    provider.sendspin_bridge_enabled = True
    provider.bridge_manager = MSXSendspinBridgeManager(provider)
    await provider.bridge_manager.evaluate_bridge(player)

    await provider.unload()

    server.remove_client.assert_awaited_once_with("spb_msx_test")


# --- Stream start -> WS push (AC3) ---


async def test_stream_start_pushes_kiosk_via_ws(
    provider: MSXBridgeProvider, mass_mock: Mock, player: MSXPlayer
) -> None:
    """AC3: a Sendspin stream start pushes the TV into Sendspin kiosk mode over WS."""
    _, server = _wire_sendspin(mass_mock)
    provider.http_server = Mock()
    bridge = MSXSendspinBridge(provider, player, server, "spb_msx_test")
    await bridge.start()

    bridge._on_stream_start(Mock(client_id="spb_msx_test"))

    provider.http_server.broadcast_sendspin.assert_called_once()
    player_id, url = provider.http_server.broadcast_sendspin.call_args[0]
    assert player_id == "msx_test"
    assert "kiosk=1" in url
    assert "sendspin=1" in url
    assert "sendspin_client_id=spb_msx_test" in url


# --- Connect watchdog (AC6) ---


async def test_connect_watchdog_falls_back_to_http_player(
    provider: MSXBridgeProvider, mass_mock: Mock, player: MSXPlayer
) -> None:
    """AC6: when the TV never connects its Sendspin client, playback transfers to HTTP."""
    _, server = _wire_sendspin(mass_mock)
    server.register_external_player.return_value.is_connected = False
    mass_mock.player_queues.transfer_queue = AsyncMock()
    bridge = MSXSendspinBridge(provider, player, server, "spb_msx_test")
    await bridge.start()

    with patch(
        "music_assistant.providers.msx_bridge.sendspin_bridge.SENDSPIN_CONNECT_TIMEOUT", 0.01
    ):
        await bridge._watch_connect()

    mass_mock.player_queues.transfer_queue.assert_awaited_once_with(
        "spb_msx_test", "msx_test", auto_play=True
    )


async def test_connect_watchdog_noop_when_connected(
    provider: MSXBridgeProvider, mass_mock: Mock, player: MSXPlayer
) -> None:
    """No fallback when the TV's Sendspin client connected in time."""
    _, server = _wire_sendspin(mass_mock)
    server.register_external_player.return_value.is_connected = True
    mass_mock.player_queues.transfer_queue = AsyncMock()
    bridge = MSXSendspinBridge(provider, player, server, "spb_msx_test")
    await bridge.start()

    with patch(
        "music_assistant.providers.msx_bridge.sendspin_bridge.SENDSPIN_CONNECT_TIMEOUT", 0.01
    ):
        await bridge._watch_connect()

    mass_mock.player_queues.transfer_queue.assert_not_awaited()


# --- WS broadcast plumbing ---


async def test_broadcast_sendspin_sends_ws_message(
    provider: MSXBridgeProvider, mass_mock: Mock
) -> None:
    """broadcast_sendspin delivers a typed message to the player's WS clients."""
    server = MSXHTTPServer(provider, 0)
    ws = AsyncMock()
    ws.closed = False
    server._ws_clients["msx_test"] = {ws}
    coros: list[Any] = []

    def _capture_task(coro: Any) -> Mock:
        coros.append(coro)
        return Mock()

    mass_mock.create_task = Mock(side_effect=_capture_task)

    server.broadcast_sendspin("msx_test", "/web?kiosk=1&sendspin=1")

    assert len(coros) == 1
    await coros[0]
    ws.send_str.assert_awaited_once()
    payload = json.loads(ws.send_str.call_args[0][0])
    assert payload == {
        "type": "sendspin",
        "url": "/web?kiosk=1&sendspin=1",
        "player_id": "msx_test",
    }


# --- Provider wiring ---


async def test_config_entry_default_on(provider: MSXBridgeProvider) -> None:
    """
    The enable_sendspin_bridge config entry exists and defaults to True.

    Supersedes the original spec's AC5 (default off while experimental):
    the default was deliberately flipped on once the bridge stabilized.
    """
    entries = await provider.get_config_entries()
    entry = next(e for e in entries if e.key == CONF_ENABLE_SENDSPIN_BRIDGE)
    assert entry.type == ConfigEntryType.BOOLEAN
    assert entry.default_value is True


async def test_register_player_evaluates_bridge(
    provider: MSXBridgeProvider, mass_mock: Mock
) -> None:
    """Registering a new MSX player triggers bridge evaluation."""
    mass_mock.players.get_player = Mock(return_value=None)
    provider.bridge_manager = Mock()
    provider.bridge_manager.evaluate_bridge = AsyncMock()

    registered = await provider.get_or_register_player("msx_new")

    assert registered is not None
    provider.bridge_manager.evaluate_bridge.assert_awaited_once_with(registered)
