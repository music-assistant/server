"""
Tests for mirroring a live session's shuffle/repeat options onto the consuming queue.

``update_source_queue_options`` is the options sibling of ``update_stream_metadata``:
plugin providers push the session's shuffle/repeat state (identified by their instance
id) and the update lands on the queue only when its current item is the AudioSource
owned by that provider — a late callback must never stamp session state over an
unrelated item. Unlike stream metadata, the anchor is the item's media item: the
update must also work in the source-selection window before streamdetails exist.
"""

from __future__ import annotations

from unittest.mock import MagicMock, Mock

from music_assistant_models.enums import RepeatMode
from music_assistant_models.media_items import AudioSource, PlayableMediaItemType, Radio
from music_assistant_models.player_queue import PlayerQueue
from music_assistant_models.queue_item import QueueItem

from music_assistant.controllers.streams.controller import StreamsController

QUEUE_ID = "q1"
SOURCE_ID = "main"
INSTANCE_ID = "spotify_connect--test"


def _audio_source(*, provider: str = INSTANCE_ID, item_id: str = SOURCE_ID) -> AudioSource:
    """Build the AudioSource media item for the queue's current item."""
    return AudioSource(item_id=item_id, provider=provider, name="live", provider_mappings=set())


def _setup(media_item: PlayableMediaItemType | None) -> tuple[StreamsController, PlayerQueue]:
    """Build a bare streams controller whose queue plays an item with the given media item."""
    streams = StreamsController.__new__(StreamsController)
    streams.mass = MagicMock()
    streams.logger = MagicMock()
    queue = PlayerQueue(queue_id=QUEUE_ID, active=True, display_name="Q1", available=True, items=1)
    item = QueueItem(
        queue_id=QUEUE_ID, queue_item_id="qi1", name="live", duration=None, media_item=media_item
    )
    queue.current_item = item
    streams.mass.player_queues.get = Mock(return_value=queue)
    streams.mass.player_queues.signal_update = Mock()
    return streams, queue


def test_options_update_writes_shuffle_and_repeat_and_signals() -> None:
    """A matching update lands on the queue's options and signals a single update."""
    streams, queue = _setup(_audio_source())

    streams.update_source_queue_options(
        QUEUE_ID, SOURCE_ID, INSTANCE_ID, shuffle_enabled=True, repeat_mode=RepeatMode.ALL
    )

    assert queue.shuffle_enabled is True
    assert queue.repeat_mode == RepeatMode.ALL
    streams.mass.player_queues.signal_update.assert_called_once_with(QUEUE_ID)  # type: ignore[attr-defined]


def test_options_update_accepted_before_streamdetails_exist() -> None:
    """
    The cached-options replay on claiming a queue happens before the item streams.

    on_source_selected fires before the item's streamdetails are populated, so the
    options update must anchor on the media item and not require streamdetails.
    """
    streams, queue = _setup(_audio_source())
    assert queue.current_item is not None
    assert queue.current_item.streamdetails is None

    streams.update_source_queue_options(
        QUEUE_ID, SOURCE_ID, INSTANCE_ID, shuffle_enabled=True, repeat_mode=RepeatMode.ONE
    )

    assert queue.shuffle_enabled is True
    assert queue.repeat_mode == RepeatMode.ONE


def test_options_update_rejected_for_a_different_source() -> None:
    """An update for another source of the same provider is dropped."""
    streams, queue = _setup(_audio_source())

    streams.update_source_queue_options(
        QUEUE_ID, "other_source", INSTANCE_ID, shuffle_enabled=True, repeat_mode=RepeatMode.ALL
    )

    assert queue.shuffle_enabled is False
    assert queue.repeat_mode == RepeatMode.OFF
    streams.mass.player_queues.signal_update.assert_not_called()  # type: ignore[attr-defined]


def test_options_update_rejected_for_a_different_provider() -> None:
    """An update from a provider that does not own the current item is dropped."""
    streams, queue = _setup(_audio_source(provider="another_instance"))

    streams.update_source_queue_options(
        QUEUE_ID, SOURCE_ID, INSTANCE_ID, shuffle_enabled=True, repeat_mode=RepeatMode.ALL
    )

    assert queue.shuffle_enabled is False
    streams.mass.player_queues.signal_update.assert_not_called()  # type: ignore[attr-defined]


def test_options_update_rejected_when_current_item_is_no_audio_source() -> None:
    """Once the queue moved on to regular media, a late session callback is dropped."""
    streams, queue = _setup(
        Radio(item_id=SOURCE_ID, provider=INSTANCE_ID, name="radio", provider_mappings=set())
    )

    streams.update_source_queue_options(
        QUEUE_ID, SOURCE_ID, INSTANCE_ID, shuffle_enabled=True, repeat_mode=RepeatMode.ALL
    )

    assert queue.shuffle_enabled is False
    streams.mass.player_queues.signal_update.assert_not_called()  # type: ignore[attr-defined]


def test_options_update_rejected_without_media_item() -> None:
    """An item without a media item cannot receive session options."""
    streams, queue = _setup(None)

    streams.update_source_queue_options(
        QUEUE_ID, SOURCE_ID, INSTANCE_ID, shuffle_enabled=True, repeat_mode=RepeatMode.ALL
    )

    assert queue.shuffle_enabled is False
    streams.mass.player_queues.signal_update.assert_not_called()  # type: ignore[attr-defined]


def test_unknown_repeat_mode_is_skipped() -> None:
    """RepeatMode.UNKNOWN never overwrites the queue's repeat state."""
    streams, queue = _setup(_audio_source())

    streams.update_source_queue_options(
        QUEUE_ID, SOURCE_ID, INSTANCE_ID, shuffle_enabled=True, repeat_mode=RepeatMode.UNKNOWN
    )

    assert queue.shuffle_enabled is True
    assert queue.repeat_mode == RepeatMode.OFF
    streams.mass.player_queues.signal_update.assert_called_once_with(QUEUE_ID)  # type: ignore[attr-defined]


def test_none_values_leave_the_options_untouched() -> None:
    """None values mean "no report", so neither option changes and nothing is signaled."""
    streams, queue = _setup(_audio_source())
    queue.shuffle_enabled = True
    queue.repeat_mode = RepeatMode.ONE

    streams.update_source_queue_options(
        QUEUE_ID, SOURCE_ID, INSTANCE_ID, shuffle_enabled=None, repeat_mode=None
    )

    assert queue.shuffle_enabled is True
    assert queue.repeat_mode == RepeatMode.ONE
    streams.mass.player_queues.signal_update.assert_not_called()  # type: ignore[attr-defined]


def test_unchanged_options_do_not_signal() -> None:
    """An update that matches the queue's current state does not emit an event."""
    streams, queue = _setup(_audio_source())
    queue.shuffle_enabled = True
    queue.repeat_mode = RepeatMode.ALL

    streams.update_source_queue_options(
        QUEUE_ID, SOURCE_ID, INSTANCE_ID, shuffle_enabled=True, repeat_mode=RepeatMode.ALL
    )

    streams.mass.player_queues.signal_update.assert_not_called()  # type: ignore[attr-defined]
