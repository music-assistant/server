"""Unit tests for the AI Radio sticky queue DJ state."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Coroutine
from itertools import pairwise
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import Mock

import pytest
from music_assistant_models.enums import EventType, MediaType
from music_assistant_models.errors import InvalidDataError, MusicAssistantError

from music_assistant.providers.ai_radio.constants import (
    ATTR_GAP_NEXT_ID,
    ATTR_HOST_ID,
    ATTR_QUEUE_DJ,
    ATTR_SESSION_ID,
)
from music_assistant.providers.ai_radio.media import _ShowRun
from music_assistant.providers.ai_radio.models import PlannedSection
from music_assistant.providers.ai_radio.queue_dj import AIRadioQueueDJMixin
from music_assistant.providers.ai_radio.runtime import AIRadioRuntimeMixin
from music_assistant.providers.ai_radio.storage import AIRadioStorageMixin


class FakeQueue:
    """Minimal PlayerQueue stand-in."""

    def __init__(
        self,
        queue_id: str,
        current_index: int | None,
        index_in_buffer: int | None,
        sources: list[Any] | None = None,
        ended: bool = False,
    ) -> None:
        """Initialize the fake queue with its playback pointers."""
        self.queue_id = queue_id
        self.current_index = current_index
        self.index_in_buffer = index_in_buffer
        self.sources = sources or []
        self.ended = ended


class FakeQueueItem:
    """Minimal QueueItem stand-in."""

    _counter = 0

    def __init__(
        self, name: str, duration: int | None = 200, extra: dict[str, Any] | None = None
    ) -> None:
        """Initialize the fake queue item with a unique id."""
        FakeQueueItem._counter += 1
        self.queue_item_id = f"qi{FakeQueueItem._counter}"
        # mirrors the insert order a real queue restores when it is un-shuffled
        self.sort_index = FakeQueueItem._counter
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
        # what each update_items call added, as (items, index), and what it dropped. the real
        # controller only ever replaces the whole list, so both are derived from the diff
        self.loads: list[tuple[list[Any], int]] = []
        self.deleted: list[str] = []
        self.update_calls = 0
        # stands in for the QUEUE_ITEMS_UPDATED event a real update_items emits
        self.on_items_updated: Callable[[], None] | None = None

    def get(self, queue_id: str) -> FakeQueue | None:
        """Return the queue when the id matches."""
        return self._queue if queue_id == self._queue.queue_id else None

    def items(self, queue_id: str, limit: int = 500, offset: int = 0) -> list[Any]:
        """Return one page of queue items."""
        return self._items[offset : offset + limit]

    def update_items(self, queue_id: str, queue_items: list[Any]) -> None:
        """Replace the queue items with the given list."""
        self.update_calls += 1
        previous_ids = {item.queue_item_id for item in self._items}
        current_ids = {item.queue_item_id for item in queue_items}
        self.deleted.extend(
            item.queue_item_id for item in self._items if item.queue_item_id not in current_ids
        )
        self.loads.extend(
            ([item], index)
            for index, item in enumerate(queue_items)
            if item.queue_item_id not in previous_ids
        )
        self._items = queue_items
        if self.on_items_updated is not None:
            self.on_items_updated()


class FakeMass:
    """Minimal MusicAssistant stand-in for the queue DJ mixin."""

    def __init__(self, player_queues: FakePlayerQueues) -> None:
        """Initialize with the fake player queues controller."""
        self.player_queues = player_queues
        self.tasks: list[asyncio.Task[Any]] = []
        self._tasks_by_id: dict[str, asyncio.Task[Any]] = {}

    def create_task(
        self, target: Coroutine[Any, Any, Any], task_id: str | None = None
    ) -> asyncio.Task[Any]:
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
        self._stations: dict[str, dict[str, Any]] = {}
        self._show_runs: dict[str, _ShowRun] = {}
        self._dj_queues: dict[str, Any] = {}
        self._dj_file = tmp_path / "queue_dj.json"
        self._dj_lock = asyncio.Lock()
        # the disable path reaches the real clip cleanup, which needs a queue layer;
        # this one holds no queue at all, so cleanup finds nothing to do
        # the mixin declares `mass: MusicAssistant`; a fake stands in for tests, so its own
        # attributes (not MusicAssistant's) are what callers here actually see
        self.mass: FakeMass = FakeMass(  # type: ignore[assignment]
            FakePlayerQueues(FakeQueue("other-queue", None, None), [])
        )

    def _schedule_replan(self, queue_id: str) -> None:
        """Record replan requests instead of running them."""
        self.replanned = getattr(self, "replanned", [])
        self.replanned.append(queue_id)

    def _end_show_run(self, station_id: str) -> None:
        """Drop the station's run, mirroring the real media mixin's behaviour."""
        self._show_runs.pop(station_id, None)


def _dj_harness(tmp_path: Path) -> DummyQueueDJ:
    """Build a queue DJ harness with a morning_show station bound to host amy."""
    dj = DummyQueueDJ(tmp_path)
    dj._hosts["amy"] = {"id": "amy", "name": "Amy", "instructions": "x", "tts_engine": ""}
    dj._stations["morning_show"] = {"id": "morning_show", "host_id": "amy"}
    return dj


def _fake_queue_with_sources(dj: DummyQueueDJ, queue_id: str, source_uris: list[str]) -> FakeQueue:
    """Register a fake queue exposing the given source uris on the harness's player_queues."""
    sources = [SimpleNamespace(uri=uri) for uri in source_uris]
    queue = FakeQueue(queue_id, None, None, sources=sources)
    dj.mass.player_queues._queue = queue
    return queue


def _seed_show_run(dj: DummyQueueDJ, station_id: str) -> _ShowRun:
    """Register an in-flight, not-yet-armed show run for the given station."""
    run = _ShowRun(tracks=[])
    dj._show_runs[station_id] = run
    return run


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
        self._stations: dict[str, dict[str, Any]] = {}
        self._show_runs: dict[str, _ShowRun] = {}
        self._dj_queues: dict[str, Any] = {}
        self._dj_file = tmp_path / "queue_dj.json"
        self._dj_lock = asyncio.Lock()
        self._unloading = False
        self.player_queues = FakePlayerQueues(queue, items)
        # see DummyQueueDJ.__init__ for why this needs its own annotation
        self.mass: FakeMass = FakeMass(self.player_queues)  # type: ignore[assignment]


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


def _hourly_sections() -> list[dict[str, Any]]:
    """Return two once-per-hour sections plus the ai_meta section that merges them."""
    return [
        {
            "id": "Weather",
            "name": "Weather",
            "type": "ai_text",
            "web_search": "disabled",
            "prompt": "Give the forecast",
            "constraints": {"max_chars": 200},
        },
        {
            "id": "News",
            "name": "News",
            "type": "ai_text",
            "web_search": "disabled",
            "prompt": "Give the headlines",
            "constraints": {"max_chars": 200},
        },
        {
            "id": "Smoother",
            "name": "Between Songs Mix",
            "type": "ai_meta",
            "prompt": "Combine these: <section_drafts>",
        },
    ]


