"""Unit tests for the AI Radio sticky queue DJ state."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import pytest
from music_assistant_models.errors import InvalidDataError, MusicAssistantError

from music_assistant.providers.ai_radio.constants import (
    ATTR_GAP_NEXT_ID,
    ATTR_HOST_ID,
    ATTR_QUEUE_DJ,
    ATTR_SESSION_ID,
)
from music_assistant.providers.ai_radio.queue_dj import AIRadioQueueDJMixin
from music_assistant.providers.ai_radio.runtime import AIRadioRuntimeMixin
from music_assistant.providers.ai_radio.storage import AIRadioStorageMixin


class FakeQueue:
    """Minimal PlayerQueue stand-in."""

    def __init__(
        self, queue_id: str, current_index: int | None, index_in_buffer: int | None
    ) -> None:
        """Initialize the fake queue with its playback pointers."""
        self.queue_id = queue_id
        self.current_index = current_index
        self.index_in_buffer = index_in_buffer


class FakeQueueItem:
    """Minimal QueueItem stand-in."""

    _counter = 0

    def __init__(
        self, name: str, duration: int | None = 200, extra: dict[str, Any] | None = None
    ) -> None:
        """Initialize the fake queue item with a unique id."""
        FakeQueueItem._counter += 1
        self.queue_item_id = f"qi{FakeQueueItem._counter}"
        self.name = name
        self.duration = duration
        self.media_item = None
        self.extra_attributes: dict[str, Any] = dict(extra or {})


class FakePlayerQueues:
    """Minimal PlayerQueues controller stand-in."""

    def __init__(self, queue: FakeQueue, items: list[FakeQueueItem]) -> None:
        """Initialize with one queue and its items."""
        self._queue = queue
        self._items = items
        self.loads: list[tuple[list[Any], int]] = []
        self.deleted: list[str] = []
        # stands in for the QUEUE_ITEMS_UPDATED event a real load() emits
        self.on_load: Callable[[], None] | None = None

    def get(self, queue_id: str) -> FakeQueue | None:
        """Return the queue when the id matches."""
        return self._queue if queue_id == self._queue.queue_id else None

    def items(self, queue_id: str, limit: int = 500, offset: int = 0) -> list[Any]:
        """Return one page of queue items."""
        return self._items[offset : offset + limit]

    def index_by_id(self, queue_id: str, queue_item_id: str) -> int | None:
        """Return the current index of the given item."""
        for index, item in enumerate(self._items):
            if item.queue_item_id == queue_item_id:
                return index
        return None

    async def load(
        self,
        queue_id: str,
        queue_items: list[Any],
        insert_at_index: int,
        keep_remaining: bool,
        keep_played: bool,
    ) -> None:
        """Insert the given items at the requested index."""
        self.loads.append((queue_items, insert_at_index))
        self._items[insert_at_index:insert_at_index] = queue_items
        if self.on_load is not None:
            self.on_load()

    def delete_item(self, queue_id: str, item_id: str) -> None:
        """Delete the given item from the queue."""
        self.deleted.append(item_id)
        self._items = [i for i in self._items if i.queue_item_id != item_id]


class FakeMass:
    """Minimal MusicAssistant stand-in for the queue DJ mixin."""

    def __init__(self, player_queues: FakePlayerQueues) -> None:
        """Initialize with the fake player queues controller."""
        self.player_queues = player_queues
        self.tasks: list[asyncio.Task[Any]] = []
        self._tasks_by_id: dict[str, asyncio.Task[Any]] = {}

    def create_task(self, target: Any, task_id: str | None = None) -> asyncio.Task[Any]:
        """Run the given coroutine as a task, deduplicating on task id like mass does."""
        if task_id and (existing := self._tasks_by_id.get(task_id)) and not existing.done():
            target.close()
            return existing
        task = asyncio.ensure_future(target)
        self.tasks.append(task)
        if task_id:
            self._tasks_by_id[task_id] = task
        return task

    def subscribe(self, *args: Any, **kwargs: Any) -> Any:
        """Return a no-op unsubscribe callback."""
        return lambda: None


class StubConfig:
    """Minimal ProviderConfig stand-in exposing get_value."""

    def get_value(self, key: str, default: Any = None) -> Any:
        """Return the default for every config key."""
        return default


class DummyQueueDJ(AIRadioQueueDJMixin, AIRadioStorageMixin):
    """Minimal harness for queue DJ state tests."""

    instance_id = "ai_radio_test"

    def __init__(self, tmp_path: Path) -> None:
        """Initialize dummy mixin state."""
        self.logger = logging.getLogger(__name__)
        self._hosts: dict[str, dict[str, Any]] = {
            "rick": {"id": "rick", "name": "Rick", "instructions": "x", "tts_engine": ""},
        }
        self._dj_queues: dict[str, Any] = {}
        self._dj_file = tmp_path / "queue_dj.json"
        self._dj_lock = asyncio.Lock()
        # the disable path reaches the real clip cleanup, which needs a queue layer;
        # this one holds no queue at all, so cleanup finds nothing to do
        self.mass = cast(
            "Any", FakeMass(FakePlayerQueues(FakeQueue("other-queue", None, None), []))
        )

    def _schedule_replan(self, queue_id: str) -> None:
        """Record replan requests instead of running them."""
        self.replanned = getattr(self, "replanned", [])
        self.replanned.append(queue_id)


class ReplanQueueDJ(AIRadioRuntimeMixin, AIRadioQueueDJMixin, AIRadioStorageMixin):
    """Harness combining the queue DJ mixin with the real planner and clip builder."""

    instance_id = "ai_radio_test"
    domain = "ai_radio"

    def __init__(
        self,
        tmp_path: Path,
        queue: FakeQueue,
        items: list[FakeQueueItem],
        host: dict[str, Any],
    ) -> None:
        """Initialize the harness around one fake queue."""
        self.logger = logging.getLogger(__name__)
        self.config = cast("Any", StubConfig())
        self._sections = {_transition_section()["id"]: _transition_section()}
        self._hosts: dict[str, dict[str, Any]] = {host["id"]: host}
        self._dj_queues: dict[str, Any] = {}
        self._dj_file = tmp_path / "queue_dj.json"
        self._dj_lock = asyncio.Lock()
        self._unloading = False
        self.player_queues = FakePlayerQueues(queue, items)
        self.mass = cast("Any", FakeMass(self.player_queues))


def _transition_section() -> dict[str, Any]:
    """Return the single shared section used by the queue DJ test hosts."""
    return {
        "id": "Song_Transition",
        "name": "Song Transition",
        "type": "ai_text",
        "web_search": "disabled",
        "prompt": "From <prev_songinfo> to <next_songinfo>",
        "constraints": {"max_chars": 200},
    }


def _must_host() -> dict[str, Any]:
    """Return a host that always plans a section between songs."""
    return {
        "id": "rick",
        "name": "Rick",
        "instructions": "keep it short",
        "tts_engine": "",
        "section_ids": ["Song_Transition"],
        "section_order": [{"when": "between_songs", "flow": [{"MUST": "Song_Transition"}]}],
        "merge_section_id": "",
    }


def _optional_host(min_gap_songs: int) -> dict[str, Any]:
    """Return a host whose section is certain to fire but guarded by a song gap."""
    host = _must_host()
    host["section_order"] = [
        {
            "when": "between_songs",
            "flow": [
                {
                    "OPTIONAL": {
                        "section": "Song_Transition",
                        "chance": 1.0,
                        "guards": {"min_gap_songs": min_gap_songs},
                    }
                }
            ],
        }
    ]
    return host


def _track(index: int) -> FakeQueueItem:
    """Return a fake music queue item."""
    return FakeQueueItem(f"Artist {index} - Song {index}")


def _dj_clip(gap_next_id: str, session_id: str) -> FakeQueueItem:
    """Return a fake DJ clip queue item announcing the given track."""
    return FakeQueueItem(
        "Song Transition",
        duration=30,
        extra={
            ATTR_QUEUE_DJ: True,
            ATTR_GAP_NEXT_ID: gap_next_id,
            ATTR_SESSION_ID: session_id,
        },
    )


def _make_replan_dj(
    tmp_path: Path,
    items: list[FakeQueueItem],
    current_index: int | None = 0,
    index_in_buffer: int | None = 0,
    host: dict[str, Any] | None = None,
) -> ReplanQueueDJ:
    """Build an armed replan harness around the given queue items."""
    queue = FakeQueue("queue-1", current_index, index_in_buffer)
    dummy = ReplanQueueDJ(tmp_path, queue, items, host or _must_host())
    dummy._arm_dj_state("queue-1", "rick")
    return dummy


async def test_set_queue_dj_enables_and_persists(tmp_path: Path) -> None:
    """Arm a queue DJ, persist it, and reload it into a fresh instance."""
    dummy = DummyQueueDJ(tmp_path)
    mapping = await dummy.set_queue_dj("queue-1", "rick")
    assert mapping == {"queue-1": "rick"}
    assert dummy._dj_queues["queue-1"].host_id == "rick"
    assert dummy._dj_queues["queue-1"].dj_session_id
    assert dummy.replanned == ["queue-1"]
    assert dummy._dj_file.exists()

    fresh = DummyQueueDJ(tmp_path)
    await fresh._load_queue_dj()
    assert fresh._dj_queues["queue-1"].host_id == "rick"


async def test_set_queue_dj_rejects_unknown_host(tmp_path: Path) -> None:
    """Reject arming a queue DJ with an unknown host id."""
    dummy = DummyQueueDJ(tmp_path)
    with pytest.raises(InvalidDataError):
        await dummy.set_queue_dj("queue-1", "nobody")


async def test_set_queue_dj_none_disables(tmp_path: Path) -> None:
    """Disable an armed queue DJ by passing host_id=None."""
    dummy = DummyQueueDJ(tmp_path)
    await dummy.set_queue_dj("queue-1", "rick")
    mapping = await dummy.set_queue_dj("queue-1", None)
    assert mapping == {}
    assert dummy._dj_queues == {}


async def test_status_returns_mapping(tmp_path: Path) -> None:
    """Return the queue-to-host mapping for an armed queue DJ."""
    dummy = DummyQueueDJ(tmp_path)
    await dummy.set_queue_dj("queue-1", "rick")
    assert await dummy.get_queue_dj_status() == {"queue-1": "rick"}


async def test_replan_inserts_clip_between_upcoming_tracks(tmp_path: Path) -> None:
    """Inject one DJ clip into every plannable gap ahead of the playback guards."""
    tracks = [_track(index) for index in range(4)]
    dummy = _make_replan_dj(tmp_path, list(tracks))

    await dummy._replan_queue("queue-1")

    queues = dummy.player_queues
    assert len(queues.loads) == 2
    for queue_items, insert_at_index in queues.loads:
        assert insert_at_index > 1
        clip = queue_items[0]
        assert clip.extra_attributes[ATTR_QUEUE_DJ] is True
        assert clip.extra_attributes[ATTR_HOST_ID] == "rick"
    announced = {items[0].extra_attributes[ATTR_GAP_NEXT_ID] for items, _ in queues.loads}
    assert announced == {tracks[2].queue_item_id, tracks[3].queue_item_id}

    final_items = queues.items("queue-1")
    for index, item in enumerate(final_items):
        if item.extra_attributes.get(ATTR_QUEUE_DJ):
            successor = final_items[index + 1]
            assert successor.queue_item_id == item.extra_attributes[ATTR_GAP_NEXT_ID]
    state = dummy._dj_queues["queue-1"]
    assert state.clip_counter == 2
    assert state.last_planned_item_id == tracks[3].queue_item_id
    assert state.songs_consumed == 1


async def test_replan_skips_gaps_that_already_have_a_clip(tmp_path: Path) -> None:
    """Leave a gap alone when it already holds a DJ clip for the following track."""
    tracks = [_track(index) for index in range(4)]
    dummy = _make_replan_dj(tmp_path, list(tracks))
    state = dummy._dj_queues["queue-1"]
    existing = _dj_clip(tracks[2].queue_item_id, state.dj_session_id)
    dummy.player_queues._items = [tracks[0], tracks[1], existing, tracks[2], tracks[3]]

    await dummy._replan_queue("queue-1")

    queues = dummy.player_queues
    assert queues.deleted == []
    assert len(queues.loads) == 1
    assert queues.loads[0][0][0].extra_attributes[ATTR_GAP_NEXT_ID] == tracks[3].queue_item_id


async def test_replan_repairs_stale_clip_after_reorder(tmp_path: Path) -> None:
    """Delete a DJ clip that no longer sits in front of the track it announces."""
    tracks = [_track(index) for index in range(4)]
    dummy = _make_replan_dj(tmp_path, list(tracks))
    state = dummy._dj_queues["queue-1"]
    stale = _dj_clip("vanished-item", state.dj_session_id)
    dummy.player_queues._items = [tracks[0], tracks[1], stale, tracks[2], tracks[3]]

    await dummy._replan_queue("queue-1")

    queues = dummy.player_queues
    assert queues.deleted == [stale.queue_item_id]
    # the freed gap is plannable again in the same pass
    announced = {items[0].extra_attributes[ATTR_GAP_NEXT_ID] for items, _ in queues.loads}
    assert announced == {tracks[2].queue_item_id, tracks[3].queue_item_id}


async def test_replan_respects_buffer_guard(tmp_path: Path) -> None:
    """Never insert into the gap right after the item already loaded into the buffer."""
    tracks = [_track(index) for index in range(4)]
    dummy = _make_replan_dj(tmp_path, list(tracks), current_index=0, index_in_buffer=1)

    await dummy._replan_queue("queue-1")

    queues = dummy.player_queues
    assert len(queues.loads) == 1
    queue_items, insert_at_index = queues.loads[0]
    assert insert_at_index == 3
    assert queue_items[0].extra_attributes[ATTR_GAP_NEXT_ID] == tracks[3].queue_item_id


async def test_disable_removes_pending_clips_only(tmp_path: Path) -> None:
    """Disabling drops this session's upcoming clips but keeps played and foreign ones."""
    tracks = [_track(index) for index in range(3)]
    dummy = _make_replan_dj(tmp_path, [], current_index=2, index_in_buffer=2)
    state = dummy._dj_queues["queue-1"]
    played_clip = _dj_clip(tracks[1].queue_item_id, state.dj_session_id)
    pending_clip = _dj_clip(tracks[2].queue_item_id, state.dj_session_id)
    foreign_clip = _dj_clip(tracks[2].queue_item_id, "other-session")
    dummy.player_queues._items = [
        tracks[0],
        played_clip,
        tracks[1],
        pending_clip,
        tracks[2],
        foreign_clip,
    ]

    await dummy.set_queue_dj("queue-1", None)

    assert dummy._dj_queues == {}
    assert dummy.player_queues.deleted == [pending_clip.queue_item_id]


