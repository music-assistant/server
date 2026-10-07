"""Shared fixtures for Soundcloud provider tests."""

from __future__ import annotations

from collections.abc import Iterator
from unittest.mock import AsyncMock, Mock

import pytest

from music_assistant.models.music_provider import SYNC_RUN_STATE, SyncRunState
from music_assistant.providers.soundcloud import SUPPORTED_FEATURES, SoundcloudMusicProvider


@pytest.fixture
def sync_run() -> Iterator[SyncRunState]:
    """Run the library listings as part of a sync run, so their skips are recorded."""
    state = SyncRunState()
    token = SYNC_RUN_STATE.set(state)
    yield state
    SYNC_RUN_STATE.reset(token)


@pytest.fixture
def provider() -> SoundcloudMusicProvider:
    """Create a real SoundcloudMusicProvider with mocked dependencies."""
    mass = Mock()
    manifest = Mock()
    manifest.domain = "soundcloud"
    config = Mock()
    config.instance_id = "soundcloud--test123"
    config.name = "Soundcloud Test"
    config.enabled = True
    config.get_value.side_effect = lambda key, default=None: {
        "log_level": "GLOBAL",
    }.get(key, default)
    prov = SoundcloudMusicProvider(mass, manifest, config, SUPPORTED_FEATURES)
    # stand in for the api client that handle_async_init would otherwise create
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