def _hourly_host() -> dict[str, Any]:
    """Return a host whose two hourly sections merge into a single clip per gap."""
    host = _must_host()
    host["section_ids"] = ["Weather", "News", "Smoother"]
    host["section_order"] = [
        {
            "when": "between_songs",
            "flow": [
                {
                    "OPTIONAL": {
                        "section": "Weather",
                        "chance": 1.0,
                        "guards": {"max_per_60min": 1},
                    }
                },
                {
                    "OPTIONAL": {"section": "News", "chance": 1.0, "guards": {"max_per_60min": 1}},
                },
            ],
        }
    ]
    host["merge_section_id"] = "Smoother"
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
    # set_queue_dj marks a state plannable once its clip cleanup ran, and these harnesses
    # start from a queue whose switch already settled
    dummy._arm_dj_state("queue-1", "rick").ready = True
    return dummy


async def test_set_queue_dj_enables_and_persists(tmp_path: Path) -> None:
    """Arm a queue DJ, persist it, and reload it into a fresh instance."""
    dummy = DummyQueueDJ(tmp_path)
    mapping = await dummy.set_queue_dj("queue-1", "rick")
    assert mapping == {"queue-1": {"host_id": "rick", "station_id": ""}}
    assert dummy._dj_queues["queue-1"].host_id == "rick"
    assert dummy._dj_queues["queue-1"].dj_session_id
    assert dummy._dj_queues["queue-1"].ready is True
    assert dummy.replanned == ["queue-1"]
    assert dummy._dj_file.exists()

    fresh = DummyQueueDJ(tmp_path)
    await fresh._load_queue_dj()
    assert fresh._dj_queues["queue-1"].host_id == "rick"
    # nothing has to be cleaned up before a boot arm, so it may plan on the first event
    assert fresh._dj_queues["queue-1"].ready is True


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
    assert await dummy.get_queue_dj_status() == {"queue-1": {"host_id": "rick", "station_id": ""}}


async def test_ensure_show_dj_arms_host_for_show_queue(tmp_path: Path) -> None:
    """Playing a show with no DJ armed yet arms its station's host, once a run exists."""
    dj = _dj_harness(tmp_path)
    _fake_queue_with_sources(dj, "q1", [f"{dj.instance_id}://radio/morning_show"])
    run = _seed_show_run(dj, "morning_show")

    await dj._ensure_show_dj("q1")

    state = dj._dj_queues["q1"]
    assert state.host_id == "amy"
    assert state.station_id == "morning_show"
    assert run.dj_armed is True
    assert run.queue_id == "q1"


async def test_ensure_show_dj_does_not_arm_before_a_run_exists(tmp_path: Path) -> None:
    """A show queue with no fetched-tracks run yet is left alone (nothing to bind against)."""
    dj = _dj_harness(tmp_path)
    _fake_queue_with_sources(dj, "q1", [f"{dj.instance_id}://radio/morning_show"])

    await dj._ensure_show_dj("q1")

    assert "q1" not in dj._dj_queues


async def test_ensure_show_dj_never_overwrites_manual_host(tmp_path: Path) -> None:
    """A manually picked host survives; only the station binding is restored."""
    dj = _dj_harness(tmp_path)
    _fake_queue_with_sources(dj, "q1", [f"{dj.instance_id}://radio/morning_show"])
    state = dj._arm_dj_state("q1", "bob")
    state.ready = True

    await dj._ensure_show_dj("q1")

    assert dj._dj_queues["q1"].host_id == "bob"
    assert dj._dj_queues["q1"].station_id == "morning_show"


async def test_auto_armed_dj_detaches_and_ends_run_when_source_gone(tmp_path: Path) -> None:
    """An auto-armed DJ whose show left the queue's sources is disarmed and its run ended."""
    dj = _dj_harness(tmp_path)
    _fake_queue_with_sources(dj, "q1", [])
    state = dj._arm_dj_state("q1", "amy")
    state.ready = True
    state.station_id = "morning_show"
    dj._end_show_run = Mock()  # type: ignore[method-assign]

    await dj._maybe_detach_show_dj("q1")

    assert "q1" not in dj._dj_queues
    dj._end_show_run.assert_called_once_with("morning_show")


async def test_manually_armed_dj_is_never_auto_detached(tmp_path: Path) -> None:
    """A DJ with no station binding (a manual pick) is left alone by the auto-detach."""
    dj = _dj_harness(tmp_path)
    _fake_queue_with_sources(dj, "q1", [])
    state = dj._arm_dj_state("q1", "amy")
    state.ready = True

    await dj._maybe_detach_show_dj("q1")

    assert "q1" in dj._dj_queues


async def test_status_includes_station_binding(tmp_path: Path) -> None:
    """The status payload surfaces the station a queue DJ is bound to."""
    dj = _dj_harness(tmp_path)
    state = dj._arm_dj_state("q1", "amy")
    state.station_id = "morning_show"

    status = await dj.get_queue_dj_status()

    assert status == {"q1": {"host_id": "amy", "station_id": "morning_show"}}


async def test_on_dj_queue_event_auto_arms_a_show_queue(tmp_path: Path) -> None:
    """A queue event for a freshly loaded show arms its host without an explicit dj call."""
    dj = _dj_harness(tmp_path)
    _fake_queue_with_sources(dj, "q1", [f"{dj.instance_id}://radio/morning_show"])
    _seed_show_run(dj, "morning_show")

    await dj._on_dj_queue_event(
        cast("Any", SimpleNamespace(event=EventType.QUEUE_ADDED, object_id="q1"))
    )

    state = dj._dj_queues["q1"]
    assert state.host_id == "amy"
    assert state.station_id == "morning_show"
    # armed twice: once by set_queue_dj's own arm, once by the event's usual replan request
    assert dj.replanned == ["q1", "q1"]


async def test_on_dj_queue_event_does_not_rearm_an_ended_show_queue(tmp_path: Path) -> None:
    """An ended queue keeps its sources; detaching must not flap-rearm on the next event."""
    dj = _dj_harness(tmp_path)
    queue = _fake_queue_with_sources(dj, "q1", [f"{dj.instance_id}://radio/morning_show"])
    run = _seed_show_run(dj, "morning_show")
    dj._end_show_run = Mock(wraps=dj._end_show_run)  # type: ignore[method-assign]

    # first pass: arms, then immediately detaches because the queue already ended
    queue.ended = True
    await dj._on_dj_queue_event(
        cast("Any", SimpleNamespace(event=EventType.QUEUE_ITEMS_UPDATED, object_id="q1"))
    )
    assert "q1" not in dj._dj_queues
    dj._end_show_run.assert_called_once_with("morning_show")
    assert run.dj_armed is True  # the popped run itself stays armed; it is simply gone

    # a later event on the same still-ended queue must not re-arm: the run is gone
    await dj._on_dj_queue_event(
        cast("Any", SimpleNamespace(event=EventType.QUEUE_UPDATED, object_id="q1"))
    )
    assert "q1" not in dj._dj_queues
    dj._end_show_run.assert_called_once_with("morning_show")


