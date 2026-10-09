"""
Tests for ordering a shuffled queue for Smart Fades one batch at a time.

A shuffle orders only the first batch of upcoming items for their transitions. Playback orders the
next batch behind it once it gets close, so loading a queue never waits for all of it to be ordered
and smart shuffle keeps deciding which tracks come next.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest
from music_assistant_models.enums import MediaType, PlaybackState
from music_assistant_models.media_items import ItemMapping, ProviderMapping, Track
from music_assistant_models.player_queue import PlayerQueue
from music_assistant_models.queue_item import QueueItem
from music_assistant_models.unique_list import UniqueList

from music_assistant.constants import CONF_VALUE_ENABLED
from music_assistant.controllers.music.recency import RecencySnapshot
from music_assistant.controllers.player_queues import PlayerQueuesController
from music_assistant.controllers.player_queues.constants import SMART_FADE_ORDERING_BATCH
from music_assistant.controllers.player_queues.smart_shuffle import SmartShuffle
from music_assistant.controllers.player_queues.state import PlayerQueueData

QUEUE_ID = "q1"
SMART_SHUFFLE = "music_assistant.controllers.player_queues.smart_shuffle"


def _item(index: int) -> QueueItem:
    """Build a queue item wrapping a track by an artist of its own."""
    name = f"t{index}"
    track = Track(
        item_id=name,
        provider="library",
        name=name,
        duration=180,
        artists=UniqueList(
            [
                ItemMapping(
                    item_id=f"a{index}",
                    provider="library",
                    name=f"Artist {index}",
                    media_type=MediaType.ARTIST,
                )
            ]
        ),
        provider_mappings={
            ProviderMapping(item_id=name, provider_domain="library", provider_instance="library")
        },
    )
    return QueueItem(
        queue_id=QUEUE_ID, queue_item_id=name, name=name, duration=180, media_item=track
    )


def _ids(items: list[QueueItem]) -> list[str]:
    return [item.queue_item_id for item in items]


def _controller(
    count: int = 80, current_index: int = 0
) -> tuple[PlayerQueuesController, PlayerQueueData]:
    """Build a bare controller playing a shuffled queue with Smart Fades ordering enabled."""
    ctrl = PlayerQueuesController.__new__(PlayerQueuesController)
    ctrl.logger = MagicMock()
    ctrl.mass = MagicMock()
    ctrl.mass.config.get_effective_player_queue_config_value = Mock(return_value=CONF_VALUE_ENABLED)
    ctrl.mass.music.recency.snapshot = AsyncMock(return_value=RecencySnapshot(now=1_000_000_000))
    ctrl.mass.streams.is_smart_fades_active.return_value = True
    ctrl.signal_update = Mock()  # type: ignore[method-assign]
    ctrl.update_next_item_on_player = Mock()  # type: ignore[method-assign]
    items = [_item(index) for index in range(count)]
    queue = PlayerQueue(
        queue_id=QUEUE_ID,
        active=True,
        display_name="Q1",
        available=True,
        items=count,
        state=PlaybackState.PLAYING,
        current_index=current_index,
        index_in_buffer=current_index,
        shuffle_enabled=True,
    )
    queue.smart_fades_active = True
    queue.current_item = items[current_index]
    queue_data = PlayerQueueData(queue=queue, items=items)
    ctrl._queue_data = {QUEUE_ID: queue_data}
    ctrl._smart_shuffle = SmartShuffle(ctrl)
    return ctrl, queue_data


@pytest.fixture
def ordered_batches(monkeypatch: pytest.MonkeyPatch) -> list[tuple[list[str], str | None]]:
    """Make each transition ordering reverse its batch, recording the batch and its anchor."""
    calls: list[tuple[list[str], str | None]] = []

    async def reverse(
        _mass: object,
        items: list[QueueItem],
        *,
        preceding_track: Track | None = None,
        **_kwargs: object,
    ) -> list[QueueItem]:
        calls.append((_ids(items), preceding_track.item_id if preceding_track else None))
        return list(reversed(items))

    monkeypatch.setattr(f"{SMART_SHUFFLE}._interleave", list)
    monkeypatch.setattr(f"{SMART_SHUFFLE}.order_queue_items", reverse)
    return calls


async def test_a_shuffle_orders_only_the_first_batch_and_remembers_its_end(
    ordered_batches: list[tuple[list[str], str | None]],
) -> None:
    """A shuffle orders the first batch behind the fixed item, and the queue remembers its end."""
    ctrl, queue_data = _controller()
    tail = queue_data.items[1:]

    arranged = await ctrl._smart_shuffle.arrange(
        queue_data.queue, tail, preceding_item=queue_data.items[0]
    )

    assert ordered_batches == [(_ids(tail[:SMART_FADE_ORDERING_BATCH]), "t0")]
    assert queue_data.fade_ordered_until == arranged[SMART_FADE_ORDERING_BATCH - 1].queue_item_id


async def test_the_next_batch_is_ordered_behind_the_last_ordered_item(
    ordered_batches: list[tuple[list[str], str | None]],
) -> None:
    """The next batch starts right after the remembered item, which anchors its first transition."""
    ctrl, queue_data = _controller(current_index=20)
    items = list(queue_data.items)
    queue_data.fade_ordered_until = "t24"
    end = 25 + SMART_FADE_ORDERING_BATCH

    await ctrl._smart_shuffle.order_next_batch(QUEUE_ID)

    assert ordered_batches == [(_ids(items[25:end]), "t24")]
    assert _ids(queue_data.items) == _ids([*items[:25], *reversed(items[25:end]), *items[end:]])
    assert queue_data.fade_ordered_until == "t25"


@pytest.mark.parametrize("ordered_until", [None, "t5"])
async def test_the_next_batch_leaves_the_playing_and_the_next_item_alone(
    ordered_batches: list[tuple[list[str], str | None]], ordered_until: str | None
) -> None:
    """Without ordered items ahead, the batch starts behind the item after the buffered one."""
    ctrl, queue_data = _controller(current_index=10)
    queue_data.queue.index_in_buffer = 11
    queue_data.fade_ordered_until = ordered_until
    items = list(queue_data.items)

    await ctrl._smart_shuffle.order_next_batch(QUEUE_ID)

    assert ordered_batches == [(_ids(items[13 : 13 + SMART_FADE_ORDERING_BATCH]), "t12")]
    assert _ids(queue_data.items[:13]) == _ids(items[:13])


async def test_the_next_batch_is_dropped_when_the_queue_changed_meanwhile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An edit that lands while the batch is ordered wins, and the remembered end stays."""
    ctrl, queue_data = _controller(current_index=20)
    queue_data.fade_ordered_until = "t24"
    edited = [*queue_data.items, _item(99)]

    async def edit_meanwhile(
        _mass: object, items: list[QueueItem], **_kwargs: object
    ) -> list[QueueItem]:
        ctrl.update_items(QUEUE_ID, edited)
        return list(reversed(items))

    monkeypatch.setattr(f"{SMART_SHUFFLE}._interleave", list)
    monkeypatch.setattr(f"{SMART_SHUFFLE}.order_queue_items", edit_meanwhile)

    await ctrl._smart_shuffle.order_next_batch(QUEUE_ID)

    assert queue_data.items is edited
    assert queue_data.fade_ordered_until == "t24"


