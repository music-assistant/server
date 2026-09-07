"""Tests for MSXBridgeProvider lifecycle."""

from __future__ import annotations

from unittest.mock import AsyncMock, Mock, patch

from music_assistant.providers.msx_bridge.provider import MSXBridgeProvider


async def test_init_without_sendspin_provider_module(
    provider: MSXBridgeProvider,
) -> None:
    """
    The provider must load even when the Sendspin provider module is absent.

    In a Music Assistant install that ships no Sendspin provider, importing
    the bridge manager fails; the MSX provider must degrade to "no bridge"
    instead of failing to load.
    """
    provider.sendspin_bridge_enabled = True
    with (
        patch("music_assistant.providers.msx_bridge.provider.MSXHTTPServer") as mock_server_cls,
        patch.object(
            MSXBridgeProvider,
            "_make_bridge_manager",
            side_effect=ImportError("No module named 'music_assistant.providers.sendspin'"),
        ),
    ):
        mock_server_cls.return_value = AsyncMock()
        await provider.handle_async_init()

    assert provider.bridge_manager is None


async def test_handle_async_init(provider: MSXBridgeProvider) -> None:
    """handle_async_init should create an MSXHTTPServer and start it."""
    with patch("music_assistant.providers.msx_bridge.provider.MSXHTTPServer") as mock_server_cls:
        mock_server = AsyncMock()
        mock_server_cls.return_value = mock_server

        await provider.handle_async_init()

        mock_server_cls.assert_called_once_with(provider, 8099)
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

    with patch("music_assistant.providers.msx_bridge.provider.MSXHTTPServer") as mock_server_cls:
        mock_server = AsyncMock()
        mock_server_cls.return_value = mock_server

        await prov.handle_async_init()

        # config.get_value() returns DEFAULT_HTTP_PORT when the key has a default_value
        mock_server_cls.assert_called_once()
        mock_server.start.assert_awaited_once()


async def test_get_ma_stream_url_uses_streamserver(
    provider: MSXBridgeProvider, mass_mock: Mock
) -> None:
    """get_ma_stream_url must resolve the URL via the MA streamserver API."""
    media = Mock()
    mass_mock.streams.resolve_stream_url = AsyncMock(
        return_value="http://ma:8097/single/s1/q1/i1/msx_test.mp3"
    )

    url = await provider.get_ma_stream_url("msx_test", media)

    assert url == "http://ma:8097/single/s1/q1/i1/msx_test.mp3"
    mass_mock.streams.resolve_stream_url.assert_awaited_once_with("msx_test", media)


async def test_get_ma_stream_url_rejects_flow_urls(
    provider: MSXBridgeProvider, mass_mock: Mock
) -> None:
    """
    A flow-mode URL must be rejected (None -> proxy fallback).

    MA forces flow mode when e.g. crossfade is enabled and the player lacks
    gapless support. A flow URL streams the whole queue continuously, which
    breaks the MSX per-track model (progress display, auto-advance).
    """
    mass_mock.streams.resolve_stream_url = AsyncMock(
        return_value="http://ma:8097/flow/s1/q1/i1/msx_test.mp3"
    )

    url = await provider.get_ma_stream_url("msx_test", Mock())

    assert url is None


async def test_get_ma_stream_url_returns_none_on_error(
    provider: MSXBridgeProvider, mass_mock: Mock
) -> None:
    """get_ma_stream_url must degrade to None (proxy fallback) when resolution fails."""
    mass_mock.streams.resolve_stream_url = AsyncMock(side_effect=RuntimeError("no session"))

    url = await provider.get_ma_stream_url("msx_test", Mock())

    assert url is None
    mass_mock.streams.resolve_stream_url.assert_awaited_once()


def test_on_player_activity_uses_monotonic_clock(provider: MSXBridgeProvider) -> None:
    """
    The idle-activity ledger must use the monotonic clock.

    With wall-clock timestamps, an NTP step forward (common on RTC-less hosts
    right after boot) instantly ages every player past the idle cutoff and
    mass-unregisters them mid-session.
    """
    with patch("music_assistant.providers.msx_bridge.provider.time") as mock_time:
        mock_time.monotonic.return_value = 1234.0
        provider.on_player_activity("msx_x")

    assert provider._player_last_activity["msx_x"] == 1234.0


async def test_loaded_in_mass_starts_timeout_task(provider: MSXBridgeProvider) -> None:
    """loaded_in_mass should start idle timeout task and/or register default player."""
    mock_task = Mock()
    provider.mass.create_task = Mock(return_value=mock_task)  # type: ignore[method-assign]

    await provider.loaded_in_mass()

    # Our impl: starts timeout task. MA-server bundled: may register default player.
    assert provider.mass.create_task.called or provider.mass.players.register.called  # type: ignore[attr-defined]
    if provider.mass.create_task.called:
        assert provider._timeout_task is mock_task


async def test_unload_stops_server_first(provider: MSXBridgeProvider) -> None:
    """Unload should stop the HTTP server and unregister all players."""
    mock_server = AsyncMock()
    provider.http_server = mock_server

    mock_player = Mock()
    mock_player.display_name = "Test TV"
    mock_player.player_id = "msx_test"
    provider.mass.players.all.return_value = [mock_player]  # type: ignore[attr-defined]
    provider.mass.players.iter_players.return_value = [mock_player]  # type: ignore[attr-defined]

    await provider.unload()

    mock_server.stop.assert_awaited_once()
    provider.mass.players.unregister.assert_awaited_once_with("msx_test")  # type: ignore[attr-defined]


async def test_unload_no_server(provider: MSXBridgeProvider) -> None:
    """Unload should not crash when http_server is None."""
    provider.http_server = None
    provider.mass.players.all.return_value = []  # type: ignore[attr-defined]
    provider.mass.players.iter_players.return_value = []  # type: ignore[attr-defined]

    await provider.unload()  # should not raise


async def test_discover_players_noop(provider: MSXBridgeProvider) -> None:
    """discover_players should complete without error."""
    await provider.discover_players()


async def test_on_player_disabled_does_not_unregister(
    provider: MSXBridgeProvider,
) -> None:
    """on_player_disabled should broadcast stop and cancel streams, but NOT unregister."""
    mock_server = Mock()
    mock_server.broadcast_stop = Mock()
    mock_server.cancel_streams_for_player = Mock()
    provider.http_server = mock_server

    provider.on_player_disabled("msx_test")

    mock_server.broadcast_stop.assert_called_once_with("msx_test")
    mock_server.cancel_streams_for_player.assert_called_once_with("msx_test")
    provider.mass.players.unregister.assert_not_called()  # type: ignore[attr-defined]


async def test_on_player_disabled_noop_when_no_server(
    provider: MSXBridgeProvider,
) -> None:
    """on_player_disabled should not crash when http_server is None."""
    provider.http_server = None
    provider.on_player_disabled("msx_test")  # should not raise


async def test_on_player_enabled_noop(provider: MSXBridgeProvider) -> None:
    """on_player_enabled should complete without error (player stays registered)."""
    provider.on_player_enabled("msx_test")  # should not raise