async def test_manual_disable_mid_show_is_not_rearmed(tmp_path: Path) -> None:
    """A user disabling the DJ mid-show must not get it re-armed by the next queue event."""
    dj = _dj_harness(tmp_path)
    _fake_queue_with_sources(dj, "q1", [f"{dj.instance_id}://radio/morning_show"])
    run = _seed_show_run(dj, "morning_show")

    await dj._ensure_show_dj("q1")
    assert "q1" in dj._dj_queues

    # the user turns the DJ off from the player menu; the show keeps playing (run alive)
    await dj.set_queue_dj("q1", None)
    assert "q1" not in dj._dj_queues

    await dj._on_dj_queue_event(
        cast("Any", SimpleNamespace(event=EventType.QUEUE_ITEMS_UPDATED, object_id="q1"))
    )

    assert "q1" not in dj._dj_queues
    assert run.dj_armed is True


async def test_replaying_a_show_arms_again_on_a_fresh_run(tmp_path: Path) -> None:
    """A brand new run (after _end_show_run) is armed again, even for the same station."""
    dj = _dj_harness(tmp_path)
    _fake_queue_with_sources(dj, "q1", [f"{dj.instance_id}://radio/morning_show"])
    old_run = _seed_show_run(dj, "morning_show")
    await dj._ensure_show_dj("q1")
    assert "q1" in dj._dj_queues
    assert old_run.dj_armed is True

    dj._end_show_run("morning_show")
    await dj.set_queue_dj("q1", None)
    assert "q1" not in dj._dj_queues

    new_run = _seed_show_run(dj, "morning_show")
    await dj._ensure_show_dj("q1")

    assert "q1" in dj._dj_queues
    assert dj._dj_queues["q1"].host_id == "amy"
    assert new_run.dj_armed is True


async def test_on_dj_queue_event_ends_run_for_bound_station_on_player_removed(
    tmp_path: Path,
) -> None:
    """Removing the player behind a show-bound DJ also ends that station's run."""
    dj = _dj_harness(tmp_path)
    _fake_queue_with_sources(dj, "q1", [])
    state = dj._arm_dj_state("q1", "amy")
    state.station_id = "morning_show"
    dj._end_show_run = Mock()  # type: ignore[method-assign]

    await dj._on_dj_queue_event(
        cast("Any", SimpleNamespace(event=EventType.PLAYER_REMOVED, object_id="q1"))
    )

    assert "q1" not in dj._dj_queues
    dj._end_show_run.assert_called_once_with("morning_show")


async def test_repair_never_treats_a_trailing_outro_as_stale(tmp_path: Path) -> None:
    """An outro clip carries no gap-next id, so repair must not delete it for lacking one."""
    tracks = [_track(index) for index in range(2)]
    dummy = _make_replan_dj(tmp_path, list(tracks))
    state = dummy._dj_queues["queue-1"]
    outro = FakeQueueItem(
        "Song Transition",
        duration=30,
        extra={ATTR_QUEUE_DJ: True, ATTR_SESSION_ID: state.dj_session_id},
    )
    dummy.player_queues._items = [*tracks, outro]

    repaired = dummy._repair_dj_clips("queue-1", state, dummy.player_queues.items("queue-1"), -1)

    assert repaired is False
    assert dummy.player_queues.deleted == []


async def test_splice_dj_clip_inserts_after_the_last_window_track(tmp_path: Path) -> None:
    """An end_of_playlist section splices its clip after the target, not before it."""
    tracks = [_track(index) for index in range(2)]
    dummy = _make_replan_dj(tmp_path, list(tracks))
    state = dummy._dj_queues["queue-1"]
    program = dummy._build_program({"id": "", "name": "AI DJ Rick"}, dummy._hosts["rick"])
    section = PlannedSection(
        order=0,
        clip_id="",
        section_id="Song_Transition",
        section_name="Song Transition",
        when="end_of_playlist",
        insert_at_index=len(tracks),
        prompt="",
        max_chars=200,
        web_search_mode="disabled",
    )
    items = list(dummy.player_queues.items("queue-1"))

    outcome = dummy._splice_dj_clip(
        queue_id="queue-1",
        items=items,
        guard_index=-1,
        state=state,
        program=program,
        target={"item_id": tracks[-1].queue_item_id},
        section=section,
        after_target=True,
    )

    assert outcome == "injected"
    assert items[-1].extra_attributes[ATTR_QUEUE_DJ] is True
    assert items[-1].extra_attributes.get(ATTR_GAP_NEXT_ID) is None


async def test_replan_plans_an_outro_when_the_show_run_is_exhausted(tmp_path: Path) -> None:
    """A replan plans and splices an outro once the bound show's run is fully served."""
    tracks = [_track(index) for index in range(2)]
    host = _must_host()
    host["section_order"] = [{"when": "end_of_playlist", "flow": [{"MUST": "Song_Transition"}]}]
    dummy = _make_replan_dj(tmp_path, list(tracks), current_index=-1, index_in_buffer=-1, host=host)
    state = dummy._dj_queues["queue-1"]
    state.station_id = "station_a"
    dummy._show_runs["station_a"] = _ShowRun(tracks=[], cursor=0)

    await dummy._replan_queue("queue-1")

    final_items = dummy.player_queues.items("queue-1")
    assert final_items[-1].extra_attributes[ATTR_QUEUE_DJ] is True
    assert final_items[-1].extra_attributes.get(ATTR_GAP_NEXT_ID) is None


async def test_replan_splices_both_intro_and_outro_in_one_pass(tmp_path: Path) -> None:
    """
    A short, fully-loaded, exhausted show plans and splices both intro and outro at once.

    Regression test: the splice loop processes sections in descending insert_at_index order, so
    the outro (the higher index) lands before the intro. The intro's occupied check must not
    read a negative index (which would wrap around to the just-spliced outro at the tail) and
    wrongly reject the intro as occupied.
    """
    tracks = [_track(index) for index in range(2)]
    host = _must_host()
    host["section_order"] = [
        {"when": "start_of_playlist", "flow": [{"MUST": "Song_Transition"}]},
        {"when": "end_of_playlist", "flow": [{"MUST": "Song_Transition"}]},
    ]
    dummy = _make_replan_dj(
        tmp_path, list(tracks), current_index=None, index_in_buffer=None, host=host
    )
    state = dummy._dj_queues["queue-1"]
    state.station_id = "station_a"
    dummy._show_runs["station_a"] = _ShowRun(tracks=[], cursor=0)

    await dummy._replan_queue("queue-1")

    final_items = dummy.player_queues.items("queue-1")
    assert len(final_items) == 4  # intro clip, both tracks, outro clip
    assert final_items[0].extra_attributes[ATTR_QUEUE_DJ] is True
    assert final_items[0].extra_attributes[ATTR_GAP_NEXT_ID] == tracks[0].queue_item_id
    assert final_items[-1].extra_attributes[ATTR_QUEUE_DJ] is True
    assert final_items[-1].extra_attributes.get(ATTR_GAP_NEXT_ID) is None


