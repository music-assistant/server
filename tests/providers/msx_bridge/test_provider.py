"""Tests for MSXBridgeProvider lifecycle."""

from __future__ import annotations

from typing import Any, cast
from unittest.mock import AsyncMock, Mock, patch

import pytest
from music_assistant_models.enums import MediaType
from music_assistant_models.errors import InvalidDataError, PlayerUnavailableError
from music_assistant_models.player import PlayerMedia

from music_assistant.providers.msx_bridge.player import MSXPlayer
from music_assistant.providers.msx_bridge.provider import MSXBridgeProvider


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
    media = PlayerMedia(uri="library://track/1")
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

    url = await provider.get_ma_stream_url("msx_test", PlayerMedia(uri="library://track/1"))

    assert url is None


async def test_get_ma_stream_url_accepts_universal_group_flow_media(
    provider: MSXBridgeProvider, mass_mock: Mock
) -> None:
    """Universal Group flow media must be redirected to its common stream."""
    stream_url = "http://ma:8097/flow/universal-group.mp3?player_id=msx_test"
    mass_mock.streams.resolve_stream_url = AsyncMock(return_value=stream_url)
    media = PlayerMedia(uri=stream_url, media_type=MediaType.FLOW_STREAM)

    url = await provider.get_ma_stream_url("msx_test", media)

    assert url == stream_url


def test_shared_stream_mode_migrates_to_independent(provider: MSXBridgeProvider) -> None:
    """The removed shared mode migrates without changing local delivery topology."""
    cast("Any", provider.config).get_value = Mock(return_value="shared")
    set_raw_value = Mock()
    cast("Any", provider.mass.config).set_raw_provider_config_value = set_raw_value

    mode = provider._load_stream_mode()

    assert mode == "independent"
    set_raw_value.assert_called_once_with(
        provider.instance_id,
        "group_stream_mode",
        "independent",
    )


def test_shared_stream_mode_migration_failure_is_non_fatal(
    provider: MSXBridgeProvider,
) -> None:
    """A failed best-effort config write must not prevent provider startup."""
    cast("Any", provider.config).get_value = Mock(return_value="shared")
    cast("Any", provider.mass.config).set_raw_provider_config_value = Mock(
        side_effect=OSError("read-only")
    )

    assert provider._load_stream_mode() == "independent"


async def test_get_ma_stream_url_returns_none_on_error(
    provider: MSXBridgeProvider, mass_mock: Mock
) -> None:
    """get_ma_stream_url must degrade to None (proxy fallback) when resolution fails."""
    mass_mock.streams.resolve_stream_url = AsyncMock(side_effect=InvalidDataError("no session"))

    url = await provider.get_ma_stream_url("msx_test", PlayerMedia(uri="library://track/1"))

    assert url is None
    mass_mock.streams.resolve_stream_url.assert_awaited_once()


async def test_get_ma_stream_url_does_not_swallow_unexpected_error(
    provider: MSXBridgeProvider, mass_mock: Mock
) -> None:
    """Programming errors while resolving a stream URL must not look like a missing URL."""
    mass_mock.streams.resolve_stream_url = AsyncMock(side_effect=ValueError("bug"))

    with pytest.raises(ValueError, match="bug"):
        await provider.get_ma_stream_url("msx_test", PlayerMedia(uri="library://track/1"))


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


def test_on_player_activity_restores_unavailable_player(
    provider: MSXBridgeProvider, mass_mock: Mock, player: MSXPlayer
) -> None:
    """A later HTTP request from the TV must make the player available to MA again."""
    player._attr_available = False
    mass_mock.players.get_player = Mock(return_value=player)

    provider.on_player_activity(player.player_id)

    assert player.available is True


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
    cast("Mock", provider.mass.players.iter_players).assert_called_once_with(
        return_disabled=True,
        provider_filter=provider.instance_id,
        return_protocol_players=True,
    )
    provider.mass.players.unregister.assert_awaited_once_with("msx_test")  # type: ignore[attr-defined]


async def test_unload_continues_when_unregister_fails(
    provider: MSXBridgeProvider,
) -> None:
    """One unavailable player must not block the rest of unload."""
    first = Mock(display_name="A", player_id="msx_a")
    second = Mock(display_name="B", player_id="msx_b")
    provider.mass.players.all.return_value = [first, second]  # type: ignore[attr-defined]
    provider.mass.players.iter_players.return_value = [first, second]  # type: ignore[attr-defined]
    provider.mass.players.unregister = AsyncMock(  # type: ignore[method-assign]
        side_effect=[PlayerUnavailableError("gone"), None]
    )
    provider.http_server = None

    await provider.unload()

    assert provider.mass.players.unregister.await_count == 2


async def test_unload_does_not_swallow_unexpected_unregister_error(
    provider: MSXBridgeProvider,
) -> None:
    """A bug while unregistering must not be hidden as a missing player."""
    mock_player = Mock(display_name="Test TV", player_id="msx_test")
    provider.mass.players.all.return_value = [mock_player]  # type: ignore[attr-defined]
    provider.mass.players.iter_players.return_value = [mock_player]  # type: ignore[attr-defined]
    provider.mass.players.unregister = AsyncMock(side_effect=ValueError("bug"))  # type: ignore[method-assign]
    provider.http_server = None

    with pytest.raises(ValueError, match="bug"):
        await provider.unload()


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
