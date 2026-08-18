"""Tests for the Spotify Connect provider."""

from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from music_assistant_models.config_entries import ProviderConfig
from music_assistant_models.enums import ProviderType

from music_assistant.providers.spotify_connect import (
    BACKEND_SOLOIST,
    CONF_API_KEY,
    CONF_BACKEND,
    CONF_SOLOIST_CONSENT,
    CONF_VOLUME_MODE,
    SpotifyConnectProvider,
)
from music_assistant.providers.spotify_connect.backends.go_librespot import (
    API_PORT_RANGE_END,
    API_PORT_RANGE_START,
    GoLibrespotBackend,
)
from music_assistant.providers.spotify_connect.backends.soloist import (
    VOLUME_MODE_SYNC_SPOTIFY,
    SoloistBackend,
)
from music_assistant.providers.spotify_connect.clients.go_librespot import GoLibrespotClient


async def test_backend_start_probes_api_port_on_ipv4_loopback() -> None:
    """The daemon API port is selected on the address go-librespot binds."""
    backend = object.__new__(GoLibrespotBackend)
    backend.mass = MagicMock()
    backend.logger = MagicMock()
    backend.mass.create_task.side_effect = lambda coroutine: coroutine.close()

    with (
        patch(
            "music_assistant.providers.spotify_connect.backends.go_librespot"
            ".get_go_librespot_binary",
            return_value="/usr/bin/go-librespot",
        ),
        patch(
            "music_assistant.providers.spotify_connect.backends.go_librespot.select_free_port",
            new=AsyncMock(return_value=38801),
        ) as select_port,
    ):
        await backend.start()

    select_port.assert_awaited_once_with(API_PORT_RANGE_START, API_PORT_RANGE_END, host="127.0.0.1")
    assert backend._client is not None
    assert backend._client.base_url == "http://127.0.0.1:38801"


async def test_daemon_runner_reselects_api_port_when_taken(tmp_path: Path) -> None:
    """An API port taken while the daemon was down is replaced before (re)starting."""
    backend = object.__new__(GoLibrespotBackend)
    backend.mass = MagicMock()
    backend.mass.streams.get_source_ip = AsyncMock(return_value="192.168.1.5")
    backend.logger = MagicMock()
    backend.name = "Spotify Test"
    backend.cache_dir = str(tmp_path)
    backend._binary = "/usr/bin/go-librespot"
    backend._api_port = 38800
    backend._client = GoLibrespotClient(backend.mass, "http://127.0.0.1:38800", backend.logger)
    backend._event_callback = AsyncMock()
    # exit the supervisor loop after a single iteration
    backend._stop_called = True
    backend._restart_error_count = 0

    async def _no_stderr() -> AsyncGenerator[str]:
        return
        yield

    proc = MagicMock()
    proc.start = AsyncMock()
    proc.close = AsyncMock()
    proc.iter_stderr = _no_stderr

    with (
        patch(
            "music_assistant.providers.spotify_connect.backends.go_librespot.is_port_in_use",
            new=AsyncMock(return_value=True),
        ) as port_probe,
        patch(
            "music_assistant.providers.spotify_connect.backends.go_librespot.select_free_port",
            new=AsyncMock(return_value=38801),
        ),
        patch(
            "music_assistant.providers.spotify_connect.backends.go_librespot.AsyncProcess",
            return_value=proc,
        ),
        patch.object(GoLibrespotBackend, "_write_config") as write_config,
    ):
        await backend._daemon_runner()

    port_probe.assert_awaited_once_with(38800, host="127.0.0.1")
    assert backend._api_port == 38801
    assert backend._client.base_url == "http://127.0.0.1:38801"
    # the daemon config pins the advertisement to the player-facing interface
    write_config.assert_called_once_with("192.168.1.5")


def _volume_sync_provider(volume_level: int | None) -> tuple[SpotifyConnectProvider, AsyncMock]:
    """Build a minimal provider whose linked player reports the given volume."""
    provider = object.__new__(SpotifyConnectProvider)
    provider.mass = MagicMock()
    provider.logger = MagicMock()
    provider._last_volume_sent = None
    backend = MagicMock()
    set_volume = AsyncMock()
    backend.set_volume = set_volume
    provider._backend = backend
    player = MagicMock()
    player.state.volume_level = volume_level
    provider.mass.players.get_player.return_value = player
    return provider, set_volume


async def test_sync_player_volume_pushes_player_volume_to_backend() -> None:
    """The player's volume is pushed to the backend and cached for echo dedupe."""
    provider, set_volume = _volume_sync_provider(50)

    await provider._sync_player_volume_to_spotify("player1")

    set_volume.assert_awaited_once_with(50)
    assert provider._last_volume_sent == 50


async def test_sync_player_volume_pushes_when_cache_matches() -> None:
    """The push is unconditional: the backend's volume resets between sessions."""
    provider, set_volume = _volume_sync_provider(50)
    provider._last_volume_sent = 50

    await provider._sync_player_volume_to_spotify("player1")

    set_volume.assert_awaited_once_with(50)


async def test_sync_player_volume_skips_when_volume_unknown() -> None:
    """No push happens when the player does not expose a volume level."""
    provider, set_volume = _volume_sync_provider(None)

    await provider._sync_player_volume_to_spotify("player1")

    set_volume.assert_not_awaited()
    assert provider._last_volume_sent is None


async def test_sync_player_volume_restores_cache_on_failure() -> None:
    """A failed push restores the dedupe cache so a retry is not wrongly deduped."""
    provider, set_volume = _volume_sync_provider(50)
    set_volume.side_effect = OSError("daemon gone")

    await provider._sync_player_volume_to_spotify("player1")

    assert provider._last_volume_sent is None


def _provider_with_stored_config(
    setup_data: dict[str, Any], tmp_path: Path
) -> SpotifyConnectProvider:
    """Build a provider whose stored setup_data resolves through the real accessors."""
    provider = object.__new__(SpotifyConnectProvider)
    provider.mass = MagicMock()
    provider.mass.storage_path = str(tmp_path / "storage")
    provider.mass.cache_path = str(tmp_path / "cache")
    provider.mass.config.get.return_value = setup_data
    provider.mass.config.decrypt_string.side_effect = lambda value: value
    provider.logger = MagicMock()
    provider._publish_name = "Test Speaker"
    provider.config = ProviderConfig(
        values={},
        type=ProviderType.PLUGIN,
        domain="spotify_connect",
        instance_id="spotify_connect--test",
        name="Spotify Connect",
    )
    return provider


def test_config_without_backend_choice_loads_go_librespot(tmp_path: Path) -> None:
    """A config from before the backend choice existed loads go-librespot unchanged."""
    provider = _provider_with_stored_config({}, tmp_path)

    assert isinstance(provider._create_backend(), GoLibrespotBackend)


def test_soloist_setup_data_loads_soloist_backend(tmp_path: Path) -> None:
    """A flow-configured soloist instance loads the soloist backend with its stored values."""
    provider = _provider_with_stored_config(
        {
            CONF_BACKEND: BACKEND_SOLOIST,
            CONF_API_KEY: "soloist-api-key-0123456789abcdef",
            CONF_SOLOIST_CONSENT: True,
            CONF_VOLUME_MODE: VOLUME_MODE_SYNC_SPOTIFY,
        },
        tmp_path,
    )

    backend = provider._create_backend()

    assert isinstance(backend, SoloistBackend)
    assert backend._api_key == "soloist-api-key-0123456789abcdef"
    assert backend._consent is True
    assert backend._volume_mode == VOLUME_MODE_SYNC_SPOTIFY