def _intro_host() -> dict[str, Any]:
    """Return a host that always plans a section at the start of the playlist."""
    host = _must_host()
    host["section_order"] = [{"when": "start_of_playlist", "flow": [{"MUST": "Song_Transition"}]}]
    return host


async def test_replan_plans_the_intro_before_playback_starts(tmp_path: Path) -> None:
    """The start_of_playlist slot is offered while the queue has not started playing."""
    tracks = [_track(index) for index in range(3)]
    dummy = _make_replan_dj(
        tmp_path, list(tracks), current_index=None, index_in_buffer=None, host=_intro_host()
    )

    await dummy._replan_queue("queue-1")

    queues = dummy.player_queues
    assert len(queues.loads) == 1
    assert queues.loads[0][0][0].extra_attributes[ATTR_GAP_NEXT_ID] == tracks[0].queue_item_id


async def test_replan_skips_the_intro_once_playback_started(tmp_path: Path) -> None:
    """The start_of_playlist slot is withdrawn once playback won the race to start."""
    tracks = [_track(index) for index in range(3)]
    dummy = _make_replan_dj(
        tmp_path, list(tracks), current_index=0, index_in_buffer=0, host=_intro_host()
    )

    await dummy._replan_queue("queue-1")

    # playback already owns the first track, so the intro lost the race and is skipped
    assert dummy.player_queues.loads == []


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
    assert state.decided_gap_ids == {tracks[2].queue_item_id, tracks[3].queue_item_id}
    assert state.songs_before_window == 1


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


async def test_disable_removes_all_pending_clips(tmp_path: Path) -> None:
    """Disabling drops every upcoming DJ clip whatever session it came from, played ones stay."""
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
    assert dummy.player_queues.deleted == [pending_clip.queue_item_id, foreign_clip.queue_item_id]


async def test_switch_removes_clips_of_a_session_no_state_ever_knew(tmp_path: Path) -> None:
    """Clear a clip left behind by an earlier provider run, whose session id nothing remembers."""
    tracks = [_track(index) for index in range(4)]
    dummy = _make_replan_dj(tmp_path, [])
    stale = _dj_clip(tracks[2].queue_item_id, "dj_from_a_previous_run")
    dummy.player_queues._items = [tracks[0], tracks[1], stale, tracks[2], tracks[3]]

    daisy = _must_host()
    daisy["id"] = "daisy"
    dummy._hosts["daisy"] = daisy
    await dummy.set_queue_dj("queue-1", "daisy")

    assert stale.queue_item_id in dummy.player_queues.deleted
    await asyncio.gather(*dummy.mass.tasks)


async def test_cleanup_keeps_the_freshly_armed_sessions_clips(tmp_path: Path) -> None:
    """A clip the newly armed session already injected survives the previous host's cleanup."""
    tracks = [_track(index) for index in range(4)]
    dummy = _make_replan_dj(tmp_path, [])
    old_state = dummy._dj_queues["queue-1"]
    old_clip = _dj_clip(tracks[2].queue_item_id, old_state.dj_session_id)
    dummy.player_queues._items = [tracks[0], tracks[1], old_clip, tracks[2], tracks[3]]
    write_queue_dj = dummy._write_queue_dj

    async def _write_and_inject_racing_clip() -> None:
        # stands in for a replan of the freshly armed session landing a clip in the window
        # before the cleanup of the previous host's clips got its turn
        await write_queue_dj()
        session_id = dummy._dj_queues["queue-1"].dj_session_id
        dummy.player_queues._items.insert(4, _dj_clip(tracks[3].queue_item_id, session_id))

    dummy._write_queue_dj = _write_and_inject_racing_clip  # type: ignore[method-assign]
    daisy = _must_host()
    daisy["id"] = "daisy"
    dummy._hosts["daisy"] = daisy

    await dummy.set_queue_dj("queue-1", "daisy")

    assert dummy.player_queues.deleted == [old_clip.queue_item_id]
    await asyncio.gather(*dummy.mass.tasks)


async def test_switching_host_removes_old_hosts_pending_clips(tmp_path: Path) -> None:
    """Switching the sticky DJ to a new host clears the old host's unplayed clips."""
    tracks = [_track(index) for index in range(4)]
    dummy = _make_replan_dj(tmp_path, list(tracks))
    old_state = dummy._dj_queues["queue-1"]
    await dummy._replan_queue("queue-1")
    queues = dummy.player_queues
    old_clip_ids = {
        item.queue_item_id
        for item in queues.items("queue-1")
        if item.extra_attributes.get(ATTR_QUEUE_DJ)
    }
    assert old_clip_ids  # sanity: rick's clips actually landed

    daisy = _must_host()
    daisy["id"] = "daisy"
    daisy["name"] = "Daisy"
    dummy._hosts["daisy"] = daisy

    await dummy.set_queue_dj("queue-1", "daisy")
    await asyncio.gather(*dummy.mass.tasks)

    # rick's pending clips were removed rather than left to render under his persona
    assert old_clip_ids <= set(queues.deleted)
    remaining_dj_clips = [
        item for item in queues.items("queue-1") if item.extra_attributes.get(ATTR_QUEUE_DJ)
    ]
    assert remaining_dj_clips
    for clip in remaining_dj_clips:
        assert clip.extra_attributes[ATTR_HOST_ID] == "daisy"
        assert clip.extra_attributes[ATTR_SESSION_ID] != old_state.dj_session_id


async def test_min_gap_songs_guard_holds_across_passes(tmp_path: Path) -> None:
    """Carry the section history across passes so a min_gap_songs guard keeps holding."""
    tracks = [_track(index) for index in range(4)]
    dummy = _make_replan_dj(tmp_path, list(tracks), host=_optional_host(3))

    await dummy._replan_queue("queue-1")

    queues = dummy.player_queues
    assert len(queues.loads) == 1
    assert queues.loads[0][0][0].extra_attributes[ATTR_GAP_NEXT_ID] == tracks[2].queue_item_id

    extra = [_track(4), _track(5)]
    queues._items.extend(extra)
    await dummy._replan_queue("queue-1")

    # the clip announced track 2, so the next one may land no earlier than track 5
    assert len(queues.loads) == 2
    assert queues.loads[1][0][0].extra_attributes[ATTR_GAP_NEXT_ID] == extra[1].queue_item_id


async def test_planning_axis_stays_anchored_to_real_queue_positions(tmp_path: Path) -> None:
    """Growing the queue tail must not rewind the song axis the guards count on."""
    tracks = [_track(index) for index in range(4)]
    dummy = _make_replan_dj(tmp_path, list(tracks))

    await dummy._replan_queue("queue-1")
    state = dummy._dj_queues["queue-1"]
    # the window opened on track 1, so track 0 is the only song behind it
    assert state.songs_before_window == 1
    assert [song for song, _minute in state.history["Song_Transition"]] == [2, 3]

    dummy.player_queues._items.extend([_track(4), _track(5)])
    await dummy._replan_queue("queue-1")

    # the window still opens on track 1, so the appended tracks keep their absolute positions
    assert state.songs_before_window == 1
    assert [song for song, _minute in state.history["Song_Transition"]] == [2, 3, 4, 5]


