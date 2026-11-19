"""
Tests for the metadata a Sendspin player publishes while an announcement holds its queue.

The default announcement plays its clip as a stream of its own, while the player still shows the
track. A client that follows the position of the player would count the clip's seconds along the
track's bar and jump back when the music returns, so the track stands still at the position the
queue is held at.
"""

from __future__ import annotations

import time
from dataclasses import replace
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from aiosendspin.server.roles.metadata.state import Metadata
from music_assistant_models.player import PlayerMedia

from music_assistant.constants import ATTR_ANNOUNCEMENT_HELD_QUEUE, ATTR_ANNOUNCEMENT_IN_PROGRESS
from music_assistant.providers.sendspin.player import SendspinPlayer

QUEUE_ID = "queue-1"
HELD_POSITION = 280.6


def _queue(*, held: bool) -> Any:
    """Return the queue of the player, held at a position or not."""
    return SimpleNamespace(
        queue_id=QUEUE_ID,
        elapsed_time=HELD_POSITION if held else 12.0,
        extra_attributes={ATTR_ANNOUNCEMENT_IN_PROGRESS: True} if held else {},
    )


def _media(elapsed: int) -> PlayerMedia:
    """Return the media the player shows, with the position the clip's stream is at."""
    return PlayerMedia(
        uri="library://track/1",
        title="Track",
        duration=300,
        source_id=QUEUE_ID,
        queue_item_id="qi-1",
        elapsed_time=elapsed,
        elapsed_time_last_updated=time.time(),
    )


def _player(queue: Any, *, playing: bool = True) -> Any:
    """Return a player that publishes its metadata, with what the lookups return."""
    player = MagicMock()
    player.available = True
    player._content_takeover_pending = False
    player._metadata_generation = 1
    player._published_held_queue_id = None
    for name in (
        "_media_identity",
        "_metadata_publish_allowed",
        "_build_current_media_metadata",
        "_compute_track_progress_ms",
    ):
        setattr(player, name, getattr(SendspinPlayer, name).__get__(player, SendspinPlayer))
    player._announcement_held_position_ms = SendspinPlayer._announcement_held_position_ms
    player._held_or_anchored = SendspinPlayer._held_or_anchored.__get__(player, SendspinPlayer)
    type(player)._published_held = SendspinPlayer._published_held
    player._queue_repeat_shuffle.return_value = (MagicMock(), False)
    player._send_album_artwork = AsyncMock()
    player._album_artist = AsyncMock(return_value=None)
    player._send_beat_schedule = AsyncMock()
    player._timeline_anchored = MagicMock(
        side_effect=lambda metadata, *_args, **_kwargs: (metadata, 123)
    )
    player._player_media = _media(2)
    player._player_playing = playing
    player.mass.player_queues.get = MagicMock(return_value=queue)
    player.mass.player_queues.get_item = MagicMock(return_value=None)
    return player


async def test_a_held_queue_publishes_the_position_it_is_held_at() -> None:
    """The track stands still where the music resumes from, whatever the clip's stream reports."""
    player = _player(_queue(held=True))

    await SendspinPlayer.send_current_media_metadata(player)

    metadata = player._metadata_role.set_metadata.call_args.args[0]
    assert metadata.track_progress == int(HELD_POSITION * 1000)
    assert metadata.playback_speed == 0
    assert metadata.title == "Track"
    assert player._metadata_role.set_metadata.call_args.kwargs["timestamp_us"] is None
    player._timeline_anchored.assert_not_called()
    assert player._published_held_queue_id == QUEUE_ID


async def test_a_held_queue_has_no_beats_running() -> None:
    """The beats stand still with the position."""
    player = _player(_queue(held=True))

    await SendspinPlayer.send_current_media_metadata(player)

    beat_args = player._send_beat_schedule.call_args.args
    assert beat_args[2] == int(HELD_POSITION * 1000)
    assert beat_args[3] is False


async def test_a_queue_that_is_not_held_publishes_the_position_of_its_player() -> None:
    """Nothing changes for a track that plays on its own."""
    player = _player(_queue(held=False))

    await SendspinPlayer.send_current_media_metadata(player)

    metadata = player._metadata_role.set_metadata.call_args.args[0]
    assert metadata.playback_speed == 1000
    assert metadata.track_progress != int(HELD_POSITION * 1000)
    player._timeline_anchored.assert_called_once()
    assert player._published_held_queue_id is None


def _queue_update(queue: Any, *, published_held: bool) -> Any:
    """Return a player that is told about an update of its queue."""
    player = MagicMock()
    player.synced_to = None
    player._content_takeover_pending = False
    type(player)._published_held = SendspinPlayer._published_held
    player._player_media = _media(2)
    player._published_held_queue_id = QUEUE_ID if published_held else None
    player._announcement_held_position_ms = SendspinPlayer._announcement_held_position_ms
    player._queue_repeat_shuffle.return_value = (MagicMock(), False)
    player.mass.player_queues.get = MagicMock(return_value=queue)
    return player


