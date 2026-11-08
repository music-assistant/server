"""
Tests for the metadata position a Sendspin player takes from its flow stream's timeline.

The queue's position counts from the play or seek command. On a server that takes a
while to prepare the next track, the audio starts seconds later, and clients that
extrapolate from the queue's position run ahead of what they play by that much.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, cast
from unittest.mock import MagicMock

from aiosendspin.server.roles.metadata.state import Metadata
from music_assistant_models.enums import PlayerType
from music_assistant_models.player_queue import PlayLogEntry

from music_assistant.providers.sendspin.player import SendspinPlayer

if TYPE_CHECKING:
    import pytest
    from music_assistant_models.player_queue import PlayerQueue
    from music_assistant_models.queue_item import QueueItem

_QUEUE_ITEM_ID = "qi-1"
_NOW_US = 50_000_000
# What the queue reports: counting since the command, 2.5 s before the audio started
_QUEUE_PROGRESS_MS = 4_500


def _queue_item(seek_position: int = 0) -> QueueItem:
    return cast(
        "QueueItem",
        SimpleNamespace(
            queue_item_id=_QUEUE_ITEM_ID,
            streamdetails=SimpleNamespace(seek_position=seek_position),
        ),
    )


def _player(
    monkeypatch: pytest.MonkeyPatch, timeline_start_us: int | None, seek_position: int = 0
) -> SendspinPlayer:
    """Build a SendspinPlayer whose flow stream started playing the track at the given time."""
    player = SendspinPlayer.__new__(SendspinPlayer)
    monkeypatch.setattr(
        SendspinPlayer,
        "provider",
        property(
            lambda _self: SimpleNamespace(
                server_api=SimpleNamespace(clock=SimpleNamespace(now_us=lambda: _NOW_US))
            )
        ),
        raising=False,
    )
    player.playback_session = SimpleNamespace(  # type: ignore[assignment]
        flow_track_anchor_us=lambda offset_us: (
            None if timeline_start_us is None else timeline_start_us + offset_us
        )
    )
    pq_data = SimpleNamespace(
        flow_mode_stream_log=[
            PlayLogEntry(
                queue_item_id=_QUEUE_ITEM_ID,
                seconds_streamed=None,
                seek_position=seek_position,
            )
        ]
    )
    player.mass = SimpleNamespace(  # type: ignore[assignment]
        player_queues=SimpleNamespace(queue_data_or_none=lambda _queue_id: pq_data)
    )
    return player


def _metadata() -> Metadata:
    return Metadata(
        title="Track",
        track_duration=180_000,
        track_progress=_QUEUE_PROGRESS_MS,
        playback_speed=1000,
    )


def _queue() -> PlayerQueue:
    return cast("PlayerQueue", SimpleNamespace(queue_id="queue-1"))


def test_position_comes_from_when_the_track_is_heard(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two seconds after the track's audio started, the position is 2 s, whatever the queue says."""
    player = _player(monkeypatch, timeline_start_us=_NOW_US - 2_000_000)

    metadata, timestamp_us = player._timeline_anchored(
        _metadata(), _queue(), _queue_item(), is_playing=True
    )

    assert metadata.track_progress == 2_000
    assert timestamp_us is None


def test_a_track_not_heard_yet_takes_effect_when_it_is(monkeypatch: pytest.MonkeyPatch) -> None:
    """Committed but still the lead time away: position 0 is scheduled for its first chunk."""
    first_chunk_us = _NOW_US + 700_000
    player = _player(monkeypatch, timeline_start_us=first_chunk_us)

    metadata, timestamp_us = player._timeline_anchored(
        _metadata(), _queue(), _queue_item(), is_playing=True
    )

    assert metadata.track_progress == 0
    assert timestamp_us == first_chunk_us


def test_a_seek_counts_from_the_seek_position(monkeypatch: pytest.MonkeyPatch) -> None:
    """After a seek to 60 s the stream starts at 60 s, so that is where the position counts from."""
    player = _player(monkeypatch, timeline_start_us=_NOW_US - 1_500_000, seek_position=60)

    metadata, _ = player._timeline_anchored(
        _metadata(), _queue(), _queue_item(seek_position=60), is_playing=True
    )

    assert metadata.track_progress == 61_500


def test_without_a_timeline_the_queue_position_stays(monkeypatch: pytest.MonkeyPatch) -> None:
    """Before the first chunk commits, and while paused, the queue's position is all there is."""
    player = _player(monkeypatch, timeline_start_us=None)

    metadata, timestamp_us = player._timeline_anchored(
        _metadata(), _queue(), _queue_item(), is_playing=True
    )
    assert metadata.track_progress == _QUEUE_PROGRESS_MS
    assert timestamp_us is None

    player = _player(monkeypatch, timeline_start_us=_NOW_US - 2_000_000)
    metadata, timestamp_us = player._timeline_anchored(
        _metadata(), _queue(), _queue_item(), is_playing=False
    )
    assert metadata.track_progress == _QUEUE_PROGRESS_MS
    assert timestamp_us is None


def test_first_audio_heard_rebases_the_queue_of_the_protocol_parent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    The heard correction is below the jump threshold, so the queue is re-based explicitly.

    A Sendspin player usually runs as the protocol player of a universal player that owns the
    queue, and whose state only follows on a deferred update.
    """
    player = SendspinPlayer.__new__(SendspinPlayer)
    parent = MagicMock()
    mass = MagicMock()
    mass.players.get_player.return_value = parent
    player.mass = mass
    monkeypatch.setattr(SendspinPlayer, "synced_to", property(lambda _self: None), raising=False)
    monkeypatch.setattr(SendspinPlayer, "player_id", "sendspin-1", raising=False)
    monkeypatch.setattr(
        SendspinPlayer, "type", property(lambda _self: PlayerType.PROTOCOL), raising=False
    )
    monkeypatch.setattr(
        SendspinPlayer, "protocol_parent_id", property(lambda _self: "universal-1"), raising=False
    )
    monkeypatch.setattr(SendspinPlayer, "send_current_media_metadata", MagicMock())

    player.on_first_audio_heard()

    mass.players.get_player.assert_called_once_with("universal-1")
    parent.update_state.assert_called_once_with(force_update=True)
    mass.players.on_player_position_jumped.assert_called_once_with(parent)
    mass.create_task.assert_called_once()