async def test_forward_axis_progress_leaves_history_untouched(tmp_path: Path) -> None:
    """A pass whose planning axis only moves forward must not touch the guard history."""
    tracks = [_track(index) for index in range(6)]
    dummy = _make_replan_dj(tmp_path, list(tracks), host=_optional_host(1))

    await dummy._replan_queue("queue-1")
    state = dummy._dj_queues["queue-1"]
    assert state.songs_before_window == 1
    history_before = {section_id: list(events) for section_id, events in state.history.items()}
    assert history_before  # sanity: something is actually there to protect

    dummy.player_queues._queue.current_index = 1
    dummy.player_queues._queue.index_in_buffer = 1
    await dummy._replan_queue("queue-1")

    # more songs sit behind the window now, yet no event shifted with it
    assert state.songs_before_window == 2
    assert state.history == history_before


async def test_queue_clear_and_refill_speaks_again(tmp_path: Path) -> None:
    """A queue clear followed by a refill must not leave the DJ permanently muted."""
    tracks = [_track(index) for index in range(10)]
    dummy = _make_replan_dj(
        tmp_path, list(tracks), current_index=5, index_in_buffer=5, host=_optional_host(3)
    )

    await dummy._replan_queue("queue-1")
    queues = dummy.player_queues
    assert queues.loads
    loaded_before_clear = len(queues.loads)

    # clearing the queue leaves nothing to plan against, so the pass is a no-op
    queues._items = []
    queues._queue.current_index = None
    queues._queue.index_in_buffer = None
    await dummy._replan_queue("queue-1")
    assert len(queues.loads) == loaded_before_clear

    fresh = [_track(index) for index in range(8)]
    queues._items = fresh
    queues._queue.current_index = 0
    queues._queue.index_in_buffer = 0
    await dummy._replan_queue("queue-1")

    new_loads = queues.loads[loaded_before_clear:]
    assert new_loads
    # the discarded break never aired, so nothing but the buffer guard holds the DJ back
    assert new_loads[0][0][0].extra_attributes[ATTR_GAP_NEXT_ID] == fresh[2].queue_item_id


async def test_clear_and_refill_at_the_same_position_speaks_again(tmp_path: Path) -> None:
    """A clear and refill that lands the player back on the same position must not mute the DJ."""
    tracks = [_track(index) for index in range(10)]
    dummy = _make_replan_dj(tmp_path, list(tracks), host=_optional_host(3))

    await dummy._replan_queue("queue-1")
    queues = dummy.player_queues
    assert queues.loads
    loaded_before_clear = len(queues.loads)

    queues._items = []
    queues._queue.current_index = None
    queues._queue.index_in_buffer = None
    await dummy._replan_queue("queue-1")

    # same track count behind the window as before, so the offsets cannot tell them apart
    fresh = [_track(index) for index in range(8)]
    queues._items = fresh
    queues._queue.current_index = 0
    queues._queue.index_in_buffer = 0
    await dummy._replan_queue("queue-1")

    new_loads = queues.loads[loaded_before_clear:]
    assert new_loads
    assert new_loads[0][0][0].extra_attributes[ATTR_GAP_NEXT_ID] == fresh[2].queue_item_id


async def test_a_break_that_aired_still_holds_the_dj_back_after_a_clear(tmp_path: Path) -> None:
    """A break the listener actually heard keeps its distance across a clear and refill."""
    tracks = [_track(index) for index in range(10)]
    dummy = _make_replan_dj(tmp_path, list(tracks), host=_optional_host(3))

    await dummy._replan_queue("queue-1")
    queues = dummy.player_queues
    # playback reaches the track the first break announced, so that break has aired
    clip = next(item for item in queues._items if item.extra_attributes.get(ATTR_QUEUE_DJ))
    aired_index = next(
        index
        for index, item in enumerate(queues._items)
        if item.queue_item_id == clip.extra_attributes[ATTR_GAP_NEXT_ID]
    )
    queues._queue.current_index = aired_index
    queues._queue.index_in_buffer = aired_index
    await dummy._replan_queue("queue-1")
    loaded_before_clear = len(queues.loads)

    queues._items = []
    queues._queue.current_index = None
    queues._queue.index_in_buffer = None
    await dummy._replan_queue("queue-1")

    fresh = [_track(index) for index in range(10)]
    queues._items = fresh
    queues._queue.current_index = 0
    queues._queue.index_in_buffer = 0
    await dummy._replan_queue("queue-1")

    new_loads = queues.loads[loaded_before_clear:]
    assert new_loads
    # the aired break moved with the queue, so its guard still applies from the new start:
    # it sat one song behind the window and keeps that distance on the fresh queue
    assert new_loads[0][0][0].extra_attributes[ATTR_GAP_NEXT_ID] == fresh[3].queue_item_id


async def test_a_pending_break_at_the_buffer_edge_holds_nothing_after_a_clear(
    tmp_path: Path,
) -> None:
    """A clear that discards a break sitting right at the buffer edge frees its budget too."""
    tracks = [_track(index) for index in range(10)]
    dummy = _make_replan_dj(tmp_path, list(tracks), host=_optional_host(3))

    await dummy._replan_queue("queue-1")
    queues = dummy.player_queues
    # playback advances to the track right before the first staged clip, so the clip sits
    # one slot past the buffer and its event lands exactly on the recorded window start
    queues._queue.current_index = 1
    queues._queue.index_in_buffer = 1
    await dummy._replan_queue("queue-1")
    state = dummy._dj_queues["queue-1"]
    assert state.songs_before_window == 2
    loaded_before_clear = len(queues.loads)

    queues._items = []
    queues._queue.current_index = None
    queues._queue.index_in_buffer = None
    await dummy._replan_queue("queue-1")

    fresh = [_track(index) for index in range(8)]
    queues._items = fresh
    queues._queue.current_index = 0
    queues._queue.index_in_buffer = 0
    await dummy._replan_queue("queue-1")

    new_loads = queues.loads[loaded_before_clear:]
    assert new_loads
    # the discarded clip never aired, so nothing holds the DJ past the first allowed gap
    assert new_loads[0][0][0].extra_attributes[ATTR_GAP_NEXT_ID] == fresh[2].queue_item_id


async def test_an_airing_break_still_holds_the_dj_back_when_the_tail_is_swapped(
    tmp_path: Path,
) -> None:
    """A break the player owns keeps its guard weight when every upcoming track is swapped."""
    tracks = [_track(index) for index in range(10)]
    dummy = _make_replan_dj(tmp_path, list(tracks), host=_optional_host(3))

    await dummy._replan_queue("queue-1")
    queues = dummy.player_queues
    # the first staged clip reaches the player and starts airing
    clip_index = next(
        index
        for index, item in enumerate(queues._items)
        if item.extra_attributes.get(ATTR_QUEUE_DJ)
    )
    queues._queue.current_index = clip_index
    queues._queue.index_in_buffer = clip_index
    await dummy._replan_queue("queue-1")
    state = dummy._dj_queues["queue-1"]
    assert state.songs_before_window == 2
    loaded_before_swap = len(queues.loads)

    # everything behind the airing clip is swapped out, so no decided track is left
    head = queues._items[: clip_index + 1]
    fresh = [_track(20 + index) for index in range(8)]
    queues._items = [*head, *fresh]
    await dummy._replan_queue("queue-1")

    new_loads = queues.loads[loaded_before_swap:]
    assert new_loads
    # the listener is hearing that break right now, so the guard clears three songs later
    assert new_loads[0][0][0].extra_attributes[ATTR_GAP_NEXT_ID] == fresh[3].queue_item_id


