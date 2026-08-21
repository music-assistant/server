"""
Tests for mirroring a live session's shuffle/repeat options onto the consuming queue.

``update_source_queue_options`` is the options sibling of ``update_stream_metadata``:
plugin providers push the session's shuffle/repeat state and the update lands on the
queue only when its current item is the AudioSource owned by that provider — a late
callback must never stamp session state over an unrelated item.
"""

from __future__ import annotations

from unittest.mock import MagicMock, Mock

from music_assistant_models.enums import ContentType, MediaType, RepeatMode, StreamType
from music_assistant_models.media_items import AudioFormat
from music_assistant_models.player_queue import PlayerQueue
from music_assistant_models.queue_item import QueueItem
from music_assistant_models.streamdetails import StreamDetails

from music_assistant.controllers.streams.controller import StreamsController
from music_assistant.models.plugin import PluginProvider

QUEUE_ID = "q1"
SOURCE_ID = "main"
INSTANCE_ID = "spotify_connect--test"


def _streamdetails(
    *, media_type: MediaType = MediaType.AUDIO_SOURCE, provider: str = INSTANCE_ID
) -> StreamDetails:
    """Build StreamDetails for the queue's current item."""
    return StreamDetails(
        item_id=SOURCE_ID,
        provider=provider,
        audio_format=AudioFormat(content_type=ContentType.PCM_S16LE, channels=2),
        media_type=media_type,
        stream_type=StreamType.CUSTOM,
    )


def _setup(
    streamdetails: StreamDetails | None,
) -> tuple[StreamsController, PlayerQueue, MagicMock]:
    """Build a bare streams controller whose queue plays an item with the given streamdetails."""
    streams = StreamsController.__new__(StreamsController)
    streams.mass = MagicMock()
    streams.logger = MagicMock()
    queue = PlayerQueue(queue_id=QUEUE_ID, active=True, display_name="Q1", available=True, items=1)
    item = QueueItem(queue_id=QUEUE_ID, queue_item_id="qi1", name="live", duration=None)
    item.streamdetails = streamdetails
    queue.current_item = item
    streams.mass.player_queues.get = Mock(return_value=queue)
    streams.mass.player_queues.signal_update = Mock()
    provider = MagicMock(spec=PluginProvider)
    provider.instance_id = INSTANCE_ID
    return streams, queue, provider


def test_options_update_writes_shuffle_and_repeat_and_signals() -> None:
    """A matching update lands on the queue's options and signals a single update."""
    streams, queue, provider = _setup(_streamdetails())

    streams.update_source_queue_options(
        QUEUE_ID, SOURCE_ID, provider, shuffle_enabled=True, repeat_mode=RepeatMode.ALL
    )

    assert queue.shuffle_enabled is True
    assert queue.repeat_mode == RepeatMode.ALL
    streams.mass.player_queues.signal_update.assert_called_once_with(QUEUE_ID)  # type: ignore[attr-defined]


def test_options_update_rejected_for_a_different_source() -> None:
    """An update for another source of the same provider is dropped."""
    streams, queue, provider = _setup(_streamdetails())

    streams.update_source_queue_options(
        QUEUE_ID, "other_source", provider, shuffle_enabled=True, repeat_mode=RepeatMode.ALL
    )

    assert queue.shuffle_enabled is False
    assert queue.repeat_mode == RepeatMode.OFF
    streams.mass.player_queues.signal_update.assert_not_called()  # type: ignore[attr-defined]


def test_options_update_rejected_for_a_different_provider() -> None:
    """An update from a provider that does not own the current item is dropped."""
    streams, queue, provider = _setup(_streamdetails(provider="another_instance"))

    streams.update_source_queue_options(
        QUEUE_ID, SOURCE_ID, provider, shuffle_enabled=True, repeat_mode=RepeatMode.ALL
    )

    assert queue.shuffle_enabled is False
    streams.mass.player_queues.signal_update.assert_not_called()  # type: ignore[attr-defined]


def test_options_update_rejected_when_current_item_is_no_audio_source() -> None:
    """Once the queue moved on to regular media, a late session callback is dropped."""
    streams, queue, provider = _setup(_streamdetails(media_type=MediaType.TRACK))

    streams.update_source_queue_options(
        QUEUE_ID, SOURCE_ID, provider, shuffle_enabled=True, repeat_mode=RepeatMode.ALL
    )

    assert queue.shuffle_enabled is False
    streams.mass.player_queues.signal_update.assert_not_called()  # type: ignore[attr-defined]


def test_options_update_rejected_without_streamdetails() -> None:
    """An item that is not streaming (yet) cannot receive session options."""
    streams, queue, provider = _setup(None)

    streams.update_source_queue_options(
        QUEUE_ID, SOURCE_ID, provider, shuffle_enabled=True, repeat_mode=RepeatMode.ALL
    )

    assert queue.shuffle_enabled is False
    streams.mass.player_queues.signal_update.assert_not_called()  # type: ignore[attr-defined]


def test_unknown_repeat_mode_is_skipped() -> None:
    """RepeatMode.UNKNOWN never overwrites the queue's repeat state."""
    streams, queue, provider = _setup(_streamdetails())

    streams.update_source_queue_options(
        QUEUE_ID, SOURCE_ID, provider, shuffle_enabled=True, repeat_mode=RepeatMode.UNKNOWN
    )

    assert queue.shuffle_enabled is True
    assert queue.repeat_mode == RepeatMode.OFF
    streams.mass.player_queues.signal_update.assert_called_once_with(QUEUE_ID)  # type: ignore[attr-defined]


def test_none_values_leave_the_options_untouched() -> None:
    """None values mean "no report", so neither option changes and nothing is signaled."""
    streams, queue, provider = _setup(_streamdetails())
    queue.shuffle_enabled = True
    queue.repeat_mode = RepeatMode.ONE

    streams.update_source_queue_options(
        QUEUE_ID, SOURCE_ID, provider, shuffle_enabled=None, repeat_mode=None
    )

    assert queue.shuffle_enabled is True
    assert queue.repeat_mode == RepeatMode.ONE
    streams.mass.player_queues.signal_update.assert_not_called()  # type: ignore[attr-defined]


def test_unchanged_options_do_not_signal() -> None:
    """An update that matches the queue's current state does not emit an event."""
    streams, queue, provider = _setup(_streamdetails())
    queue.shuffle_enabled = True
    queue.repeat_mode = RepeatMode.ALL

    streams.update_source_queue_options(
        QUEUE_ID, SOURCE_ID, provider, shuffle_enabled=True, repeat_mode=RepeatMode.ALL
    )

    streams.mass.player_queues.signal_update.assert_not_called()  # type: ignore[attr-defined]
