"""
Tests for ChromecastPlayer dashboard keepalive media-status handling.

The cast receiver app plays a keepalive media item (currently a paused
``keepalive.png``, soon a looping ``dashboard-keepalive.mp4``) while showing a
dashboard. That media is an implementation detail of the receiver and must
never leak into MA player state - otherwise a Nest Hub showing a dashboard
appears in MA as "playing dashboard-keepalive.mp4".
"""

from __future__ import annotations

from typing import cast
from unittest.mock import MagicMock

from music_assistant_models.enums import PlaybackState

from music_assistant.providers.chromecast.player import ChromecastPlayer


def _handle_media_status(fake: MagicMock, status: MagicMock) -> None:
    ChromecastPlayer._handle_media_status(cast("ChromecastPlayer", fake), status)


def _fake_player() -> MagicMock:
    """Build a MagicMock standing in for a ChromecastPlayer with no active cast group."""
    fake = MagicMock()
    fake.active_cast_group = None
    return fake


def _media_status(content_id: str, *, player_is_playing: bool, player_is_paused: bool) -> MagicMock:
    status = MagicMock()
    status.content_id = content_id
    status.player_is_playing = player_is_playing
    status.player_is_paused = player_is_paused
    return status


def test_dashboard_keepalive_video_treated_as_idle() -> None:
    """A playing dashboard-keepalive.mp4 status must not surface as MA playback."""
    fake = _fake_player()
    fake._attr_elapsed_time = 123.0
    status = _media_status(
        "https://cast.music-assistant.io/dashboard-keepalive.mp4",
        player_is_playing=True,
        player_is_paused=False,
    )

    _handle_media_status(fake, status)

    assert fake._attr_playback_state == PlaybackState.IDLE
    assert fake._attr_current_media is None
    assert fake._attr_active_source is None
    assert fake._attr_elapsed_time == 0
    assert isinstance(fake._attr_elapsed_time_last_updated, float)
    fake.update_state.assert_called_once()


def test_legacy_keepalive_image_treated_as_idle() -> None:
    """The legacy paused keepalive.png status must also not surface as MA playback."""
    fake = _fake_player()
    fake._attr_elapsed_time = 123.0
    status = _media_status(
        "https://cast.music-assistant.io/keepalive.png",
        player_is_playing=False,
        player_is_paused=True,
    )

    _handle_media_status(fake, status)

    assert fake._attr_playback_state == PlaybackState.IDLE
    assert fake._attr_current_media is None
    assert fake._attr_active_source is None
    assert fake._attr_elapsed_time == 0
    assert isinstance(fake._attr_elapsed_time_last_updated, float)
    fake.update_state.assert_called_once()
