"""Tests for the Sendspin bridge policy and client identity."""

from __future__ import annotations

import asyncio
from typing import cast
from unittest.mock import AsyncMock, Mock, patch

import pytest
from music_assistant_models.enums import PlaybackState
from music_assistant_models.errors import PlayerUnavailableError
from music_assistant_models.player_queue import PlayerQueue

import music_assistant.providers.web_kiosk.sendspin_bridge as bridge_module
from music_assistant.providers.web_kiosk.constants import normalize_sendspin_client_id
from music_assistant.providers.web_kiosk.ownership import (
    SendspinIdentityConflictError,
    VerifiedSendspinClaim,
)
from music_assistant.providers.web_kiosk.player import WebKioskPlayer
from music_assistant.providers.web_kiosk.provider import WebKioskProvider
from music_assistant.providers.web_kiosk.sendspin_bridge import (
    WebKioskSendspinBridge,
    WebKioskSendspinBridgeManager,
)

BROWSER_CLIENT_ID = "hSDwCYkwp1R0i33ctD73Wg2_Og0mOBr066SpjqqbTmo"


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


async def test_stream_start_opens_kiosk_in_sendspin_mode(
    player: WebKioskPlayer, mass_mock: Mock
) -> None:
    """A synchronized stream tells this browser to open Sendspin mode under its own id."""
    http = Mock()
    player.provider.http_server = http  # type: ignore[attr-defined]
    mass_mock.create_task.side_effect = asyncio.create_task
    bridge = WebKioskSendspinBridge(
        cast("WebKioskProvider", player.provider), player, Mock(), BROWSER_CLIENT_ID
    )

    bridge._on_stream_start(Mock())

    http.broadcast_sendspin.assert_called_once()
    player_id, url = http.broadcast_sendspin.call_args.args
    assert player_id == player.player_id
    assert "sendspin=1" in url
    assert BROWSER_CLIENT_ID in url
    await bridge.stop()


async def test_connect_timeout_transfers_playback_back(player: WebKioskPlayer) -> None:
    """A browser that never connects loses the stream back to the HTTP player."""
    cast("WebKioskProvider", player.provider).request_sendspin_fallback = Mock()  # type: ignore[method-assign]
    bridge = WebKioskSendspinBridge(
        cast("WebKioskProvider", player.provider), player, Mock(), BROWSER_CLIENT_ID
    )
    bridge._client = Mock(is_connected=False)

    with patch(
        "music_assistant.providers.web_kiosk.sendspin_bridge.asyncio.sleep",
        new_callable=AsyncMock,
    ):
        await bridge._watch_connect()

    cast(
        "Mock", cast("WebKioskProvider", player.provider).request_sendspin_fallback
    ).assert_called_once_with(player.player_id, BROWSER_CLIENT_ID)


async def test_connect_timeout_keeps_playback_when_client_connects(player: WebKioskPlayer) -> None:
    """A browser that connected in time keeps the synchronized stream."""
    cast("WebKioskProvider", player.provider).request_sendspin_fallback = Mock()  # type: ignore[method-assign]
    bridge = WebKioskSendspinBridge(
        cast("WebKioskProvider", player.provider), player, Mock(), BROWSER_CLIENT_ID
    )
    bridge._client = Mock(is_connected=True)

    with patch(
        "music_assistant.providers.web_kiosk.sendspin_bridge.asyncio.sleep",
        new_callable=AsyncMock,
    ):
        await bridge._watch_connect()

    cast(
        "Mock", cast("WebKioskProvider", player.provider).request_sendspin_fallback
    ).assert_not_called()


async def test_note_sendspin_client_id_keeps_the_proven_identity(
    provider: WebKioskProvider, player: WebKioskPlayer
) -> None:
    """A device keeps its proven identity; a different key needs a new device ID."""
    manager = WebKioskSendspinBridgeManager(provider)
    with (
        patch.object(manager, "evaluate_bridge", new_callable=AsyncMock) as evaluate_bridge,
        patch.object(manager, "remove_bridge", new_callable=AsyncMock) as remove_bridge,
    ):
        provider.bridge_manager = manager

        await provider.note_sendspin_client_id(
            player,
            BROWSER_CLIENT_ID,
            sendspin_claim=VerifiedSendspinClaim(player.player_id, BROWSER_CLIENT_ID),
        )

        assert player.sendspin_client_id == BROWSER_CLIENT_ID
        remove_bridge.assert_not_awaited()
        evaluate_bridge.assert_awaited_once_with(player)

        replacement = "3p7bfXt9wbTTW2HC7OQ1Nz-DQ8hbeGdNrfx-FG-IK08"
        with pytest.raises(SendspinIdentityConflictError):
            await provider.note_sendspin_client_id(
                player,
                replacement,
                sendspin_claim=VerifiedSendspinClaim(player.player_id, replacement),
            )

        assert player.sendspin_client_id == BROWSER_CLIENT_ID
        remove_bridge.assert_not_awaited()


