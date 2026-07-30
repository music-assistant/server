"""Tests for the Spotify Connect provider."""

from unittest.mock import AsyncMock, MagicMock, patch

from music_assistant.providers.spotify_connect import (
    API_PORT_RANGE_END,
    API_PORT_RANGE_START,
    SpotifyConnectProvider,
)


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