def test_the_hold_of_a_queue_publishes_the_metadata_again() -> None:
    """The bar stops as soon as the queue is held, not with the next position update."""
    player = _queue_update(_queue(held=True), published_held=False)

    SendspinPlayer.on_queue_updated(player, QUEUE_ID)

    player.mass.create_task.assert_called_once()
    player.send_current_media_metadata.assert_called_once()


def test_the_release_of_a_queue_publishes_the_metadata_again() -> None:
    """The bar follows the track again as soon as the queue is released."""
    player = _queue_update(_queue(held=False), published_held=True)

    SendspinPlayer.on_queue_updated(player, QUEUE_ID)

    player.mass.create_task.assert_called_once()


def test_an_update_that_does_not_change_the_hold_publishes_nothing_more() -> None:
    """Every other update of the queue leaves the metadata alone."""
    for held in (True, False):
        player = _queue_update(_queue(held=held), published_held=held)

        SendspinPlayer.on_queue_updated(player, QUEUE_ID)

        player.mass.create_task.assert_not_called()


def _without_media(queue: Any) -> Any:
    """Return a player whose clip has ended, so that it shows no media, after a held publish."""
    player = _player(queue)
    player._player_media = None
    player._published_held_queue_id = QUEUE_ID
    player.player.extra_data = {ATTR_ANNOUNCEMENT_HELD_QUEUE: QUEUE_ID}
    player.announcement_held = SendspinPlayer.announcement_held.__get__(player, SendspinPlayer)
    player._clear_current_media_metadata = AsyncMock()
    return player


async def test_the_held_track_stays_when_the_clip_ends_before_the_music_is_back() -> None:
    """The gap between the clip and the restored music does not show an empty track."""
    player = _without_media(_queue(held=True))

    await SendspinPlayer.send_current_media_metadata(player)

    player._clear_current_media_metadata.assert_not_called()
    player._metadata_role.set_metadata.assert_not_called()


async def test_a_track_that_is_not_held_is_cleared_when_the_media_is_gone() -> None:
    """A player that shows nothing any more shows nothing on the client either."""
    player = _without_media(_queue(held=False))

    await SendspinPlayer.send_current_media_metadata(player)

    player._clear_current_media_metadata.assert_awaited_once()


def test_the_release_without_returning_music_clears_what_was_held() -> None:
    """The held track does not stay on the client when the music never comes back."""
    player = _queue_update(_queue(held=False), published_held=True)
    player._player_media = None

    SendspinPlayer.on_queue_updated(player, QUEUE_ID)

    player.mass.create_task.assert_called_once()


def test_the_update_of_another_queue_does_not_touch_a_held_player() -> None:
    """A player whose clip ended is not disturbed by the queues of other players."""
    player = _queue_update(_queue(held=False), published_held=True)
    player._player_media = None

    SendspinPlayer.on_queue_updated(player, "another-queue")

    player.mass.create_task.assert_not_called()


def test_an_update_while_the_clip_is_over_but_the_queue_is_held_clears_nothing() -> None:
    """The queue is still held, so the held track stays."""
    player = _queue_update(_queue(held=True), published_held=True)
    player._player_media = None

    SendspinPlayer.on_queue_updated(player, QUEUE_ID)

    player.mass.create_task.assert_not_called()


def _republishing(queue: Any) -> Any:
    """Return a player that moves its published position onto the timeline of its stream."""
    player = _player(queue)
    published = Metadata(title="Track", track_duration=300_000, track_progress=0, playback_speed=0)
    player._published_metadata = published
    player._published_item = ("library://track/1", "qi-1")
    player._timeline_anchored = MagicMock(
        side_effect=lambda metadata, *_args, **_kwargs: (replace(metadata, track_progress=5), None)
    )
    return player


async def test_the_position_is_not_moved_onto_the_timeline_of_the_clip() -> None:
    """The clip's stream has a timeline of its own, which does not place the held track."""
    player = _republishing(_queue(held=True))

    await SendspinPlayer._republish_position(player)

    player._timeline_anchored.assert_not_called()
    player._metadata_role.set_metadata.assert_not_called()


async def test_the_position_follows_the_timeline_when_the_queue_is_not_held() -> None:
    """Nothing changes for a track that plays on its own."""
    player = _republishing(_queue(held=False))

    await SendspinPlayer._republish_position(player)

    player._metadata_role.set_metadata.assert_called_once()
    assert player._published_held_queue_id is None


async def test_the_beats_are_not_refreshed_while_the_queue_is_held() -> None:
    """The beats stand still with the position."""
    player = _player(_queue(held=True))

    await SendspinPlayer._refresh_beat_schedule(player)

    player._send_beat_schedule.assert_not_called()