@pytest.mark.parametrize(
    "error", [PlayerUnavailableError("Missing player"), RuntimeError("Broken transfer")]
)
async def test_fallback_reports_unexpected_transfer_failures(
    player: WebKioskPlayer, mass_mock: Mock, monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    """Expected unavailable players are logged, while a broken provider fallback fails visibly."""
    monkeypatch.setattr(bridge_module, "SENDSPIN_CONNECT_TIMEOUT", 0)
    tasks: list[asyncio.Task[None]] = []

    def spawn(coro: object) -> asyncio.Task[None]:
        task: asyncio.Task[None] = asyncio.create_task(coro)  # type: ignore[arg-type]
        tasks.append(task)
        return task

    mass_mock.create_task.side_effect = spawn
    cast("WebKioskProvider", player.provider).fallback_sendspin = AsyncMock(side_effect=error)  # type: ignore[method-assign]
    server = Mock()
    server.remove_client = AsyncMock()
    server.register_external_player.return_value = Mock(is_connected=False)
    server.register_external_player.return_value.roles_by_family.return_value = []
    player.provider.http_server = Mock()  # type: ignore[attr-defined]
    bridge = WebKioskSendspinBridge(
        cast("WebKioskProvider", player.provider), player, server, BROWSER_CLIENT_ID
    )
    player.sendspin_claim = VerifiedSendspinClaim(player.player_id, BROWSER_CLIENT_ID)
    await bridge.start()
    server.register_external_player.call_args.kwargs["on_stream_start"](Mock())
    await asyncio.gather(*tasks, return_exceptions=True)
    results = await asyncio.gather(*tasks, return_exceptions=True)
    await bridge.stop()

    # The watchdog only detects the timeout; the fallback runs as provider work.
    assert results == [None, error if isinstance(error, RuntimeError) else None]


def _timeout_fallback_setup(
    provider: WebKioskProvider,
    player: WebKioskPlayer,
    mass_mock: Mock,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[WebKioskSendspinBridgeManager, Mock, list[asyncio.Task[None]]]:
    """Register a not-yet-connected bridge whose stream start arms the watchdog."""
    monkeypatch.setattr(bridge_module, "SENDSPIN_CONNECT_TIMEOUT", 0)
    tasks: list[asyncio.Task[None]] = []

    def spawn(coro: object) -> asyncio.Task[None]:
        task: asyncio.Task[None] = asyncio.create_task(coro)  # type: ignore[arg-type]
        tasks.append(task)
        return task

    mass_mock.create_task.side_effect = spawn
    player.sendspin_client_id = BROWSER_CLIENT_ID
    player.sendspin_claim = VerifiedSendspinClaim(player.player_id, BROWSER_CLIENT_ID)
    mass_mock.players.get_player = Mock(
        side_effect=lambda player_id, **_: player if player_id == player.player_id else None
    )
    queue = PlayerQueue(
        queue_id=player.player_id,
        active=True,
        display_name="Test Kiosk",
        available=True,
        items=1,
        state=PlaybackState.PLAYING,
    )
    mass_mock.player_queues.get_active_queue = Mock(return_value=queue)
    mass_mock.player_queues.resume = AsyncMock()
    server = Mock()
    server.remove_client = AsyncMock()
    server.register_external_player.return_value = Mock(is_connected=False)
    server.register_external_player.return_value.roles_by_family.return_value = []
    provider.http_server = Mock(stop=AsyncMock())
    manager = WebKioskSendspinBridgeManager(provider)
    provider.bridge_manager = manager
    return manager, server, tasks


async def test_connect_timeout_fallback_survives_removing_its_own_bridge(
    provider: WebKioskProvider,
    player: WebKioskPlayer,
    mass_mock: Mock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The watchdog's fallback removes the bridge that owns it and still resumes HTML5."""
    manager, server, tasks = _timeout_fallback_setup(provider, player, mass_mock, monkeypatch)
    bridge = WebKioskSendspinBridge(provider, player, server, BROWSER_CLIENT_ID)
    await bridge.start()
    manager._bridges[player.player_id] = bridge

    server.register_external_player.call_args.kwargs["on_stream_start"](Mock())
    while not all(task.done() for task in tasks):
        await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 1)

    assert all(task.exception() is None for task in tasks)
    assert manager.get_bridge(player.player_id) is None
    server.remove_client.assert_awaited_once_with(BROWSER_CLIENT_ID)
    assert player.sendspin_fallback
    assert player.active_output_protocol == "native"
    mass_mock.player_queues.resume.assert_awaited_once_with(player.player_id)


async def test_unload_cancels_a_watchdog_fallback_suspended_in_resume(
    provider: WebKioskProvider,
    player: WebKioskPlayer,
    mass_mock: Mock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A timeout fallback is provider work: unload cancels it after it removed its bridge."""
    manager, server, tasks = _timeout_fallback_setup(provider, player, mass_mock, monkeypatch)
    resume_started = asyncio.Event()
    resume_cancelled = asyncio.Event()

    async def resume(_queue_id: str) -> None:
        resume_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            resume_cancelled.set()
            raise

    mass_mock.player_queues.resume = AsyncMock(side_effect=resume)
    mass_mock.players.iter_players.return_value = []
    bridge = WebKioskSendspinBridge(provider, player, server, BROWSER_CLIENT_ID)
    await bridge.start()
    manager._bridges[player.player_id] = bridge

    server.register_external_player.call_args.kwargs["on_stream_start"](Mock())
    await asyncio.wait_for(resume_started.wait(), 1)
    assert manager.get_bridge(player.player_id) is None

    try:
        await asyncio.wait_for(provider.unload(), 1)
        assert resume_cancelled.is_set()
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