async def test_jump_to_top_speaks_in_head_gaps_without_redeciding_the_tail(
    tmp_path: Path,
) -> None:
    """Jumping playback back to the top opens the head gaps without re-deciding the tail."""
    tracks = [_track(index) for index in range(10)]
    dummy = _make_replan_dj(
        tmp_path, list(tracks), current_index=5, index_in_buffer=5, host=_optional_host(3)
    )

    await dummy._replan_queue("queue-1")
    queues = dummy.player_queues
    assert queues.loads
    loaded_before_jump = len(queues.loads)
    tail_gap_ids = {
        item.extra_attributes[ATTR_GAP_NEXT_ID]
        for item in queues._items
        if item.extra_attributes.get(ATTR_QUEUE_DJ)
    }

    queues._queue.current_index = 0
    queues._queue.index_in_buffer = 0
    await dummy._replan_queue("queue-1")

    new_loads = queues.loads[loaded_before_jump:]
    assert new_loads
    new_gap_ids = {load[0][0].extra_attributes[ATTR_GAP_NEXT_ID] for load in new_loads}
    # the history is capped at the head's first gap, so the guard clears three songs later
    assert new_gap_ids == {tracks[4].queue_item_id}
    assert new_gap_ids.isdisjoint(tail_gap_ids)


async def test_stepping_back_a_track_keeps_staged_breaks_apart(tmp_path: Path) -> None:
    """A break staged further down the queue still spaces the ones planned behind it."""
    tracks = [_track(index) for index in range(12)]
    dummy = _make_replan_dj(
        tmp_path, list(tracks), current_index=5, index_in_buffer=5, host=_optional_host(3)
    )

    await dummy._replan_queue("queue-1")
    queues = dummy.player_queues

    # the listener steps back a track, which reopens the gaps behind the staged clips
    queues._queue.current_index = 4
    queues._queue.index_in_buffer = 4
    await dummy._replan_queue("queue-1")

    clip_positions = [
        index
        for index, item in enumerate(queues._items)
        if item.extra_attributes.get(ATTR_QUEUE_DJ)
    ]
    for before, after in pairwise(clip_positions):
        tracks_between = sum(
            1
            for item in queues._items[before + 1 : after]
            if not item.extra_attributes.get(ATTR_QUEUE_DJ)
        )
        assert tracks_between >= 3


async def test_a_probed_duration_alone_does_not_move_the_history(tmp_path: Path) -> None:
    """A track duration correcting downwards is not a queue that rewound."""
    tracks = [_track(index) for index in range(10)]
    dummy = _make_replan_dj(tmp_path, list(tracks), current_index=4, index_in_buffer=4)

    await dummy._replan_queue("queue-1")
    state = dummy._dj_queues["queue-1"]
    history_before = {section_id: list(events) for section_id, events in state.history.items()}
    minutes_before = state.minutes_before_window
    assert history_before

    # a shorter probed duration replaces the estimate, so only the minute count drops
    tracks[0].duration = 30
    await dummy._replan_queue("queue-1")

    assert state.minutes_before_window < minutes_before
    assert state.songs_before_window == 5
    assert state.history == history_before


async def test_hourly_host_speaks_again_after_a_clear_and_refill(tmp_path: Path) -> None:
    """A max_per_60min guard must not keep muting the DJ off the old queue's high-water mark."""
    old_items = [_track(index) for index in range(24)]
    dummy = _make_replan_dj(
        tmp_path, list(old_items), current_index=19, index_in_buffer=19, host=_hourly_host()
    )
    for section in _hourly_sections():
        dummy._sections[section["id"]] = section

    await dummy._replan_queue("queue-1")
    queues = dummy.player_queues
    assert queues.loads
    loaded_before_clear = len(queues.loads)

    queues._items = []
    queues._queue.current_index = None
    queues._queue.index_in_buffer = None
    await dummy._replan_queue("queue-1")
    assert len(queues.loads) == loaded_before_clear

    fresh = [_track(index) for index in range(6)]
    queues._items = fresh
    queues._queue.current_index = 0
    queues._queue.index_in_buffer = 0
    await dummy._replan_queue("queue-1")

    new_loads = queues.loads[loaded_before_clear:]
    assert new_loads
    # the discarded break never aired, so it spends none of the once-per-hour budget
    assert new_loads[0][0][0].extra_attributes[ATTR_GAP_NEXT_ID] == fresh[2].queue_item_id


async def test_replacing_the_upcoming_tracks_reanchors_the_guard(tmp_path: Path) -> None:
    """Swapping every upcoming track leaves the DJ speaking at its normal gap, not silent."""
    tracks_v1 = [_track(index) for index in range(6)]
    dummy = _make_replan_dj(
        tmp_path, list(tracks_v1), current_index=0, index_in_buffer=0, host=_optional_host(2)
    )

    await dummy._replan_queue("queue-1")
    queues = dummy.player_queues
    assert queues.loads
    loaded_after_pass1 = len(queues.loads)

    # the playing track stays, so the axis holds still and only the history's tracks go
    new_tail = [_track(20 + index) for index in range(5)]
    queues._items = [tracks_v1[0], *new_tail]
    await dummy._replan_queue("queue-1")

    new_loads = queues.loads[loaded_after_pass1:]
    assert new_loads
    assert new_loads[0][0][0].extra_attributes[ATTR_GAP_NEXT_ID] == new_tail[1].queue_item_id


async def test_scheduled_replan_serves_requests_landing_during_a_pass(tmp_path: Path) -> None:
    """A replan request raised by the pass's own inserts is served and then converges."""
    tracks = [_track(index) for index in range(4)]
    dummy = _make_replan_dj(tmp_path, list(tracks))
    queues = dummy.player_queues
    queues.on_items_updated = lambda: dummy._schedule_replan("queue-1")

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

    dummy._build_program = _failing_build_program  # type: ignore[method-assign, assignment]
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
        # set_queue_dj re-arms the queue mid-pass, marks the fresh state plannable once its
        # clip cleanup ran, and latches its request onto the still running task
        dummy._arm_dj_state("queue-1", host["id"]).ready = True
        dummy._schedule_replan("queue-1")
        raise MusicAssistantError("misconfigured host")

    dummy._build_program = _failing_build_program  # type: ignore[method-assign, assignment]
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


