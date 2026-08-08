"""Tests for the HEOS player."""

from __future__ import annotations

from datetime import UTC, datetime
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


def _external_now_playing(current_position: int | None, duration: int | None = 245000) -> MagicMock:
    """HEOS now-playing for an external source, with millisecond progress values."""
    now_playing = _url_stream_now_playing()
    now_playing.source_id = 9999
    now_playing.song = "External Track"
    now_playing.current_position = current_position
    now_playing.current_position_updated = datetime(2026, 1, 1, tzinfo=UTC)
    now_playing.duration = duration
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


def test_external_source_is_ignored_during_ma_playback_transition() -> None:
    """Suppress external-source media updates while MA playback is starting."""
    player = _make_player(_external_now_playing(None))
    player._ma_playback_starting = True
    player._ma_controls_playback = True
    current_media = PlayerMedia(
        uri="http://ma/stream/foo", media_type=MediaType.TRACK, title="MA Track"
    )
    player._attr_current_media = current_media

    player._update_player_current_media()

    assert player._ma_playback_starting is True
    assert player._ma_controls_playback is True
    assert player._attr_current_media.title == "MA Track"

    player._finish_ma_playback_transition()
    player._update_player_current_media()

    assert player._attr_current_media.title == "External Track"
    assert player._ma_controls_playback is False


def test_media_position_and_duration_reported_in_seconds() -> None:
    """HEOS reports milliseconds; the media must carry seconds."""
    player = _make_player(_external_now_playing(113000))
    player._attr_active_source = "9999"

    player._update_player_current_media()

    assert player._attr_current_media is not None
    assert player._attr_current_media.elapsed_time == 113
    assert player._attr_current_media.duration == 245


def test_media_and_player_position_agree() -> None:
    """The media-level and player-level positions must use the same unit."""
    player = _make_player(_external_now_playing(113000))
    player._attr_active_source = "9999"

    player.set_dynamic_attributes(update_media=True)

    assert player._attr_current_media is not None
    assert player._attr_current_media.elapsed_time == player._attr_elapsed_time == 113


def test_position_zero_is_reported_not_dropped() -> None:
    """Position 0 is a real position at the start of a track, not a missing one."""
    player = _make_player(_external_now_playing(0))
    player._attr_active_source = "9999"

    player.set_dynamic_attributes(update_media=True)

    assert player._attr_current_media is not None
    assert player._attr_current_media.elapsed_time == 0
    assert player._attr_elapsed_time == 0


def test_missing_progress_stays_none() -> None:
    """Without progress info, no position or duration is reported."""
    player = _make_player(_external_now_playing(None, duration=None))
    player._attr_active_source = "9999"

    player.set_dynamic_attributes(update_media=True)

    assert player._attr_current_media is not None
    assert player._attr_current_media.elapsed_time is None
    assert player._attr_current_media.duration is None
    assert player._attr_elapsed_time is None
