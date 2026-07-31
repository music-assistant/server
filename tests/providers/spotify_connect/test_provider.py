"""Tests for the Spotify Connect provider."""

from collections.abc import AsyncGenerator
from unittest.mock import AsyncMock, MagicMock, patch

from music_assistant.providers.spotify_connect import (
    API_PORT_RANGE_END,
    API_PORT_RANGE_START,
    SpotifyConnectProvider,
)
from music_assistant.providers.spotify_connect.client import GoLibrespotClient


async def test_async_init_probes_api_port_on_ipv4_loopback() -> None:
    """The daemon API port is selected on the address go-librespot binds."""
    provider = object.__new__(SpotifyConnectProvider)
    provider.mass = MagicMock()
    provider.logger = MagicMock()
    provider.mass.create_task.side_effect = lambda coroutine: coroutine.close()

    with (
        patch(
            "music_assistant.providers.spotify_connect.get_go_librespot_binary",
            return_value="/usr/bin/go-librespot",
        ),
        patch(
            "music_assistant.providers.spotify_connect.select_free_port",
            new=AsyncMock(return_value=38801),
        ) as select_port,
    ):
        await provider.handle_async_init()

    select_port.assert_awaited_once_with(API_PORT_RANGE_START, API_PORT_RANGE_END, host="127.0.0.1")
    assert provider._client is not None
    assert provider._client.base_url == "http://127.0.0.1:38801"


async def test_daemon_runner_reselects_api_port_when_taken() -> None:
    """An API port taken while the daemon was down is replaced before (re)starting."""
    provider = object.__new__(SpotifyConnectProvider)
    provider.mass = MagicMock()
    provider.logger = MagicMock()
    provider.config = MagicMock()
    provider.config.name = "Spotify Test"
    provider._binary = "/usr/bin/go-librespot"
    provider._api_port = 38800
    provider._client = GoLibrespotClient(provider.mass, "http://127.0.0.1:38800", provider.logger)
    # exit the supervisor loop after a single iteration
    provider._stop_called = True
    provider._restart_error_count = 0

    async def _no_stderr() -> AsyncGenerator[str]:
        return
        yield

    proc = MagicMock()
    proc.start = AsyncMock()
    proc.close = AsyncMock()
    proc.iter_stderr = _no_stderr

    with (
        patch(
            "music_assistant.providers.spotify_connect.is_port_in_use",
            new=AsyncMock(return_value=True),
        ) as port_probe,
        patch(
            "music_assistant.providers.spotify_connect.select_free_port",
            new=AsyncMock(return_value=38801),
        ),
        patch("music_assistant.providers.spotify_connect.AsyncProcess", return_value=proc),
        patch.object(SpotifyConnectProvider, "_write_config") as write_config,
    ):
        await provider._daemon_runner()

    port_probe.assert_awaited_once_with(38800, host="127.0.0.1")
    assert provider._api_port == 38801
    assert provider._client.base_url == "http://127.0.0.1:38801"
    write_config.assert_called_once()