async def test_stale_pass_inserts_nothing_after_a_mid_pass_dj_switch(tmp_path: Path) -> None:
    """A pass that resumes after set_queue_dj replaced its state must not insert clips."""
    tracks = [_track(index) for index in range(4)]
    dummy = _make_replan_dj(tmp_path, list(tracks))
    old_state = dummy._dj_queues["queue-1"]
    prepare_runtime_tokens = dummy._prepare_runtime_tokens

    daisy = _must_host()
    daisy["id"] = "daisy"
    dummy._hosts["daisy"] = daisy

    async def _switch_mid_pass(program: dict[str, Any]) -> dict[str, str]:
        # stands in for set_queue_dj swapping this queue to another host while the
        # in-flight pass is still awaiting a slow runtime token fetch
        dummy._arm_dj_state("queue-1", "daisy")
        return await prepare_runtime_tokens(program)

    dummy._prepare_runtime_tokens = _switch_mid_pass  # type: ignore[method-assign]
    await dummy._replan_queue("queue-1")

    assert dummy.player_queues.loads == []
    new_state = dummy._dj_queues["queue-1"]
    assert new_state is not old_state
    assert new_state.decided_gap_ids == set()


async def test_replan_holds_off_until_the_switch_cleanup_ran(tmp_path: Path) -> None:
    """A pass entering between arming a host and clearing the old clips must not plan."""
    tracks = [_track(index) for index in range(4)]
    dummy = _make_replan_dj(tmp_path, list(tracks))
    # stands in for set_queue_dj arming the new host while a drain task is already awake
    state = dummy._arm_dj_state("queue-1", "rick")
    state.replan_pending = True

    await dummy._replan_queue("queue-1")

    assert dummy.player_queues.loads == []
    assert state.decided_gap_ids == set()
    # the request is dropped, not kept, or the drain loop would spin on the bail
    assert state.replan_pending is False


async def test_switch_refills_the_gaps_its_own_cleanup_frees(tmp_path: Path) -> None:
    """A replan racing a host switch must not leave the gaps its cleanup frees unplanned."""
    tracks = [_track(index) for index in range(4)]
    dummy = _make_replan_dj(tmp_path, list(tracks))
    await dummy._replan_queue("queue-1")
    queues = dummy.player_queues
    old_clip_ids = {
        item.queue_item_id
        for item in queues.items("queue-1")
        if item.extra_attributes.get(ATTR_QUEUE_DJ)
    }
    assert len(old_clip_ids) == 2  # sanity: rick filled both gaps

    daisy = _must_host()
    daisy["id"] = "daisy"
    dummy._hosts["daisy"] = daisy
    write_queue_dj = dummy._write_queue_dj

    async def _write_and_replan() -> None:
        # a queue event wakes the drain task while the switch sits between arming the new
        # state and clearing the previous host's clips
        await write_queue_dj()
        await dummy._replan_queue("queue-1")

    dummy._write_queue_dj = _write_and_replan  # type: ignore[method-assign]
    await dummy.set_queue_dj("queue-1", "daisy")
    await asyncio.gather(*dummy.mass.tasks)

    assert old_clip_ids <= set(queues.deleted)
    remaining = [
        item for item in queues.items("queue-1") if item.extra_attributes.get(ATTR_QUEUE_DJ)
    ]
    assert len(remaining) == 2
    for clip in remaining:
        assert clip.extra_attributes[ATTR_HOST_ID] == "daisy"


async def test_a_failing_switch_cleanup_leaves_no_armed_state(tmp_path: Path) -> None:
    """A switch whose cleanup blows up must not leave behind a state that can never plan."""
    dummy = _make_replan_dj(tmp_path, [_track(index) for index in range(3)])
    daisy = _must_host()
    daisy["id"] = "daisy"
    dummy._hosts["daisy"] = daisy

    def _failing_cleanup(queue_id: str) -> None:  # noqa: ARG001
        raise MusicAssistantError("queue layer is unhappy")

    dummy._remove_pending_dj_clips = _failing_cleanup  # type: ignore[method-assign]

    with pytest.raises(MusicAssistantError):
        await dummy.set_queue_dj("queue-1", "daisy")

    assert dummy._dj_queues == {}
    assert dummy.mass.tasks == []


async def test_a_rejected_clip_keeps_no_guard_history(tmp_path: Path) -> None:
    """A merged clip the splice rejects must not block its own sections from airing later."""
    tracks = [_track(index) for index in range(4)]
    dummy = _make_replan_dj(tmp_path, list(tracks), host=_hourly_host())
    for section in _hourly_sections():
        dummy._sections[section["id"]] = section
    prepare_runtime_tokens = dummy._prepare_runtime_tokens

    async def _slow_prepare(program: dict[str, Any]) -> dict[str, str]:
        # the player buffers ahead while the pass awaits, so the planned gap is spoken for
        dummy.player_queues._queue.index_in_buffer = 1
        return await prepare_runtime_tokens(program)

    dummy._prepare_runtime_tokens = _slow_prepare  # type: ignore[method-assign]
    await dummy._replan_queue("queue-1")

    state = dummy._dj_queues["queue-1"]
    assert dummy.player_queues.loads == []
    assert state.history["Weather"] == []
    assert state.history["News"] == []

    # the queue grows, so both sections get a fresh gap to air in
    dummy._prepare_runtime_tokens = prepare_runtime_tokens  # type: ignore[method-assign]
    extra = [_track(4), _track(5)]
    dummy.player_queues._items.extend(extra)
    await dummy._replan_queue("queue-1")

    assert len(dummy.player_queues.loads) == 1
    clip = dummy.player_queues.loads[0][0][0]
    assert clip.name == "Weather + News"
    assert clip.extra_attributes[ATTR_GAP_NEXT_ID] == extra[0].queue_item_id


async def test_a_pass_applies_all_its_clips_in_one_queue_update(tmp_path: Path) -> None:
    """A whole window of clips reaches the clients as one update, not one per clip."""
    tracks = [_track(index) for index in range(6)]
    dummy = _make_replan_dj(tmp_path, list(tracks))

    await dummy._replan_queue("queue-1")

    queues = dummy.player_queues
    assert len(queues.loads) == 4  # one clip per plannable gap
    assert queues.update_calls == 1
    for clip_items, _index in queues.loads:
        clip = clip_items[0]
        target = next(
            item
            for item in queues.items("queue-1")
            if item.queue_item_id == clip.extra_attributes[ATTR_GAP_NEXT_ID]
        )
        # sharing the target's sort index keeps the clip next to it when un-shuffling
        assert clip.sort_index == target.sort_index


async def test_a_switch_clears_and_refills_in_one_update_per_phase(tmp_path: Path) -> None:
    """The cleanup and the refill of a switch each replace the queue exactly once."""
    tracks = [_track(index) for index in range(6)]
    dummy = _make_replan_dj(tmp_path, list(tracks))
    await dummy._replan_queue("queue-1")
    queues = dummy.player_queues
    assert queues.update_calls == 1
    daisy = _must_host()
    daisy["id"] = "daisy"
    dummy._hosts["daisy"] = daisy

    await dummy.set_queue_dj("queue-1", "daisy")

    assert queues.update_calls == 2
    assert len(queues.deleted) == 4

    await asyncio.gather(*dummy.mass.tasks)

    assert queues.update_calls == 3
    assert len(queues.loads) == 8


