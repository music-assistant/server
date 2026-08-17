"""
Tests for ChromecastPlayer dashboard keepalive media-status handling.

The cast receiver app plays a keepalive media item (currently a paused
``keepalive.png``, soon a looping ``dashboard-keepalive.mp4``) while showing a
dashboard. That media is an implementation detail of the receiver and must
never leak into MA player state - otherwise a Nest Hub showing a dashboard
appears in MA as "playing dashboard-keepalive.mp4".
"""

from __future__ import annotations

from typing import Any, cast
from unittest.mock import MagicMock

from music_assistant_models.enums import PlaybackState
from music_assistant_models.player import PlayerMedia

from music_assistant.providers.chromecast.player import ChromecastPlayer


def _fake_player() -> Any:
    """Build a Cast player with no active cast group, on mocked collaborators."""
    # __init__ is skipped: it needs a provider, cast info and a live Chromecast connection.
    # Typed as Any because the collaborators below are read back as the mocks they are.
    fake = cast("Any", ChromecastPlayer.__new__(ChromecastPlayer))
    fake.logger = MagicMock()
    fake.update_state = MagicMock()
    fake.active_cast_group = None
    # display_name is a cached property, normally built from the config in __init__
    fake._cache = {"display_name": "Test Cast"}
    # state left over from what played before the dashboard took over, so the assertions
    # prove the handler clears it rather than matching an untouched default
    fake._attr_playback_state = PlaybackState.PLAYING
    fake._attr_current_media = PlayerMedia(uri="http://mass/previous.flac")
    fake._attr_active_source = "previous_source"
    fake._attr_elapsed_time = 123.0
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
    status = _media_status(
        "https://cast.music-assistant.io/dashboard-keepalive.mp4",
        player_is_playing=True,
        player_is_paused=False,
    )

    fake._handle_media_status(status)

    assert fake._attr_playback_state == PlaybackState.IDLE
    assert fake._attr_current_media is None
    assert fake._attr_active_source is None
    assert fake._attr_elapsed_time == 0
    assert isinstance(fake._attr_elapsed_time_last_updated, float)
    fake.update_state.assert_called_once()


def test_legacy_keepalive_image_treated_as_idle() -> None:
    """The legacy paused keepalive.png status must also not surface as MA playback."""
    fake = _fake_player()
    status = _media_status(
        "https://cast.music-assistant.io/keepalive.png",
        player_is_playing=False,
        player_is_paused=True,
    )

    fake._handle_media_status(status)

    assert fake._attr_playback_state == PlaybackState.IDLE
    assert fake._attr_current_media is None
    assert fake._attr_active_source is None
    assert fake._attr_elapsed_time == 0
    assert isinstance(fake._attr_elapsed_time_last_updated, float)
    fake.update_state.assert_called_once()
