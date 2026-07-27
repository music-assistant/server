"""Tests for the HEOS player."""

from __future__ import annotations

from unittest.mock import MagicMock

from music_assistant_models.enums import MediaType
from pyheos import PlayState as HeosPlayState
from pyheos import const as heos_const

from music_assistant.models.player import PlayerMedia
from music_assistant.providers.heos.player import HeosPlayer


def _url_stream_now_playing() -> MagicMock:
    """HEOS now-playing for a generic URL stream it cannot parse (MA's own source)."""
    now_playing = MagicMock()
    now_playing.source_id = heos_const.MUSIC_SOURCE_LOCAL_MUSIC
    now_playing.type = "song"
    now_playing.song = "Url Stream"
    now_playing.album = "Url Stream"
    now_playing.artist = "Url Stream"
    now_playing.image_url = ""
    now_playing.media_id = "1"
    now_playing.album_id = "1"
    now_playing.current_position = None
    now_playing.current_position_updated = None
    now_playing.duration = None
    return now_playing


def _make_player(now_playing: MagicMock) -> HeosPlayer:
    """Build a HeosPlayer backed by a mocked device/controller."""
    provider = MagicMock()
    provider._heos_queue = MagicMock()
    device = MagicMock()
    device.player_id = "1"
    device.name = "Kitchen"
    device.heos = MagicMock()
    device.state = HeosPlayState.PLAY
    device.now_playing_media = now_playing
    return HeosPlayer(provider, device)


def test_url_stream_now_playing_preserves_ma_media_while_ma_controls() -> None:
    """
    Preserve MA's current_media against a HEOS "Url Stream" report.

    HEOS cannot parse metadata from the generic URL stream MA serves, so it
    reports ``Url Stream``. When MA controls playback that report must be
    ignored (even if ``active_source`` is momentarily stale from the
    ``play_url`` race) so MA's own, correct metadata is preserved.

    See https://github.com/music-assistant/support/issues/5614
    """
    player = _make_player(_url_stream_now_playing())
    correct_media = PlayerMedia(
        uri="http://ma/stream/foo",
        media_type=MediaType.TRACK,
        title="Real Track",
        artist="Real Artist",
        album="Real Album",
    )
    player._attr_current_media = correct_media
    player._ma_controls_playback = True
    # Stale: a previous external source is still the recorded active source.
    player._attr_active_source = "external_radio"

    player._update_player_current_media()

    # MA's correct media is preserved; the bogus "Url Stream" did not clobber it.
    assert player._attr_current_media is correct_media
    assert player._attr_current_media.title == "Real Track"
    # Playback control was not latched away to the phantom external source.
    assert player._ma_controls_playback is True


def test_external_source_now_playing_still_updates_media() -> None:
    """A genuinely external (non-local) source must still update current_media."""
    now_playing = _url_stream_now_playing()
    now_playing.source_id = 9999  # an external source, not MA's local stream
    now_playing.song = "External Track"
    now_playing.artist = "External Artist"
    player = _make_player(now_playing)
    player._attr_current_media = PlayerMedia(
        uri="http://ma/stream/foo", media_type=MediaType.TRACK, title="MA Track"
    )
    player._ma_controls_playback = True
    player._attr_active_source = player.player_id

    player._update_player_current_media()

    assert player._attr_current_media.title == "External Track"
