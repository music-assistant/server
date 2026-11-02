"""Fixtures for the Home Assistant MediaPlayers provider tests."""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.enums import PlaybackState

from music_assistant.providers.hass_players.player import HomeAssistantPlayer

BASE_URL = "http://10.0.0.5:8097"
PLAYER_ID = "media_player.bedroom"


@pytest.fixture
def player() -> HomeAssistantPlayer:
    """Return a HomeAssistantPlayer with mocked dependencies."""
    player = HomeAssistantPlayer.__new__(HomeAssistantPlayer)
    mass = MagicMock()
    mass.closing = False
    mass.streams.base_url = BASE_URL
    mass.streams.resolve_stream_url = AsyncMock(return_value=f"{BASE_URL}/flow/s1/q1/i1/x.mp3")
    player.mass = mass
    player.logger = logging.getLogger("test.hass_players.player")
    player._player_id = PLAYER_ID
    player._provider = MagicMock()
    player._provider.mass = mass
    player._extra_data = {}
    player.hass = MagicMock()
    player.hass.call_service = AsyncMock()
    player._hass_attributes = {}
    player._attr_supported_features = set()
    player._attr_source_list = []
    player._attr_group_members = []
    player._attr_playback_state = PlaybackState.IDLE
    player._attr_active_source = None
    player._attr_current_media = None
    player._ma_playback_active = False
    player._ma_playback_started = False
    player._reports_stream_url = False
    player.update_state = MagicMock()  # type: ignore[misc, method-assign]
    return player
