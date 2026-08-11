"""Fixtures for testing the MSX Bridge Provider."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from pathlib import Path
from pkgutil import extend_path
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
from aiohttp.test_utils import TestClient, TestServer
from music_assistant_models.enums import PlayerType

import music_assistant
import music_assistant.providers

# The upstream workflow runs these tests with ma-server as pytest's rootdir, so
# the repository-level conftest is not loaded. Keep the namespace bridge here,
# before importing the provider, to expose sibling MA providers used by stream
# controller imports.
music_assistant.__path__ = extend_path(music_assistant.__path__, music_assistant.__name__)
_repo_root = Path(__file__).resolve().parents[1]
for _ma_candidate in (
    Path.cwd() / "music_assistant",
    _repo_root / "ma-server" / "music_assistant",
):
    _ma_candidate_str = str(_ma_candidate)
    if _ma_candidate.is_dir() and _ma_candidate_str not in music_assistant.__path__:
        music_assistant.__path__.append(_ma_candidate_str)

music_assistant.providers.__path__ = extend_path(
    music_assistant.providers.__path__, music_assistant.providers.__name__
)
for _ma_package_root in music_assistant.__path__:
    _providers_root = str(Path(_ma_package_root) / "providers")
    if Path(_providers_root).is_dir() and _providers_root not in music_assistant.providers.__path__:
        music_assistant.providers.__path__.append(_providers_root)

from music_assistant.providers.msx_bridge.http_server import (  # noqa: E402
    MSXHTTPServer,
    _render_qr,
)
from music_assistant.providers.msx_bridge.player import MSXPlayer  # noqa: E402
from music_assistant.providers.msx_bridge.provider import MSXBridgeProvider  # noqa: E402


async def _empty_async_gen() -> AsyncGenerator[Any]:
    """Empty async generator for mocking AsyncGenerator return types."""
    return
    yield  # type: ignore[unreachable]  # pragma: no cover — makes it a generator


@pytest.fixture(autouse=True)
def _reset_render_qr_cache() -> None:
    """Keep the memoized QR renderer isolated between tests."""
    _render_qr.cache_clear()


@pytest.fixture
def player_config_mock() -> Mock:
    """Return a mock PlayerConfig as returned by get_base_player_config()."""
    player_config = Mock()
    player_config.name = None
    player_config.default_name = None
    player_config.enabled = True
    player_config.player_type = PlayerType.PLAYER
    player_config.get_value = Mock(return_value=None)
    return player_config


@pytest.fixture
def mass_mock(player_config_mock: Mock) -> Mock:
    """Return a mock MusicAssistant instance."""
    mass = Mock()
    mass.http_session = AsyncMock()
    mass.cache = Mock()
    mass.cache.get = AsyncMock(return_value=None)
    mass.cache.set = AsyncMock()

    # Player.__init__ deps
    mass.config.create_default_player_config = Mock()
    mass.config.get_base_player_config = Mock(return_value=player_config_mock)
    mass.config.get_raw_player_config_value = Mock(return_value="stereo")
    mass.config.get_player_dsp_config = Mock()
    mass.config.get = Mock(return_value={})
    mass.verify_event_loop_thread = Mock()

    # Library API
    mass.music.albums.library_items = AsyncMock(return_value=[])
    mass.music.albums.tracks = AsyncMock(return_value=[])
    mass.music.artists.library_items = AsyncMock(return_value=[])
    mass.music.artists.albums = AsyncMock(return_value=[])
    mass.music.playlists.library_items = AsyncMock(return_value=[])
    mass.music.playlists.tracks = Mock(side_effect=lambda *_args, **_kwargs: _empty_async_gen())
    mass.music.tracks.library_items = AsyncMock(return_value=[])
    mass.music.search = AsyncMock(return_value=Mock(artists=[], albums=[], tracks=[], playlists=[]))

    # Track metadata resolution
    mass.music.get_item_by_uri = AsyncMock(return_value=None)

    # Playback control
    mass.player_queues.play_media = AsyncMock()
    mass.player_queues.resume = AsyncMock()
    mass.player_queues.items = Mock(return_value=[])
    mass.player_queues.get = Mock(return_value=None)
    mass.players.cmd_pause = AsyncMock()
    mass.players.cmd_play = AsyncMock()
    mass.players.cmd_stop = AsyncMock()
    mass.players.cmd_next_track = AsyncMock()
    mass.players.cmd_previous_track = AsyncMock()
    mass.players.get = Mock(return_value=None)
    mass.players.get_player = Mock(return_value=None)
    mass.players.register = AsyncMock()
    mass.players.unregister = AsyncMock()
    mass.players.all = Mock(return_value=[])
    mass.players.all_players = Mock(return_value=[])
    mass.players.iter_players = Mock(return_value=[])

    # Image URLs
    mass.metadata.get_image_url = Mock(return_value=None)

    # Other providers (e.g. the Party plugin) are absent by default
    mass.get_provider = Mock(return_value=None)

    return mass


@pytest.fixture
def manifest_mock() -> Mock:
    """Return a mock provider manifest."""
    manifest = Mock()
    manifest.domain = "msx_bridge"
    manifest.name = "MSX Bridge"
    manifest.type = Mock()
    manifest.stage = Mock()
    return manifest


@pytest.fixture
def config_mock() -> Mock:
    """Return a mock provider config."""
    config = Mock()
    config.name = "MSX Bridge"
    config.instance_id = "msx_bridge_test"
    config.enabled = True
    config.get_value = Mock(
        side_effect=lambda key, default=None: {
            "http_port": 8099,
            "output_format": "mp3",
            "log_level": "GLOBAL",
            "enable_player_grouping": True,
        }.get(key, default)
    )
    return config


@pytest.fixture
def provider(mass_mock: Mock, manifest_mock: Mock, config_mock: Mock) -> MSXBridgeProvider:
    """Return an MSXBridgeProvider instance without a real HTTP server."""
    prov = MSXBridgeProvider(mass_mock, manifest_mock, config_mock, set())
    prov.http_server = None
    return prov


@pytest.fixture
def player(provider: MSXBridgeProvider) -> MSXPlayer:
    """Return an MSXPlayer with update_state mocked."""
    p = MSXPlayer(provider, "msx_test", name="Test TV", output_format="mp3")
    p.update_state = Mock()  # type: ignore[misc,method-assign]
    return p


@pytest.fixture
async def http_client(
    provider: MSXBridgeProvider,
) -> AsyncGenerator[TestClient[Any, Any]]:
    """Return an aiohttp TestClient for the MSX HTTP server."""
    server = MSXHTTPServer(provider, 0)
    client = TestClient(TestServer(server.app))
    await client.start_server()
    yield client
    await client.close()
