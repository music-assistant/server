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
from music_assistant.controllers.player_queues.media_resolver import ResolvedItem
from music_assistant.controllers.player_queues.state import PlayerQueueData
from music_assistant.controllers.players import PlayerController
from tests.common import bare_player_controller, virtual_loop_clock

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


async def _resolve(media_item: Track, *_args: Any, **_kwargs: Any) -> list[ResolvedItem]:
    """Resolve a track to itself, except the "slow" one, which never finishes."""
    if media_item.item_id == "slow":
        await asyncio.Event().wait()
    return [ResolvedItem(media_item)]


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


def _with_player_locks(ctrl: Any, owners: dict[str, str]) -> PlayerController:
    """
    Give the controller the real playback locks of a players controller.

    :param ctrl: The controller built by ``_controller``.
    :param owners: The owner (sync leader or group) of each player that has one, by player id.
    """
    players = bare_player_controller()
    players._playback_owner_id = lambda player_id: owners.get(player_id, player_id)  # type: ignore[method-assign]
    ctrl.mass.players.get_group_and_player_lock = players.get_group_and_player_lock
    return players


def _locked(players: PlayerController, *player_ids: str) -> list[str]:
    """Return which of the players' playback locks are held."""
    return [
        player_id
        for player_id in player_ids
        if (lock := players._player_command_locks.get(f"playback_{player_id}")) and lock.locked()
    ]


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


async def test_a_request_that_gives_up_lets_go_of_its_player_and_the_group_holding_it() -> None:
    """A player under a group is held with its group while resolving, and both are free after."""
    ctrl, _lock = _controller()
    players = _with_player_locks(ctrl, {"q1": "group"})
    held_while_resolving: list[str] = []

    async def _slow(*_args: Any, **_kwargs: Any) -> list[ResolvedItem]:
        held_while_resolving.extend(_locked(players, "q1", "group"))
        await asyncio.Event().wait()
        return []

    ctrl._media_resolver._resolve_media_items = AsyncMock(side_effect=_slow)

    with pytest.raises(PlayerCommandFailed) as exc_info:
        await ctrl.play_media("q1", _track("slow"), QueueOption.REPLACE)

    assert exc_info.value.translation_key == "play_request_timeout"
    assert held_while_resolving == ["q1", "group"]
    assert _locked(players, "q1", "group") == []
    assert not players._task_held_locks
    assert not players._lock_holders
    assert not ctrl._queue_data["q1"].queue.extra_attributes[ATTR_PLAY_ACTION_IN_PROGRESS]
    # the next command on the player or on its group gets its lock at once
    async with asyncio.timeout(0), players.get_group_and_player_lock("q1"):
        assert _locked(players, "q1", "group") == ["q1", "group"]


async def test_a_request_that_gives_up_leaves_the_locks_of_its_caller_with_the_caller() -> None:
    """A request made by a command that holds two players takes and lets go of none of them."""
    ctrl, _lock = _controller()
    players = _with_player_locks(ctrl, {})

    async with players.get_playback_locks("q1", "q2"):
        with pytest.raises(PlayerCommandFailed):
            await ctrl.play_media("q1", _track("slow"), QueueOption.REPLACE)
        assert _locked(players, "q1", "q2") == ["q1", "q2"]
        assert players.holds_player_lock("q1")
        assert players.holds_player_lock("q2")

    assert _locked(players, "q1", "q2") == []
    assert not players._task_held_locks
    assert not players._lock_holders


async def test_the_deadline_does_not_run_while_the_request_waits_for_its_turn() -> None:
    """A request that waits for a command over its player longer than the deadline still plays."""
    ctrl, _lock = _controller()
    players = _with_player_locks(ctrl, {})
    holding = asyncio.Event()
    wait = DEADLINE * 100

    async def _transfer() -> None:
        async with players.get_playback_locks("q1", "q2"):
            holding.set()
            await asyncio.sleep(wait)

    new = _track("new")
    with virtual_loop_clock():
        loop = asyncio.get_running_loop()
        transfer = asyncio.create_task(_transfer())
        await holding.wait()
        started = loop.time()
        await ctrl.play_media("q1", new, QueueOption.REPLACE)
        waited = loop.time() - started
        await transfer

    assert waited == pytest.approx(wait, abs=1e-3)
    assert [item.media_item for item in ctrl._queue_data["q1"].items] == [new]
    ctrl.play_index.assert_awaited_once()
    assert _locked(players, "q1", "q2") == []


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