@pytest.mark.parametrize(
    ("ordered_until", "scheduled"), [("t30", False), ("t25", True), (None, True)]
)
def test_the_next_batch_is_scheduled_once_playback_gets_close(
    ordered_until: str | None, scheduled: bool
) -> None:
    """Playback asks for the next batch when only a few ordered items are left ahead of it."""
    ctrl, queue_data = _controller(current_index=20)
    queue_data.fade_ordered_until = ordered_until

    ctrl._smart_shuffle.schedule_next_batch(queue_data.queue)

    call_later = cast("Mock", ctrl.mass.call_later)
    assert call_later.called is scheduled
    if scheduled:
        assert call_later.call_args.args[1] == ctrl._smart_shuffle.order_next_batch
        assert call_later.call_args.kwargs["task_id"] == f"order_next_fade_batch_{QUEUE_ID}"


@pytest.mark.parametrize(
    ("attribute", "value"),
    [("is_dynamic", True), ("shuffle_enabled", False), ("smart_fades_active", False)],
)
def test_the_next_batch_is_only_scheduled_for_a_smart_fades_shuffled_queue(
    attribute: str, value: bool
) -> None:
    """A dynamic queue orders its own refill batches; without the ordering there is no batch."""
    ctrl, queue_data = _controller(current_index=20)
    setattr(queue_data.queue, attribute, value)

    ctrl._smart_shuffle.schedule_next_batch(queue_data.queue)

    cast("Mock", ctrl.mass.call_later).assert_not_called()


def test_a_track_change_asks_for_the_next_batch() -> None:
    """Every change of the playing item checks whether the next batch is due, nothing else does."""
    ctrl, queue_data = _controller(count=10)
    smart_shuffle = Mock()
    ctrl._smart_shuffle = smart_shuffle

    def player_plays(queue_item_id: str) -> None:
        player: Any = SimpleNamespace(
            player_id=QUEUE_ID,
            active_output_protocol=None,
            current_media=SimpleNamespace(
                source_id=QUEUE_ID, queue_item_id=queue_item_id, uri=None
            ),
            state=SimpleNamespace(
                name="Q1",
                available=True,
                playback_state=PlaybackState.PLAYING,
                corrected_elapsed_time=10,
                group_members=[],
            ),
        )
        ctrl._update_queue_from_player(player)

    player_plays("t0")
    player_plays("t0")
    player_plays("t1")

    assert smart_shuffle.schedule_next_batch.call_count == 2
    smart_shuffle.schedule_next_batch.assert_called_with(queue_data.queue)
