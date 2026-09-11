"""
Tests for handing playback progress reports to the scrobbler plugins.

The playback tracker calls the ``on_media_item_played`` hook of every loaded plugin that
declares ProviderFeature.SCROBBLE, next to the MEDIA_ITEM_PLAYED event that external
clients consume.
"""

from __future__ import annotations

from contextlib import suppress
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import MagicMock, Mock

from music_assistant_models.enums import (
    EventType,
    MediaType,
    PlaybackState,
    ProviderFeature,
    ProviderType,
)
from music_assistant_models.media_items import ProviderMapping, Track
from music_assistant_models.playback_progress_report import MediaItemPlaybackProgressReport

from music_assistant.controllers.player_queues.helpers import CompareState
from music_assistant.controllers.player_queues.playback_tracker import PlaybackTrackerMixin
from music_assistant.models.plugin import PluginProvider

if TYPE_CHECKING:
    from collections.abc import Coroutine

QUEUE_ID = "queue-1"


class _RecordingScrobbler(PluginProvider):
    """Scrobbler plugin stand-in that records the reports handed to its hook."""

    def __init__(self, fails: bool = False) -> None:
        """Initialize, optionally raising from the hook to simulate a broken scrobbler."""
        self.reports: list[MediaItemPlaybackProgressReport] = []
        self.fails = fails

    async def on_media_item_played(self, report: MediaItemPlaybackProgressReport) -> None:
        """Record the report, or raise when this stand-in is set up to fail."""
        if self.fails:
            raise RuntimeError("scrobbler is unreachable")
        self.reports.append(report)


def _report(uri: str = "library://track/1") -> MediaItemPlaybackProgressReport:
    """Build a playback progress report for a played track."""
    return MediaItemPlaybackProgressReport(
        uri=uri,
        media_type=MediaType.TRACK,
        name="Track",
        duration=200,
        seconds_played=200,
        fully_played=True,
        is_playing=False,
        userid="user-1",
        player_id=QUEUE_ID,
    )


def _tracker(scrobblers: list[Any], tasks: list[Coroutine[Any, Any, None]]) -> SimpleNamespace:
    """Build a tracker stand-in whose provider lookup returns the given scrobblers."""
    return SimpleNamespace(
        mass=SimpleNamespace(
            get_providers_supporting_feature=Mock(return_value=scrobblers),
            create_task=Mock(side_effect=tasks.append),
        )
    )


async def _drain(tasks: list[Coroutine[Any, Any, None]]) -> None:
    """Run the captured coroutines the way a task per plugin would."""
    for coro in tasks:
        with suppress(Exception):
            await coro


async def test_every_scrobbler_gets_the_same_report() -> None:
    """Each scrobbler plugin is handed the report in its own task."""
    scrobblers = [_RecordingScrobbler(), _RecordingScrobbler()]
    tasks: list[Coroutine[Any, Any, None]] = []
    tracker = _tracker(cast("list[Any]", scrobblers), tasks)
    report = _report()

    PlaybackTrackerMixin._report_to_scrobblers(cast("Any", tracker), report)
    await _drain(tasks)

    tracker.mass.get_providers_supporting_feature.assert_called_once_with(
        ProviderFeature.SCROBBLE, priority=(ProviderType.PLUGIN,)
    )
    assert len(tasks) == 2
    for scrobbler in scrobblers:
        assert scrobbler.reports == [report]
        assert scrobbler.reports[0] is report


async def test_a_non_plugin_provider_is_skipped() -> None:
    """A provider that is not a plugin has no hook to call."""
    tasks: list[Coroutine[Any, Any, None]] = []
    tracker = _tracker([Mock()], tasks)

    PlaybackTrackerMixin._report_to_scrobblers(cast("Any", tracker), _report())

    assert tasks == []
    tracker.mass.create_task.assert_not_called()


async def test_a_failing_scrobbler_does_not_stop_the_others() -> None:
    """A scrobbler raising from its hook leaves the other scrobblers reporting."""
    failing = _RecordingScrobbler(fails=True)
    working = _RecordingScrobbler()
    tasks: list[Coroutine[Any, Any, None]] = []
    tracker = _tracker([failing, working], tasks)
    report = _report()

    PlaybackTrackerMixin._report_to_scrobblers(cast("Any", tracker), report)
    await _drain(tasks)

    assert failing.reports == []
    assert working.reports == [report]


def test_the_event_and_the_hook_receive_the_same_report() -> None:
    """A progress report reaches both the event bus and the scrobbler plugins."""
    track = Track(
        item_id="1",
        provider="library",
        name="Track",
        provider_mappings={
            ProviderMapping(item_id="1", provider_domain="library", provider_instance="library")
        },
    )
    item = SimpleNamespace(
        queue_item_id="qi-1",
        media_item=track,
        streamdetails=None,
        duration=200,
        media_type=MediaType.TRACK,
        name="Track",
        extra_attributes={},
    )
    logger = MagicMock()
    logger.isEnabledFor.return_value = False
    tracker = SimpleNamespace(
        _queue_data={
            QUEUE_ID: SimpleNamespace(
                userid="user-1", enqueued_media_items=[], credited_albums=set()
            )
        },
        get_item=Mock(return_value=item),
        _apply_probed_duration=Mock(),
        _should_mark_played=Mock(return_value=False),
        _report_to_scrobblers=Mock(),
        logger=logger,
        mass=SimpleNamespace(signal_event=Mock(), metadata=MagicMock()),
    )
    queue = SimpleNamespace(queue_id=QUEUE_ID, display_name="Q", state=PlaybackState.PLAYING)
    state = CompareState(
        queue_id=QUEUE_ID,
        state=PlaybackState.PLAYING,
        current_item_id="qi-1",
        current_item=cast("Any", item),
        next_item_id=None,
        elapsed_time=60,
        last_playing_elapsed_time=60,
        stream_title=None,
        codec_type=None,
        output_player_ids=None,
    )

    PlaybackTrackerMixin._handle_playback_progress_report(
        cast("Any", tracker), cast("Any", queue), state, state
    )

    event_type, kwargs = (
        tracker.mass.signal_event.call_args.args[0],
        tracker.mass.signal_event.call_args.kwargs,
    )
    assert event_type is EventType.MEDIA_ITEM_PLAYED
    assert kwargs["object_id"] == "library://track/1"
    report = kwargs["data"]
    assert isinstance(report, MediaItemPlaybackProgressReport)
    assert report.userid == "user-1"
    assert report.player_id == QUEUE_ID
    tracker._report_to_scrobblers.assert_called_once_with(report)
