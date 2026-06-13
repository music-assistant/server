# mypy: disable-error-code="attr-defined,method-assign,misc,assignment,unused-ignore"
"""Tests for the SpotifyConnectProvider."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from music_assistant.providers.spotify_connect import (
    AUDIO_SOURCE_ID,
    CONF_MASS_PLAYER_ID,
    CONF_PUBLISH_NAME,
    PLAYER_ID_AUTO,
    SpotifyConnectProvider,
)


def _make_mock_config(values: dict[str, Any] | None = None) -> MagicMock:
    """Create a mock ProviderConfig."""
    defaults: dict[str, Any] = {
        CONF_MASS_PLAYER_ID: PLAYER_ID_AUTO,
        CONF_PUBLISH_NAME: "Spotify Connect",
        "log_level": "GLOBAL",
    }
    if values:
        defaults.update(values)
    config = MagicMock()
    config.get_value.side_effect = defaults.get
    config.instance_id = "spotify_connect_test"
    config.name = "Spotify Connect"
    return config


def _make_mock_manifest() -> MagicMock:
    """Create a mock ProviderManifest."""
    manifest = MagicMock()
    manifest.domain = "spotify_connect"
    manifest.name = "Spotify Connect"
    return manifest


def _make_provider() -> SpotifyConnectProvider:
    """Create a SpotifyConnectProvider with mock dependencies."""
    mass = MagicMock()
    mass.cache_path = "/var/cache/test-cache"
    return SpotifyConnectProvider(mass, _make_mock_manifest(), _make_mock_config())


async def test_on_volume_change_without_web_api_is_noop() -> None:
    """Without a Spotify music provider, a volume change must not raise (regression #5627)."""
    provider = _make_provider()
    provider._spotify_provider = None
    provider._librespot_playing = True
    provider._on_volume = AsyncMock()

    # must not raise (regression for support issue #5627)
    await provider.on_volume_change(AUDIO_SOURCE_ID, 50)

    provider._on_volume.assert_not_called()


async def test_on_volume_change_with_web_api_syncs_volume() -> None:
    """With a Spotify music provider available the new volume is synced upstream."""
    provider = _make_provider()
    provider._spotify_provider = MagicMock()
    provider._librespot_playing = True
    provider._on_volume = AsyncMock()

    await provider.on_volume_change(AUDIO_SOURCE_ID, 42)

    provider._on_volume.assert_awaited_once_with(42)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
