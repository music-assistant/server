"""Tests for the player queues controller helpers."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Self, cast

import pytest
from music_assistant_models.media_items import Playlist, Track
from music_assistant_models.media_items.provider_mapping import ProviderMapping
from music_assistant_models.player_queue import PlayerQueue
from music_assistant_models.queue_item import QueueItem
from music_assistant_models.unique_list import UniqueList

from music_assistant.constants import ATTR_PLAY_ACTION_IN_PROGRESS
from music_assistant.controllers.player_queues.helpers import (
    get_current_playback_speed,
    handle_play_action,
    has_dynamic_source,
)

if TYPE_CHECKING:
    from music_assistant_models.media_items import MediaItemType

    from music_assistant.controllers.player_queues.controller import PlayerQueuesController

_PROVIDER_MAPPINGS = {
    ProviderMapping(item_id="x", provider_domain="test", provider_instance="test")
}


def _playlist(*, is_dynamic: bool, name: str = "PL") -> Playlist:
    return Playlist(
        item_id=name.lower(),
        provider="test",
        name=name,
        provider_mappings=_PROVIDER_MAPPINGS,
        is_dynamic=is_dynamic,
    )


def _track(name: str) -> Track:
    return Track(
        item_id=name.lower(),
        provider="test",
        name=name,
        duration=100,
        artists=UniqueList(),
        provider_mappings=_PROVIDER_MAPPINGS,
    )


def _queue_item(name: str, *, item_id: str | None = None, **extra_attributes: Any) -> QueueItem:
    return QueueItem(
        queue_id="q1",
        queue_item_id=item_id or name.lower(),
        name=name,
        duration=100,
        extra_attributes=extra_attributes,
    )


def _queue() -> PlayerQueue:
    return PlayerQueue(queue_id="q1", active=True, display_name="Q1", available=True, items=0)


class TestHasDynamicSource:
    """Tests for has_dynamic_source."""

    def test_single_dynamic_playlist(self) -> None:
        """A single dynamic playlist puts the queue in dynamic mode."""
        assert has_dynamic_source([_playlist(is_dynamic=True)]) is True

    def test_single_non_dynamic_playlist(self) -> None:
        """A single non-dynamic playlist is not a dynamic source."""
        assert has_dynamic_source([_playlist(is_dynamic=False)]) is False

    def test_empty_source(self) -> None:
        """An empty source list is not dynamic."""
        assert has_dynamic_source([]) is False

    def test_multiple_dynamic_playlists(self) -> None:
        """Any dynamic playlist among the sources puts the queue in dynamic mode."""
        source: list[MediaItemType] = [
            _playlist(is_dynamic=True, name="A"),
            _playlist(is_dynamic=True, name="B"),
        ]
        assert has_dynamic_source(source) is True

    def test_dynamic_playlist_mixed_with_finite_source(self) -> None:
        """A dynamic playlist mixed with a finite source still counts as dynamic."""
        source: list[MediaItemType] = [_track("Song"), _playlist(is_dynamic=True)]
        assert has_dynamic_source(source) is True

    def test_non_playlist_item(self) -> None:
        """A non-playlist media item is not a dynamic source."""
        assert has_dynamic_source([_track("Song")]) is False


class TestGetCurrentPlaybackSpeed:
    """Tests for get_current_playback_speed."""

    def test_no_current_item(self) -> None:
        """Defaults to 1.0 when the queue has no current item."""
        assert get_current_playback_speed(_queue()) == 1.0

    def test_speed_from_extra_attributes(self) -> None:
        """Reads the playback_speed from the current item's extra attributes."""
        queue = _queue()
        queue.current_item = _queue_item("Song", playback_speed=1.5)
        assert get_current_playback_speed(queue) == 1.5

    def test_unset_speed_defaults_to_one(self) -> None:
        """Defaults to 1.0 when the current item has no playback_speed set."""
        queue = _queue()
        queue.current_item = _queue_item("Song")
        assert get_current_playback_speed(queue) == 1.0

    def test_zero_speed_falls_back_to_one(self) -> None:
        """A falsy playback_speed falls back to 1.0."""
        queue = _queue()
        queue.current_item = _queue_item("Song", playback_speed=0)
        assert get_current_playback_speed(queue) == 1.0