async def test_min_gap_songs_guard_holds_across_passes(tmp_path: Path) -> None:
    """Carry the section history across passes so a min_gap_songs guard keeps holding."""
    tracks = [_track(index) for index in range(4)]
    dummy = _make_replan_dj(tmp_path, list(tracks), host=_optional_host(3))

    await dummy._replan_queue("queue-1")

    queues = dummy.player_queues
    assert len(queues.loads) == 1
    assert queues.loads[0][0][0].extra_attributes[ATTR_GAP_NEXT_ID] == tracks[2].queue_item_id

    queues._items.extend([_track(4), _track(5)])
    await dummy._replan_queue("queue-1")

    assert len(queues.loads) == 1


async def test_scheduled_replan_serves_requests_landing_during_a_pass(tmp_path: Path) -> None:
    """A replan request raised by the pass's own inserts is served and then converges."""
    tracks = [_track(index) for index in range(4)]
    dummy = _make_replan_dj(tmp_path, list(tracks))
    queues = dummy.player_queues
    queues.on_load = lambda: dummy._schedule_replan("queue-1")

    dummy._schedule_replan("queue-1")
    await asyncio.gather(*dummy.mass.tasks)

    assert len(queues.loads) == 2
    assert dummy._dj_queues["queue-1"].replan_pending is False


