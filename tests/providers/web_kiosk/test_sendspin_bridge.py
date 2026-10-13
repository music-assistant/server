"""Tests for the Sendspin bridge policy and client identity."""

from __future__ import annotations

from typing import cast
from unittest.mock import AsyncMock, Mock, patch

from music_assistant.providers.web_kiosk.constants import normalize_sendspin_client_id
from music_assistant.providers.web_kiosk.player import WebKioskPlayer
from music_assistant.providers.web_kiosk.provider import WebKioskProvider
from music_assistant.providers.web_kiosk.sendspin_bridge import (
    WebKioskSendspinBridge,
    WebKioskSendspinBridgeManager,
)

BROWSER_CLIENT_ID = "AbCdEfGhIjKlMnOpQrStUv"


def test_normalize_sendspin_client_id_accepts_browser_identity() -> None:
    """A sendspin-js 5 public-key id is kept, and anything else is dropped."""
    assert normalize_sendspin_client_id(BROWSER_CLIENT_ID) == BROWSER_CLIENT_ID
    assert normalize_sendspin_client_id("spb_wk_abc123") is None
    assert normalize_sendspin_client_id("short") is None
    assert normalize_sendspin_client_id(None) is None


def test_bridge_client_id_is_the_browser_identity(provider: WebKioskProvider) -> None:
    """The bridge uses the id the browser announced, not a derived prefix."""
    manager = WebKioskSendspinBridgeManager(provider)
    player = WebKioskPlayer(provider, "wk_test", name="Test Kiosk")
    player.sendspin_client_id = BROWSER_CLIENT_ID

    assert manager._bridge_client_id(player) == BROWSER_CLIENT_ID
    assert manager._bridge_client_id(object()) is None  # type: ignore[arg-type]


def test_should_have_bridge_only_for_enabled_kiosk_with_identity(
    player: WebKioskPlayer,
) -> None:
    """No bridge until the option is on and the browser has announced an identity."""
    manager = WebKioskSendspinBridgeManager(player.provider)
    player.provider.sendspin_bridge_enabled = True  # type: ignore[attr-defined]

    assert manager._should_have_bridge(player) is False

    player.sendspin_client_id = BROWSER_CLIENT_ID
    assert manager._should_have_bridge(player) is True

    player.provider.sendspin_bridge_enabled = False  # type: ignore[attr-defined]
    assert manager._should_have_bridge(player) is False


def test_stream_start_opens_kiosk_in_sendspin_mode(player: WebKioskPlayer) -> None:
    """A synchronized stream tells this browser to open Sendspin mode under its own id."""
    http = Mock()
    player.provider.http_server = http  # type: ignore[attr-defined]
    bridge = WebKioskSendspinBridge(
        cast("WebKioskProvider", player.provider), player, Mock(), BROWSER_CLIENT_ID
    )

    bridge._on_stream_start(Mock())

    http.broadcast_sendspin.assert_called_once()
    player_id, url = http.broadcast_sendspin.call_args.args
    assert player_id == player.player_id
    assert "sendspin=1" in url
    assert BROWSER_CLIENT_ID in url


async def test_connect_timeout_transfers_playback_back(
    player: WebKioskPlayer, mass_mock: Mock
) -> None:
    """A browser that never connects loses the stream back to the HTTP player."""
    mass_mock.player_queues.transfer_queue = AsyncMock()
    bridge = WebKioskSendspinBridge(
        cast("WebKioskProvider", player.provider), player, Mock(), BROWSER_CLIENT_ID
    )
    bridge._client = Mock(is_connected=False)

    with patch(
        "music_assistant.providers.web_kiosk.sendspin_bridge.asyncio.sleep",
        new_callable=AsyncMock,
    ):
        await bridge._watch_connect()

    mass_mock.player_queues.transfer_queue.assert_awaited_once_with(
        BROWSER_CLIENT_ID, player.player_id, auto_play=True
    )


async def test_connect_timeout_keeps_playback_when_client_connects(
    player: WebKioskPlayer, mass_mock: Mock
) -> None:
    """A browser that connected in time keeps the synchronized stream."""
    mass_mock.player_queues.transfer_queue = AsyncMock()
    bridge = WebKioskSendspinBridge(
        cast("WebKioskProvider", player.provider), player, Mock(), BROWSER_CLIENT_ID
    )
    bridge._client = Mock(is_connected=True)

    with patch(
        "music_assistant.providers.web_kiosk.sendspin_bridge.asyncio.sleep",
        new_callable=AsyncMock,
    ):
        await bridge._watch_connect()

    mass_mock.player_queues.transfer_queue.assert_not_awaited()


async def test_note_sendspin_client_id_replaces_previous_bridge(
    provider: WebKioskProvider, player: WebKioskPlayer
) -> None:
    """A new browser identity drops the previous bridge before the next one is built."""
    manager = Mock()
    manager.evaluate_bridge = AsyncMock()
    manager.remove_bridge = AsyncMock()
    provider.bridge_manager = manager

    await provider.note_sendspin_client_id(player, BROWSER_CLIENT_ID)

    assert player.sendspin_client_id == BROWSER_CLIENT_ID
    manager.remove_bridge.assert_not_awaited()
    manager.evaluate_bridge.assert_awaited_once_with(player)

    replacement = "ZyXwVuTsRqPoNmLkJiHgFe"
    await provider.note_sendspin_client_id(player, replacement)

    assert player.sendspin_client_id == replacement
    manager.remove_bridge.assert_awaited_once_with(player.player_id, permanent=True)
