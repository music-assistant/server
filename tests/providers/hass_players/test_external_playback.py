"""Tests for telling Music Assistant playback apart from external playback."""

from __future__ import annotations

import logging
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from music_assistant_models.enums import PlaybackState
from music_assistant_models.player import PlayerMedia

from music_assistant.constants import ATTR_ANNOUNCEMENT_IN_PROGRESS
from music_assistant.providers.hass_players.player import HomeAssistantPlayer

BASE_URL = "http://10.0.0.5:8097"
PLAYER_ID = "media_player.bedroom"


def _make_player() -> HomeAssistantPlayer:
    """Create a HomeAssistantPlayer with mocked dependencies."""
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


def _external_attributes(content_id: str = "spotify:track:42") -> dict[str, Any]:
    """Return HA state attributes for content MA did not hand the entity."""
    return {
        "media_content_id": content_id,
        "media_title": "Some Song",
        "media_artist": "Some Artist",
    }


async def test_ma_playback_detected_when_entity_hides_the_stream_url() -> None:
    """An entity that reports its own metadata instead of our URL is still MA playback."""
    player = _make_player()
    media = PlayerMedia(uri="library://track/1", title="MA Track")
    player._ma_playback_active = True
    player._attr_current_media = media
    player._attr_playback_state = PlaybackState.PLAYING

    player._update_attributes(_external_attributes())

    assert player._attr_active_source is None
    # the queue controller provides the media, so the entity may not overwrite it
    assert player._attr_current_media is media


async def test_external_playback_detected_without_ma_session() -> None:
    """Playback that MA did not start is reported as an external source."""
    player = _make_player()
    player._attr_playback_state = PlaybackState.PLAYING

    player._update_attributes(_external_attributes())

    assert player._attr_active_source == "External"
    assert player._attr_current_media is not None
    assert player._attr_current_media.title == "Some Song"


async def test_entity_echoing_stream_url_still_detects_takeover() -> None:
    """An entity that reports our stream URL is judged by it, also mid-session."""
    player = _make_player()
    player._ma_playback_active = True
    player._attr_playback_state = PlaybackState.PLAYING

    player._update_attributes(
        {"media_content_id": f"{BASE_URL}/flow/s1/q1/i1/x.mp3", "media_title": "MA Track"}
    )
    assert player._attr_active_source is None
    assert player._reports_stream_url is True

    # another app takes the speaker over while MA still considers its session open
    player._update_attributes(_external_attributes())

    assert player._attr_active_source == "External"


async def test_idle_entity_ends_the_ma_session() -> None:
    """Playback that starts after the entity went idle is external again."""
    player = _make_player()

    await player.play_media(PlayerMedia(uri="library://track/1", title="MA Track"))
    player.update_from_compressed_state({"s": "playing"})
    player.update_from_compressed_state({"s": "idle"})
    assert player._ma_playback_active is False

    player.update_from_compressed_state({"s": "playing", "a": _external_attributes()})

    assert player._attr_active_source == "External"


async def test_play_media_starts_the_ma_session() -> None:
    """A play command from MA marks the session as ours."""
    player = _make_player()

    await player.play_media(PlayerMedia(uri="library://track/1", title="MA Track"))

    assert player._ma_playback_active is True


async def test_late_idle_of_previous_session_is_ignored() -> None:
    """An entity reporting the stop of the previous track late keeps our session ours."""
    player = _make_player()
    player._ma_playback_active = True
    player._ma_playback_started = True
    player._attr_playback_state = PlaybackState.PLAYING

    # switching tracks stops the entity first, so its idle can arrive after our play
    await player.play_media(PlayerMedia(uri="library://track/2", title="Next Track"))
    player.update_from_compressed_state({"s": "idle"})
    player.update_from_compressed_state({"s": "playing", "a": _external_attributes()})

    assert player._ma_playback_active is True
    assert player._attr_active_source is None


async def test_power_on_state_does_not_end_the_ma_session() -> None:
    """An entity that reports powering on before it plays keeps our session ours."""
    player = _make_player()

    await player.play_media(PlayerMedia(uri="library://track/1", title="MA Track"))
    player.update_from_compressed_state({"s": "on"})
    player.update_from_compressed_state({"s": "playing", "a": _external_attributes()})

    assert player._attr_active_source is None


async def test_announcement_does_not_disturb_source_detection() -> None:
    """An announcement neither ends our session nor proves the entity reports our URL."""
    player = _make_player()
    player._attr_playback_state = PlaybackState.PLAYING
    player._update_attributes(_external_attributes())
    assert player._attr_active_source == "External"

    player.extra_data[ATTR_ANNOUNCEMENT_IN_PROGRESS] = True
    player.update_from_compressed_state(
        {
            "s": "playing",
            "a": {"media_content_id": f"{BASE_URL}/announcement/{PLAYER_ID}.mp3"},
        }
    )
    player.update_from_compressed_state({"s": "idle"})

    assert player._reports_stream_url is False
    assert player._attr_active_source == "External"


async def test_stop_ends_the_ma_session() -> None:
    """A stop command from MA releases the session."""
    player = _make_player()
    player._ma_playback_active = True

    await player.stop()

    assert player._ma_playback_active is False