async def test_failing_pass_leaves_the_dj_schedulable(tmp_path: Path) -> None:
    """A raising pass clears its request, does not retry, and recovers on a later event."""
    tracks = [_track(index) for index in range(4)]
    dummy = _make_replan_dj(tmp_path, list(tracks))
    working_build_program = dummy._build_program
    attempts: list[str] = []

    def _failing_build_program(_station: dict[str, Any], host: dict[str, Any]) -> dict[str, Any]:
        attempts.append(host["id"])
        # an event landing mid-pass re-requests a replan behind the still running task
        dummy._schedule_replan("queue-1")
        raise MusicAssistantError("misconfigured host")

    dummy._build_program = _failing_build_program  # type: ignore[method-assign]
    dummy._schedule_replan("queue-1")
    await asyncio.gather(*dummy.mass.tasks)

    assert attempts == ["rick"]
    assert dummy.player_queues.loads == []
    assert dummy._dj_queues["queue-1"].replan_pending is False

    dummy._build_program = working_build_program  # type: ignore[method-assign]
    dummy._schedule_replan("queue-1")
    await asyncio.gather(*dummy.mass.tasks)

    assert len(dummy.player_queues.loads) == 2


async def test_failing_pass_clears_the_state_the_queue_was_rearmed_with(tmp_path: Path) -> None:
    """A re-arm during a failing pass must leave the new state schedulable, not the old one."""
    tracks = [_track(index) for index in range(4)]
    dummy = _make_replan_dj(tmp_path, list(tracks))
    old_state = dummy._dj_queues["queue-1"]
    working_build_program = dummy._build_program

    def _failing_build_program(_station: dict[str, Any], host: dict[str, Any]) -> dict[str, Any]:
        # set_queue_dj re-arms the queue mid-pass and latches the fresh state's request
        # onto the still running task
        dummy._arm_dj_state("queue-1", host["id"])
        dummy._schedule_replan("queue-1")
        raise MusicAssistantError("misconfigured host")

    dummy._build_program = _failing_build_program  # type: ignore[method-assign]
    dummy._schedule_replan("queue-1")
    await asyncio.gather(*dummy.mass.tasks)

    new_state = dummy._dj_queues["queue-1"]
    assert new_state is not old_state
    assert new_state.replan_pending is False

    dummy._build_program = working_build_program  # type: ignore[method-assign]
    dummy._schedule_replan("queue-1")
    await asyncio.gather(*dummy.mass.tasks)

    assert len(dummy.player_queues.loads) == 2


