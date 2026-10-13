"""Tests for the WebKioskPlayer state machine."""

from __future__ import annotations

import time
from unittest.mock import Mock

from music_assistant_models.enums import PlaybackState
from music_assistant_models.player import PlayerMedia

from music_assistant.providers.web_kiosk.player import WebKioskPlayer, media_timeline


def _media() -> Mock:
    """Return a minimal PlayerMedia stand-in."""
    media = Mock(spec=PlayerMedia)
    media.uri = "spotify://track/1"
    media.title = "Track"
    media.artist = "Artist"
    media.image_url = None
    media.stream_duration = 120
    media.duration = 120
    return media


async def test_play_media_sets_playing_and_media(player: WebKioskPlayer) -> None:
    """play_media stores the media and moves the player to PLAYING."""
    media = _media()
    await player.play_media(media)

    assert player.playback_state == PlaybackState.PLAYING
    assert player.current_media is media
    assert player.current_stream_url == media.uri


async def test_play_media_notifies_kiosk(player: WebKioskPlayer) -> None:
    """play_media pushes a 'play' broadcast through the HTTP server."""
    player.provider.http_server = Mock()  # type: ignore[attr-defined]
    await player.play_media(_media())

    player.provider.http_server.broadcast_play.assert_called_once()  # type: ignore[attr-defined]


def test_media_timeline_uses_song_length_after_seek() -> None:
    """A shortened stream still belongs to the full song."""
    media = Mock()
    media.duration = 208
    media.stream_duration = 62

    assert media_timeline(media) == (208.0, 146.0)


def test_media_timeline_without_stream_duration() -> None:
    """Missing stream length means the audio starts at the beginning."""
    media = Mock()
    media.duration = 208
    media.stream_duration = None

    assert media_timeline(media) == (208.0, 0.0)


def test_media_timeline_stream_only() -> None:
    """A stream length alone is the only clock the screen can draw."""
    media = Mock()
    media.duration = None
    media.stream_duration = 10

    assert media_timeline(media) == (10.0, 0.0)


def test_media_timeline_equal_durations_have_no_offset() -> None:
    """Equal lengths mean the served audio is the whole song."""
    media = Mock()
    media.duration = 120
    media.stream_duration = 120

    assert media_timeline(media) == (120.0, 0.0)


async def test_play_media_reports_song_length_and_stream_start(player: WebKioskPlayer) -> None:
    """After a seek, the kiosk is told the song length and where the file starts."""
    media = _media()
    media.duration = 208
    media.stream_duration = 62
    player.provider.http_server = Mock()  # type: ignore[attr-defined]

    await player.play_media(media)

    kwargs = player.provider.http_server.broadcast_play.call_args.kwargs  # type: ignore[attr-defined]
    assert kwargs["duration"] == 208.0
    assert kwargs["start"] == 146.0


async def test_pause_snapshots_elapsed_time(player: WebKioskPlayer) -> None:
    """pause() accumulates elapsed time and moves to PAUSED."""
    player._attr_playback_state = PlaybackState.PLAYING
    player._attr_elapsed_time = 10.0
    player._attr_elapsed_time_last_updated = time.time() - 5.0

    await player.pause()

    assert player.playback_state == PlaybackState.PAUSED
    assert player._attr_elapsed_time is not None
    assert player._attr_elapsed_time >= 15.0


async def test_stop_clears_media_and_stream(player: WebKioskPlayer) -> None:
    """stop() returns the player to IDLE and clears current media."""
    await player.play_media(_media())
    await player.stop()

    assert player.playback_state == PlaybackState.IDLE
    assert player.current_media is None
    assert player.current_stream_url is None


async def test_seek_updates_elapsed(player: WebKioskPlayer) -> None:
    """seek() moves elapsed time and notifies the kiosk."""
    player._attr_playback_state = PlaybackState.PLAYING
    player.provider.http_server = Mock()  # type: ignore[attr-defined]

    await player.seek(30)

    assert player.elapsed_time == 30.0
    player.provider.http_server.broadcast_seek.assert_called_once_with(  # type: ignore[attr-defined]
        "wk_test", 30
    )


def test_playback_reached_end_includes_seek_origin(player: WebKioskPlayer) -> None:
    """A short remainder after a seek still counts as the end of the song."""
    player._attr_current_media = Mock(duration=30, stream_duration=4)
    player._attr_elapsed_time = 3

    assert player.playback_reached_end() is True


def test_playback_reached_end_is_false_early_in_the_song(player: WebKioskPlayer) -> None:
    """The first seconds of a track are not the end."""
    player._attr_current_media = Mock(duration=180, stream_duration=None)
    player._attr_elapsed_time = 2

    assert player.playback_reached_end() is False


def test_update_position_ignored_while_paused(player: WebKioskPlayer) -> None:
    """Position reports are dropped unless the player is PLAYING."""
    player._attr_playback_state = PlaybackState.PAUSED
    player._attr_elapsed_time = 5.0

    player.update_position(99.0)

    assert player.elapsed_time == 5.0