async def test_a_vanished_target_leaves_its_gap_open(tmp_path: Path) -> None:
    """A gap whose target left the queue mid-pass stays open, so its return is planned."""
    tracks = [_track(index) for index in range(6)]
    dummy = _make_replan_dj(tmp_path, list(tracks))
    prepare_runtime_tokens = dummy._prepare_runtime_tokens

    async def _slow_prepare(program: dict[str, Any]) -> dict[str, str]:
        # stands in for a slow token source: the planned tracks are already fixed when the
        # user removes one of them from the queue
        dummy.player_queues.update_items(
            "queue-1",
            [item for item in dummy.player_queues.items("queue-1") if item is not tracks[3]],
        )
        return await prepare_runtime_tokens(program)

    dummy._prepare_runtime_tokens = _slow_prepare  # type: ignore[method-assign]
    await dummy._replan_queue("queue-1")

    queues = dummy.player_queues
    announced = {items[0].extra_attributes[ATTR_GAP_NEXT_ID] for items, _ in queues.loads}
    assert announced == {
        tracks[2].queue_item_id,
        tracks[4].queue_item_id,
        tracks[5].queue_item_id,
    }
    state = dummy._dj_queues["queue-1"]
    assert state.decided_gap_ids == announced

    # the track returns, as a reorder that rebuilds the queue does
    dummy._prepare_runtime_tokens = prepare_runtime_tokens  # type: ignore[method-assign]
    queues._items.append(tracks[3])
    await dummy._replan_queue("queue-1")

    assert queues.loads[-1][0][0].extra_attributes[ATTR_GAP_NEXT_ID] == tracks[3].queue_item_id
    assert tracks[3].queue_item_id in state.decided_gap_ids


async def test_reordering_the_queue_keeps_the_moved_gaps_plannable(tmp_path: Path) -> None:
    """Shuffling new tracks into the upcoming items must not hide the moved gaps for good."""
    tracks = [_track(index) for index in range(4)]
    dummy = _make_replan_dj(tmp_path, list(tracks))
    await dummy._replan_queue("queue-1")
    queues = dummy.player_queues
    assert len(queues.loads) == 2
    clips = {
        item.extra_attributes[ATTR_GAP_NEXT_ID]: item
        for item in queues.items("queue-1")
        if item.extra_attributes.get(ATTR_QUEUE_DJ)
    }

    # shuffle on add: the new tracks land in front of the old ones, moving the previously
    # last planned track to the very back. the clips travelled along with their own track
    extra = [_track(4), _track(5)]
    queues._items = [
        tracks[0],
        extra[0],
        tracks[1],
        extra[1],
        clips[tracks[2].queue_item_id],
        tracks[2],
        clips[tracks[3].queue_item_id],
        tracks[3],
    ]
    await dummy._replan_queue("queue-1")

    assert queues.deleted == []
    announced = {items[0].extra_attributes[ATTR_GAP_NEXT_ID] for items, _ in queues.loads[2:]}
    assert announced == {tracks[1].queue_item_id, extra[1].queue_item_id}


async def test_appending_a_batch_plans_only_the_new_gaps(tmp_path: Path) -> None:
    """A batch appended to a progressive queue is planned without re-rolling settled gaps."""
    tracks = [_track(index) for index in range(4)]
    dummy = _make_replan_dj(tmp_path, list(tracks))
    await dummy._replan_queue("queue-1")
    queues = dummy.player_queues
    state = dummy._dj_queues["queue-1"]

    extra = [_track(4), _track(5)]
    queues._items.extend(extra)
    await dummy._replan_queue("queue-1")

    assert len(queues.loads) == 4
    announced = {items[0].extra_attributes[ATTR_GAP_NEXT_ID] for items, _ in queues.loads[2:]}
    assert announced == {extra[0].queue_item_id, extra[1].queue_item_id}
    # the settled gaps never reached the planner, so their events are registered once
    assert [song for song, _minute in state.history["Song_Transition"]] == [2, 3, 4, 5]


async def test_a_repaired_clip_reopens_its_gap(tmp_path: Path) -> None:
    """A clip the repair drops leaves its gap open, so a later pass fills it again."""
    tracks = [_track(index) for index in range(4)]
    dummy = _make_replan_dj(tmp_path, list(tracks))
    await dummy._replan_queue("queue-1")
    queues = dummy.player_queues
    stale = next(
        item
        for item in queues.items("queue-1")
        if item.extra_attributes.get(ATTR_GAP_NEXT_ID) == tracks[3].queue_item_id
    )

    # a queue edit moved the clip away from the track it announces
    queues._items = [item for item in queues._items if item is not stale] + [stale]
    await dummy._replan_queue("queue-1")

    assert queues.deleted == [stale.queue_item_id]
    assert queues.loads[-1][0][0].extra_attributes[ATTR_GAP_NEXT_ID] == tracks[3].queue_item_id


async def test_replan_keeps_state_when_the_queue_is_not_registered_yet(tmp_path: Path) -> None:
    """A queue that has not registered yet is not treated as a vanished queue."""
    dummy = _make_replan_dj(tmp_path, [_track(index) for index in range(4)])
    # the boot-time replan runs before on_player_register created the queue
    dummy.player_queues._queue = FakeQueue("some-other-queue", None, None)
    dummy._dj_file.write_text("untouched")

    await dummy._replan_queue("queue-1")

    assert "queue-1" in dummy._dj_queues
    assert dummy._dj_file.read_text() == "untouched"


async def test_queue_added_event_replans_an_armed_queue(tmp_path: Path) -> None:
    """A queue registering after the provider loaded resumes injection on its own."""
    tracks = [_track(index) for index in range(4)]
    dummy = _make_replan_dj(tmp_path, list(tracks))
    dummy.player_queues._queue = FakeQueue("some-other-queue", None, None)
    await dummy._replan_queue("queue-1")
    assert dummy.player_queues.loads == []

    dummy.player_queues._queue = FakeQueue("queue-1", 0, 0)
    await dummy._on_dj_queue_event(
        cast("Any", SimpleNamespace(event=EventType.QUEUE_ADDED, object_id="queue-1"))
    )
    await asyncio.gather(*dummy.mass.tasks)

    assert len(dummy.player_queues.loads) == 2


async def test_dj_clip_is_sound_effect_media_type(tmp_path: Path) -> None:
    """A DJ clip's media item is a SoundEffect, the type the dynamic pool exclusions key on."""
    dummy = _make_replan_dj(tmp_path, [])
    section = PlannedSection(
        order=0,
        clip_id="dj_clip_1",
        section_id="Song_Transition",
        section_name="Song Transition",
        when="between_songs",
        insert_at_index=0,
        prompt="",
        max_chars=200,
        web_search_mode="disabled",
    )
    clip = dummy._section_to_clip_item("queue-1", "sess", {"id": "", "host_id": "rick"}, section)
    assert clip.media_item is not None
    assert clip.media_item.media_type == MediaType.SOUND_EFFECT
