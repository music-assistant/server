"""Tests for metadata progress extrapolation."""

from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from music_assistant_models.enums import PlaybackState
from music_assistant_models.player import PlayerMedia

from music_assistant.providers.sendspin.player import SendspinPlayer


def _compute(current_media: PlayerMedia, *, is_playing: bool) -> int:
    """Run the real method against a mocked player."""
    mock = MagicMock()
    mock.corrected_elapsed_time = None
    mock.elapsed_time = None
    return SendspinPlayer._compute_track_progress_ms(mock, current_media, is_playing=is_playing)


def _media(elapsed: int | None, *, updated_ago: float | None = None) -> PlayerMedia:
    return PlayerMedia(
        uri="library://track/1",
        elapsed_time=elapsed,
        elapsed_time_last_updated=time.time() - updated_ago if updated_ago is not None else None,
    )


def test_steady_playback_extrapolates_from_an_old_anchor() -> None:
    """A push 90 s into steady playback reports the live position, not the anchor."""
    result = _compute(_media(0, updated_ago=90.0), is_playing=True)
    assert 90_000 <= result <= 91_500


def test_not_playing_keeps_the_fixed_position() -> None:
    """Paused/idle positions must not advance at all."""
    assert _compute(_media(145, updated_ago=432_000.0), is_playing=False) == 145_000


def test_missing_media_elapsed_falls_back_to_player() -> None:
    """Without media bookkeeping, the player-level elapsed is used."""
    mock = MagicMock()
    mock.corrected_elapsed_time = 12.0
    mock.elapsed_time = 10.0
    result = SendspinPlayer._compute_track_progress_ms(mock, _media(None), is_playing=True)
    assert result == 12_000


def test_metadata_builder_preserves_unknown_duration_progress() -> None:
    """Unknown duration uses zero while progress remains populated."""
    player = MagicMock()
    player.state.playback_state = PlaybackState.PAUSED
    player.corrected_elapsed_time = None
    player.elapsed_time = None
    player._compute_track_progress_ms = SendspinPlayer._compute_track_progress_ms.__get__(
        player, SendspinPlayer
    )
    media = PlayerMedia(uri="library://track/1", duration=0, elapsed_time=12)
    metadata = SendspinPlayer._build_current_media_metadata(player, media, None, is_playing=False)

    assert metadata.track_duration == 0
    assert metadata.track_progress == 12_000


def test_metadata_builder_clamps_progress_to_duration() -> None:
    """Extrapolation never reports a position past the end of the track."""
    player = MagicMock()
    player.state.playback_state = PlaybackState.PLAYING
    player._compute_track_progress_ms = SendspinPlayer._compute_track_progress_ms.__get__(
        player, SendspinPlayer
    )
    media = _media(170, updated_ago=60.0)
    media.duration = 180
    metadata = SendspinPlayer._build_current_media_metadata(player, media, None, is_playing=True)

    assert metadata.track_progress == 180_000


async def test_push_pairs_the_position_with_the_playing_state() -> None:
    """A resume during the artwork lookup reports the fresh position, not the idle snapshot."""
    player = MagicMock()
    player.available = True
    player._content_takeover_pending = False
    player._metadata_generation = 1
    for name in (
        "_media_identity",
        "_metadata_publish_allowed",
        "_build_current_media_metadata",
        "_compute_track_progress_ms",
    ):
        setattr(player, name, getattr(SendspinPlayer, name).__get__(player, SendspinPlayer))
    player._queue_repeat_shuffle.return_value = (MagicMock(), False)
    player._send_beat_schedule = AsyncMock()
    idle_media = _media(74, updated_ago=360.0)
    idle_media.duration = 309
    player.state = SimpleNamespace(current_media=idle_media, playback_state=PlaybackState.IDLE)

    async def resume_during_lookup(*_args: object, **_kwargs: object) -> None:
        playing_media = _media(74, updated_ago=0.0)
        playing_media.duration = 309
        player.state = SimpleNamespace(
            current_media=playing_media, playback_state=PlaybackState.PLAYING
        )

    player._send_album_artwork = AsyncMock(side_effect=resume_during_lookup)

    await SendspinPlayer.send_current_media_metadata(player)

    metadata = player._metadata_role.set_metadata.call_args.args[0]
    assert 74_000 <= metadata.track_progress <= 75_500
