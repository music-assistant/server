"""Shared fixtures for the Soundcloud provider tests."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from music_assistant.providers.soundcloud import SUPPORTED_FEATURES, SoundcloudMusicProvider


@pytest.fixture
def provider() -> SoundcloudMusicProvider:
    """Return a Soundcloud provider instance with a mocked API client."""
    mass = AsyncMock()
    mass.http_session = MagicMock()
    manifest = MagicMock()
    manifest.domain = "soundcloud"
    config = MagicMock()
    config.instance_id = "soundcloud--test"
    config.get_value.return_value = "GLOBAL"
    prov = SoundcloudMusicProvider(mass, manifest, config, SUPPORTED_FEATURES)
    prov._soundcloud = AsyncMock()
    prov._soundcloud.client_id = "test-client-id"
    prov._soundcloud.headers = {"Authorization": "OAuth test-token"}
    prov._soundcloud.get_user_details.return_value = {
        "id": 1,
        "username": "Some Artist",
        "permalink": "some-artist",
    }
    prov._user_id = "1"
    return prov
