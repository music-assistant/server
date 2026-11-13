"""
Tests for the deadline on resolving what a play request plays.

Resolving a big artist, playlist or folder through a throttled provider can take minutes while
the request holds the queue's playback lock. A resolution that outlives the deadline fails the
request and releases the lock before anything was replaced, so the queue keeps its items and
playback and the next command gets through at once.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest
from music_assistant_models.enums import MediaType, QueueOption
from music_assistant_models.errors import PlayerCommandFailed
from music_assistant_models.media_items import ItemMapping, ProviderMapping, Track
from music_assistant_models.player_queue import PlayerQueue
from music_assistant_models.queue_item import QueueItem
from music_assistant_models.unique_list import UniqueList

from music_assistant.constants import ATTR_PLAY_ACTION_IN_PROGRESS
from music_assistant.controllers.player_queues import PlayerQueuesController
from music_assistant.controllers.player_queues.state import PlayerQueueData

QUEUE_LOADER = "music_assistant.controllers.player_queues.queue_loader"
HELPERS = "music_assistant.controllers.player_queues.helpers"
DEADLINE = 0.05

pytestmark = pytest.mark.usefixtures("short_deadline")


@pytest.fixture
def short_deadline() -> Any:
    """Shorten the resolve deadline and play anonymously."""
    with (
        patch(f"{HELPERS}.PLAY_REQUEST_RESOLVE_TIMEOUT", DEADLINE),
        patch(f"{QUEUE_LOADER}.get_current_user", Mock(return_value=None)),
    ):
        yield


def _track(item_id: str) -> Track:
    """Build a playable track."""
    return Track(
        item_id=item_id,
        provider="test",
        name=f"Track {item_id}",
        duration=180,
        artists=UniqueList(
            [ItemMapping(item_id="a", provider="test", name="A", media_type=MediaType.ARTIST)]
        ),
        provider_mappings={
            ProviderMapping(item_id=item_id, provider_domain="test", provider_instance="test")
        },
    )


async def _resolve(media_item: Track, *_args: Any, **_kwargs: Any) -> list[Track]:
    """Resolve a track to itself, except the "slow" one, which never finishes."""
    if media_item.item_id == "slow":
        await asyncio.Event().wait()
    return [media_item]


def _controller() -> tuple[Any, asyncio.Lock]:
    """
    Build a bare controller driving ``play_media`` on a queue "q1" that plays one item.

    The queue's playback lock is a real lock, returned next to the controller.
    """
    ctrl = PlayerQueuesController.__new__(PlayerQueuesController)
    ctrl.logger = Mock()
    ctrl.mass = MagicMock()
    ctrl.mass.players.get_player = Mock(
        return_value=Mock(extra_data={}, offline_by_power_control=False)
    )
    lock = asyncio.Lock()

    @asynccontextmanager
    async def _playback_lock(*_args: Any, **_kwargs: Any) -> AsyncIterator[None]:
        async with lock:
            yield

    ctrl.mass.players.get_group_and_player_lock = _playback_lock
    ctrl.mass.providers = []
    ctrl.signal_update = Mock()  # type: ignore[method-assign]
    ctrl.on_player_update = Mock()  # type: ignore[method-assign]
    ctrl.play_index = AsyncMock()  # type: ignore[method-assign]
    ctrl.get_next_item = Mock(return_value=None)  # type: ignore[method-assign]
    ctrl.get_config_value = Mock(  # type: ignore[method-assign]
        return_value=QueueOption.REPLACE.value
    )
    ctrl._managed_pool = Mock()
    ctrl._smart_shuffle = Mock()
    ctrl._smart_shuffle.is_enabled = Mock(return_value=False)
    ctrl._media_resolver = Mock()
    ctrl._media_resolver._resolve_media_items = AsyncMock(side_effect=_resolve)
    queue = PlayerQueue(queue_id="q1", active=True, display_name="Q1", available=True, items=1)
    playing = _track("playing")
    ctrl._queue_data = {
        "q1": PlayerQueueData(
            queue=queue,
            items=[QueueItem.from_media_item("q1", playing)],
            source_items=[playing],
        )
    }
    return ctrl, lock


async def test_a_slow_resolution_fails_and_leaves_the_queue_alone() -> None:
    """The request fails with a translated error, the queue keeps its items and playback."""
    ctrl, lock = _controller()
    data: PlayerQueueData = ctrl._queue_data["q1"]
    items_before = list(data.items)
    sources_before = list(data.source_items)

    with pytest.raises(PlayerCommandFailed) as exc_info:
        await ctrl.play_media("q1", _track("slow"), QueueOption.REPLACE)

    assert exc_info.value.translation_key == "play_request_timeout"
    assert exc_info.value.translation_owner == "core.player_queues"
    assert exc_info.value.translation_args == [DEADLINE]
    assert data.items == items_before
    assert data.source_items == sources_before
    assert data.userid is None
    ctrl.play_index.assert_not_awaited()
    assert not lock.locked()
    assert not data.queue.extra_attributes[ATTR_PLAY_ACTION_IN_PROGRESS]
    assert not data.transitioning


@pytest.mark.parametrize(
    ("is_dynamic", "option", "shuffle"),
    [(False, QueueOption.PLAY, False), (True, QueueOption.REPLACE, None)],
    ids=["queue", "smart_mix"],
)
async def test_a_slow_resolution_leaves_shuffle_and_seeds_alone(
    is_dynamic: bool, option: QueueOption, shuffle: bool | None
) -> None:
    """A request that runs out of time keeps the queue's shuffle state and autoplay seeds."""
    ctrl, _lock = _controller()
    data: PlayerQueueData = ctrl._queue_data["q1"]
    data.queue.current_index = 0
    data.queue.shuffle_enabled = True
    data.queue.is_dynamic = is_dynamic
    data.enqueued_media_items = [_track("earlier")]
    items_before = list(data.items)
    seeds_before = list(data.enqueued_media_items)

    with pytest.raises(PlayerCommandFailed):
        await ctrl.play_media("q1", _track("slow"), option, shuffle=shuffle)

    assert data.queue.shuffle_enabled
    assert data.queue.is_dynamic == is_dynamic
    assert data.enqueued_media_items == seeds_before
    assert data.items == items_before