async def test_unloading_provider_schedules_no_replans(tmp_path: Path) -> None:
    """An unloading provider starts no new replan work."""
    dummy = _make_replan_dj(tmp_path, [_track(index) for index in range(4)])
    dummy._unloading = True

    dummy._schedule_replan("queue-1")

    assert dummy.mass.tasks == []
    state = dummy._dj_queues["queue-1"]
    assert state.replan_pending is False
    assert state.task is None


async def test_injection_rereads_a_guard_that_moved_during_the_pass(tmp_path: Path) -> None:
    """A guard that advanced while the pass awaited is honoured by the injections."""
    tracks = [_track(index) for index in range(4)]
    dummy = _make_replan_dj(tmp_path, list(tracks), current_index=0, index_in_buffer=0)
    prepare_runtime_tokens = dummy._prepare_runtime_tokens

    async def _slow_prepare(program: dict[str, Any]) -> dict[str, str]:
        # stands in for a slow token source: the player buffers ahead while we wait
        dummy.player_queues._queue.index_in_buffer = 1
        return await prepare_runtime_tokens(program)

    dummy._prepare_runtime_tokens = _slow_prepare  # type: ignore[method-assign]
    await dummy._replan_queue("queue-1")

    queues = dummy.player_queues
    assert len(queues.loads) == 1
    assert queues.loads[0][0][0].extra_attributes[ATTR_GAP_NEXT_ID] == tracks[3].queue_item_id