class _FakeLock:
    """No-op re-entrant async context manager standing in for the player lock."""

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc_info: object) -> bool:
        return False


class _FakePlayers:
    def get_player_lock(self, queue_id: str, purpose: object) -> _FakeLock:
        return _FakeLock()


class _FakeMass:
    def __init__(self) -> None:
        self.players = _FakePlayers()


class _FakeQueue:
    def __init__(self) -> None:
        self.extra_attributes: dict[str, Any] = {}


class _FakeController:
    """Minimal stand-in exposing only what handle_play_action touches."""

    def __init__(self, queues: dict[str, _FakeQueue]) -> None:
        self.mass = _FakeMass()
        self._queues = queues
        self._play_action_refcount: dict[str, int] = {}
        self.signal_calls: list[str] = []

    def signal_update(self, queue_id: str, items_changed: bool = False) -> None:
        self.signal_calls.append(queue_id)


def _flag_value(ctrl: PlayerQueuesController, queue_id: str) -> bool:
    """Return the current in-progress flag for a queue (False if unknown)."""
    queue = ctrl._queues.get(queue_id)
    return bool(queue.extra_attributes.get(ATTR_PLAY_ACTION_IN_PROGRESS, False)) if queue else False


@handle_play_action
async def _flag_during(self: PlayerQueuesController, queue_id: str, sink: list[bool]) -> str:
    """Record the in-progress flag while running."""
    sink.append(_flag_value(self, queue_id))
    return "done"


@handle_play_action
async def _nested(self: PlayerQueuesController, queue_id: str, sink: list[bool]) -> str:
    """Invoke a nested decorated action on the same queue."""
    await _flag_during(self, queue_id, sink)
    sink.append(_flag_value(self, queue_id))
    return "outer"


@handle_play_action
async def _boom(self: PlayerQueuesController, queue_id: str) -> str:  # noqa: ARG001
    """Raise to exercise the cleanup path."""
    raise RuntimeError("boom")


class TestHandlePlayAction:
    """Tests for the handle_play_action decorator."""

    async def test_runs_without_known_queue(self) -> None:
        """When the queue is unknown the action still runs and nothing is signalled."""
        ctrl = _FakeController({})
        sink: list[bool] = []
        assert await _flag_during(cast("PlayerQueuesController", ctrl), "missing", sink) == "done"
        assert sink == [False]
        assert ctrl.signal_calls == []

    async def test_sets_and_clears_flag(self) -> None:
        """The in-progress flag is set during the action and cleared afterwards."""
        queue = _FakeQueue()
        ctrl = _FakeController({"q1": queue})
        sink: list[bool] = []
        assert await _flag_during(cast("PlayerQueuesController", ctrl), "q1", sink) == "done"
        assert sink == [True]
        assert queue.extra_attributes[ATTR_PLAY_ACTION_IN_PROGRESS] is False
        # signalled once on entry and once on exit
        assert ctrl.signal_calls == ["q1", "q1"]
        assert ctrl._play_action_refcount == {}

    async def test_nested_actions_refcount(self) -> None:
        """Nested actions keep the flag set until the outermost one finishes."""
        queue = _FakeQueue()
        ctrl = _FakeController({"q1": queue})
        sink: list[bool] = []
        assert await _nested(cast("PlayerQueuesController", ctrl), "q1", sink) == "outer"
        # flag stayed True through the inner action and after it returned
        assert sink == [True, True]
        assert queue.extra_attributes[ATTR_PLAY_ACTION_IN_PROGRESS] is False
        # only the outermost entry/exit signal an update (inner sees it already in progress)
        assert ctrl.signal_calls == ["q1", "q1"]
        assert ctrl._play_action_refcount == {}

    async def test_flag_cleared_on_exception(self) -> None:
        """The flag is cleared even when the action raises."""
        queue = _FakeQueue()
        ctrl = _FakeController({"q1": queue})
        with pytest.raises(RuntimeError, match="boom"):
            await _boom(cast("PlayerQueuesController", ctrl), "q1")
        assert queue.extra_attributes[ATTR_PLAY_ACTION_IN_PROGRESS] is False
        assert ctrl._play_action_refcount == {}