async def test_the_next_command_gets_through_once_the_deadline_passed() -> None:
    """A command waiting for the lock runs as soon as the slow request gave up."""
    ctrl, _lock = _controller()
    slow = asyncio.create_task(ctrl.play_media("q1", _track("slow"), QueueOption.REPLACE))
    await asyncio.sleep(0)
    new = _track("new")

    async with asyncio.timeout(DEADLINE * 10):
        await ctrl.play_media("q1", new, QueueOption.REPLACE)

    with pytest.raises(PlayerCommandFailed):
        await slow
    assert [item.media_item for item in ctrl._queue_data["q1"].items] == [new]
    ctrl.play_index.assert_awaited_once()


async def test_a_provider_timeout_is_not_reported_as_the_deadline() -> None:
    """A timeout raised by the lookup itself is passed on as it is."""
    ctrl, lock = _controller()
    ctrl._media_resolver._resolve_media_items = AsyncMock(side_effect=TimeoutError)

    with pytest.raises(TimeoutError):
        await ctrl.play_media("q1", _track("new"), QueueOption.REPLACE)

    assert not lock.locked()


async def test_a_quick_resolution_plays_as_usual() -> None:
    """A request that resolves within the deadline replaces the queue and starts it."""
    ctrl, _lock = _controller()
    new = _track("new")

    await ctrl.play_media("q1", new, QueueOption.REPLACE)

    assert [item.media_item for item in ctrl._queue_data["q1"].items] == [new]
    ctrl.play_index.assert_awaited_once()
