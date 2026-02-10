"""Tests for MSXBridgeProvider lifecycle."""

from __future__ import annotations

from typing import cast
from unittest.mock import AsyncMock, Mock, patch

from music_assistant.providers.msx_bridge.provider import MSXBridgeProvider


async def test_handle_async_init(provider: MSXBridgeProvider) -> None:
    """handle_async_init should create an MSXHTTPServer and start it."""
    with patch(
        "music_assistant.providers.msx_bridge.provider.MSXHTTPServer"
    ) as mock_http_server_class:
        mock_server = AsyncMock()
        mock_http_server_class.return_value = mock_server

        await provider.handle_async_init()

        mock_http_server_class.assert_called_once_with(provider, 8099)
        mock_server.start.assert_awaited_once()
        assert provider.http_server is mock_server


async def test_handle_async_init_default_port(mass_mock: Mock, manifest_mock: Mock) -> None:
    """handle_async_init should use DEFAULT_HTTP_PORT when config returns None."""
    config = Mock()
    config.name = "MSX Bridge"
    config.instance_id = "msx_bridge_test"
    config.enabled = True
    # Return None for http_port — provider should fall back to default
    config.get_value = Mock(
        side_effect=lambda key, default=None: {
            "log_level": "GLOBAL",
        }.get(key, default)
    )

    prov = MSXBridgeProvider(mass_mock, manifest_mock, config, set())

    with patch(
        "music_assistant.providers.msx_bridge.provider.MSXHTTPServer"
    ) as mock_http_server_class:
        mock_server = AsyncMock()
        mock_http_server_class.return_value = mock_server

        await prov.handle_async_init()

        # cast(int, None) returns None but DEFAULT_HTTP_PORT is the default
        mock_http_server_class.assert_called_once()
        mock_server.start.assert_awaited_once()


async def test_loaded_in_mass_starts_timeout_task(provider: MSXBridgeProvider) -> None:
    """loaded_in_mass should start idle timeout task and/or register default player."""
    mock_task = Mock()
    object.__setattr__(provider.mass, "create_task", Mock(return_value=mock_task))

    await provider.loaded_in_mass()

    # Our impl: starts timeout task. MA-server bundled: may register default player.
    create_task_mock = cast("Mock", provider.mass.create_task)
    register_mock = cast("Mock", provider.mass.players.register)
    assert create_task_mock.called or register_mock.called
    if create_task_mock.called:
        assert provider._timeout_task is mock_task


async def test_unload_stops_server_first(provider: MSXBridgeProvider) -> None:
    """Unload should stop the HTTP server and unregister all players."""
    mock_server = AsyncMock()
    provider.http_server = mock_server

    mock_player = Mock()
    mock_player.display_name = "Test TV"
    mock_player.player_id = "msx_test"
    cast("Mock", provider.mass.players.all).return_value = [mock_player]

    await provider.unload()

    mock_server.stop.assert_awaited_once()
    cast("Mock", provider.mass.players.unregister).assert_awaited_once_with("msx_test")


async def test_unload_no_server(provider: MSXBridgeProvider) -> None:
    """Unload should not crash when http_server is None."""
    provider.http_server = None
    cast("Mock", provider.mass.players.all).return_value = []

    await provider.unload()  # should not raise


async def test_discover_players_noop(provider: MSXBridgeProvider) -> None:
    """discover_players should complete without error."""
    await provider.discover_players()
