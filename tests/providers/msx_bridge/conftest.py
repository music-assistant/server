"""Fixtures for testing the MSX Bridge Provider."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any
from unittest.mock import Mock

import pytest
from aiohttp.test_utils import TestClient, TestServer
from music_assistant_models.enums import PlayerType

from music_assistant.providers.msx_bridge.http_server import MSXHTTPServer
from music_assistant.providers.msx_bridge.party import render_qr
from music_assistant.providers.msx_bridge.player import MSXPlayer
from music_assistant.providers.msx_bridge.provider import MSXBridgeProvider
from tests.providers.msx_bridge.fakes import FakeMass


@pytest.fixture(autouse=True)
def _reset_render_qr_cache() -> None:
    """Keep the memoized QR renderer isolated between tests."""
    render_qr.cache_clear()


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
    """Return a fake MusicAssistant with only provider-used controller seams."""
    return FakeMass(player_config_mock)  # type: ignore[return-value]


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
