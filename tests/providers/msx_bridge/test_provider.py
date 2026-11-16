"""Tests for MSXBridgeProvider lifecycle."""

from __future__ import annotations

from typing import Any, cast
from unittest.mock import AsyncMock, Mock, patch

import pytest
from music_assistant_models.enums import MediaType
from music_assistant_models.errors import InvalidDataError
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


@pytest.mark.parametrize("stored", ["redirect", "independent"])
def test_offered_stream_mode_is_kept(provider: MSXBridgeProvider, stored: str) -> None:
    """A stored value that is one of the offered modes is used as is."""
    cast("Any", provider.config).get_value = Mock(return_value=stored)

    assert provider._load_stream_mode() == stored


def test_unknown_stream_mode_falls_back_to_redirect(
    provider: MSXBridgeProvider, caplog: pytest.LogCaptureFixture
) -> None:
    """A stored value outside the offered modes plays as redirect and is logged."""
    cast("Any", provider.config).get_value = Mock(return_value="shared")

    assert provider._load_stream_mode() == "redirect"
    assert "Unknown stream delivery mode 'shared'" in caplog.text


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
    mass_mock.players.get_protocol = Mock(return_value=player)

    provider.on_player_activity(player.protocol_id)

    assert player.available is True


async def test_loaded_in_mass_starts_timeout_task(provider: MSXBridgeProvider) -> None:
    """loaded_in_mass should start the idle timeout task."""
    mock_task = Mock()
    provider.mass.create_task = Mock(return_value=mock_task)  # type: ignore[method-assign]

    await provider.loaded_in_mass()

    assert provider._timeout_task is mock_task


async def test_unload_stops_server_and_leaves_the_players_to_the_controller(
    provider: MSXBridgeProvider,
) -> None:
    """Unload stops the HTTP server; the players controller detaches the TVs itself."""
    mock_server = AsyncMock()
    provider.http_server = mock_server

    await provider.unload()

    mock_server.stop.assert_awaited_once()
    provider.mass.players.unregister.assert_not_awaited()  # type: ignore[attr-defined]


async def test_unload_no_server(provider: MSXBridgeProvider) -> None:
    """Unload should not crash when http_server is None."""
    provider.http_server = None

    await provider.unload()  # should not raise


async def test_a_new_tv_is_attached_with_its_own_id(provider: MSXBridgeProvider) -> None:
    """A TV seen for the first time attaches; commands address the player it got."""
    player = await provider.get_or_register_player("msx_tv", display_name="TV")

    provider.mass.players.register.assert_awaited_once_with(player)  # type: ignore[attr-defined]
    provider.mass.players.get_protocol.return_value = player  # type: ignore[attr-defined]
    assert player.protocol_id == "msx_tv"
    assert provider.get_player_id("msx_tv") == "pl_msx_tv"


def test_a_tv_without_a_player_keeps_its_own_id(provider: MSXBridgeProvider) -> None:
    """The id of a TV that is not attached is passed on as it is."""
    assert provider.get_player_id("msx_tv") == "msx_tv"


async def test_a_tv_of_a_disabled_player_is_not_attached(provider: MSXBridgeProvider) -> None:
    """The requests of a TV whose player the user disabled do not attach it."""
    provider.mass.players.wants_protocol.return_value = False  # type: ignore[attr-defined]

    player = await provider.get_or_register_player("msx_tv")

    provider.mass.players.register.assert_not_awaited()  # type: ignore[attr-defined]
    assert player.player is None
    assert await provider.get_or_register_player("msx_tv") is player


async def test_discovery_attaches_an_active_tv_whose_player_was_enabled(
    provider: MSXBridgeProvider,
) -> None:
    """Enabling the player of a TV that kept asking attaches it again."""
    provider.mass.players.wants_protocol.return_value = False  # type: ignore[attr-defined]
    player = await provider.get_or_register_player("msx_tv")
    provider.mass.players.wants_protocol.return_value = True  # type: ignore[attr-defined]

    await provider.discover_players()

    provider.mass.players.register.assert_awaited_once_with(player)  # type: ignore[attr-defined]
    assert player.player is not None


async def test_discovery_leaves_attached_and_idle_tvs_alone(provider: MSXBridgeProvider) -> None:
    """An attached TV is not attached again, an idle one waits for its next request."""
    attached = await provider.get_or_register_player("msx_attached")
    await provider.get_or_register_player("msx_idle")
    provider._player_last_activity.pop("msx_idle")
    provider.mass.players.get_protocol = Mock(  # type: ignore[method-assign]
        side_effect=lambda protocol_id: attached if protocol_id == "msx_attached" else None
    )
    provider.mass.players.register.reset_mock()  # type: ignore[attr-defined]

    await provider.discover_players()

    provider.mass.players.register.assert_not_awaited()  # type: ignore[attr-defined]


async def test_an_idle_tv_is_detached_and_comes_back_on_its_next_request(
    provider: MSXBridgeProvider,
) -> None:
    """The idle timeout detaches a TV; its next request attaches the same object again."""
    player = await provider.get_or_register_player("msx_tv")

    await provider._handle_player_unregister("msx_tv")

    provider.mass.players.unregister.assert_awaited_once_with("msx_tv")  # type: ignore[attr-defined]
    assert "msx_tv" not in provider._player_last_activity
    assert await provider.get_or_register_player("msx_tv") is player
    assert provider.mass.players.register.await_count == 2  # type: ignore[attr-defined]


async def test_a_disabled_tv_is_stopped_when_it_is_detached(
    provider: MSXBridgeProvider, player: MSXPlayer
) -> None:
    """Disabling a player detaches its TV, which stops playback on the TV."""
    mock_server = Mock()
    provider.http_server = mock_server
    provider.mass.players.wants_protocol.return_value = False  # type: ignore[attr-defined]

    await player.on_detach()

    mock_server.broadcast_stop.assert_called_once_with("msx_test")
    mock_server.cancel_streams_for_player.assert_called_once_with("msx_test")


async def test_an_idle_tv_is_not_stopped_when_it_is_detached(
    provider: MSXBridgeProvider, player: MSXPlayer
) -> None:
    """A detach of a TV the server still wants (idle timeout, unload) sends it nothing."""
    mock_server = Mock()
    provider.http_server = mock_server

    await player.on_detach()

    mock_server.broadcast_stop.assert_not_called()
    mock_server.cancel_streams_for_player.assert_not_called()


async def test_removed_player_stops_the_tv_and_is_forgotten(
    provider: MSXBridgeProvider, player: MSXPlayer
) -> None:
    """A removed player has its TV stopped, its streams cancelled and its activity dropped."""
    mock_server = Mock()
    provider.http_server = mock_server
    provider.on_player_activity(player.protocol_id)
    provider._tvs[player.protocol_id] = player

    await player.on_remove()

    mock_server.broadcast_stop.assert_called_once_with("msx_test")
    mock_server.cancel_streams_for_player.assert_called_once_with("msx_test")
    assert player.protocol_id not in provider._player_last_activity
    assert player.protocol_id not in provider._tvs


async def test_removed_player_noop_when_no_server(
    provider: MSXBridgeProvider, player: MSXPlayer
) -> None:
    """Removing a player does not crash when http_server is None."""
    provider.http_server = None

    await player.on_remove()  # should not raise


async def test_disabled_tv_noop_when_no_server(
    provider: MSXBridgeProvider, player: MSXPlayer
) -> None:
    """Detaching a disabled TV does not crash when http_server is None."""
    provider.http_server = None
    provider.mass.players.wants_protocol.return_value = False  # type: ignore[attr-defined]

    await player.on_detach()  # should not raise
