"""Tests for reporting the playback of Plex tracks to the Plex server."""

from __future__ import annotations

import asyncio
from collections.abc import Generator
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import requests
from music_assistant_models.enums import EventType, MediaType, PlaybackState

from music_assistant.providers.plex import PlexProvider, playback_reporter
from music_assistant.providers.plex.helpers import SUPPORTED_FEATURES, compute_client_id
from music_assistant.providers.plex.playback_reporter import PlexPlaybackReporter

INSTANCE_ID = "plex--1"


def _item(
    queue_item_id: str = "qi1",
    key: str = "/library/metadata/101",
    provider: str = INSTANCE_ID,
    media_type: MediaType = MediaType.TRACK,
) -> SimpleNamespace:
    """Return a queue item of a streamed track."""
    return SimpleNamespace(
        queue_item_id=queue_item_id,
        media_type=media_type,
        duration=180,
        streamdetails=SimpleNamespace(provider=provider, item_id=key, duration=180),
    )


def _queue(
    item: SimpleNamespace | None = None,
    state: PlaybackState = PlaybackState.PLAYING,
    elapsed: float = 30.0,
    queue_id: str = "player1",
) -> SimpleNamespace:
    """Return a player queue playing the given item."""
    return SimpleNamespace(
        queue_id=queue_id,
        active=True,
        state=state,
        current_item=item if item is not None else _item(),
        display_name="Living Room",
        corrected_elapsed_time=elapsed,
    )


def _reporter(*queues: SimpleNamespace) -> Any:
    """Return a playback reporter for a mocked Plex provider with the given queues."""
    provider = MagicMock()
    provider.instance_id = INSTANCE_ID
    provider.mass.player_queues.all.return_value = queues
    return PlexPlaybackReporter(provider)


def _sent(reporter: Any) -> list[tuple[dict[str, str], dict[str, str]]]:
    """Return the params and headers of the timeline updates sent to the Plex server."""
    return [
        (call.kwargs["params"], call.kwargs["headers"])
        for call in reporter.provider._plex_server.query.call_args_list
    ]


@pytest.fixture
def clock() -> Generator[MagicMock]:
    """Patch the monotonic clock of the reporter."""
    with patch.object(playback_reporter, "time") as mock_time:
        mock_time.monotonic.return_value = 100.0
        yield mock_time.monotonic


@pytest.mark.usefixtures("clock")
async def test_reports_playing_track_as_the_player() -> None:
    """A playing track of the provider is reported under the identity of its player."""
    reporter = _reporter(_queue())

    await reporter._report_queues()

    [(params, headers)] = _sent(reporter)
    assert params == {
        "ratingKey": "101",
        "key": "/library/metadata/101",
        "state": "playing",
        "time": "30000",
        "duration": "180000",
    }
    assert headers["X-Plex-Client-Identifier"] == compute_client_id(INSTANCE_ID, "player1")
    assert headers["X-Plex-Session-Identifier"] == compute_client_id(INSTANCE_ID, "player1")
    assert headers["X-Plex-Device-Name"] == "Living Room"


@pytest.mark.parametrize(
    "item",
    [
        _item(provider="filesystem--1"),
        _item(media_type=MediaType.PODCAST_EPISODE),
    ],
)
@pytest.mark.usefixtures("clock")
async def test_ignores_items_not_streamed_as_track_by_this_provider(item: SimpleNamespace) -> None:
    """Only tracks streamed from this Plex server are reported."""
    reporter = _reporter(_queue(item))

    await reporter._report_queues()

    assert _sent(reporter) == []


@pytest.mark.usefixtures("clock")
async def test_inactive_queue_is_not_reported() -> None:
    """A queue its player does not play from is not reported."""
    queue = _queue()
    queue.active = False
    reporter = _reporter(queue)

    await reporter._report_queues()

    assert _sent(reporter) == []


async def test_position_is_reported_at_the_interval(clock: MagicMock) -> None:
    """An unchanged playing track is reported again only once the interval has passed."""
    queue = _queue()
    reporter = _reporter(queue)

    await reporter._report_queues()
    clock.return_value = 105.0
    queue.corrected_elapsed_time = 35.0
    await reporter._report_queues()
    assert len(_sent(reporter)) == 1

    clock.return_value = 110.0
    queue.corrected_elapsed_time = 40.0
    await reporter._report_queues()
    assert [params["time"] for params, _ in _sent(reporter)] == ["30000", "40000"]


async def test_pause_is_reported_at_once(clock: MagicMock) -> None:
    """Pausing is reported right away, and a paused track is not reported again."""
    queue = _queue()
    reporter = _reporter(queue)

    await reporter._report_queues()
    clock.return_value = 102.0
    queue.state = PlaybackState.PAUSED
    queue.corrected_elapsed_time = 32.0
    await reporter._report_queues()
    clock.return_value = 200.0
    await reporter._report_queues()

    assert [(params["state"], params["time"]) for params, _ in _sent(reporter)] == [
        ("playing", "30000"),
        ("paused", "32000"),
    ]


async def test_next_track_stops_the_previous_one(clock: MagicMock) -> None:
    """A new track stops the previous one at the position it reached."""
    queue = _queue(elapsed=170.0)
    reporter = _reporter(queue)

    await reporter._report_queues()
    clock.return_value = 108.0
    queue.current_item = _item(queue_item_id="qi2", key="/library/metadata/102")
    queue.corrected_elapsed_time = 1.0
    await reporter._report_queues()

    assert [(params["key"], params["state"], params["time"]) for params, _ in _sent(reporter)] == [
        ("/library/metadata/101", "playing", "170000"),
        # the position the track had reached when the next one started
        ("/library/metadata/101", "stopped", "178000"),
        ("/library/metadata/102", "playing", "1000"),
    ]


async def test_same_track_queued_again_is_a_new_play(clock: MagicMock) -> None:
    """The same track played again from another queue item stops the earlier play."""
    queue = _queue(elapsed=179.0)
    reporter = _reporter(queue)

    await reporter._report_queues()
    clock.return_value = 101.0
    queue.current_item = _item(queue_item_id="qi2")
    queue.corrected_elapsed_time = 0.0
    await reporter._report_queues()

    assert [(params["state"], params["time"]) for params, _ in _sent(reporter)] == [
        ("playing", "179000"),
        # the position the track had reached is capped at its duration
        ("stopped", "180000"),
        ("playing", "0"),
    ]


@pytest.mark.parametrize("change", ["idle", "other_provider", "queue_removed"])
async def test_stopped_playback_ends_the_report(clock: MagicMock, change: str) -> None:
    """A track that no longer plays is reported stopped, and only once."""
    queue = _queue()
    reporter = _reporter(queue)

    await reporter._report_queues()
    clock.return_value = 104.0
    if change == "idle":
        queue.state = PlaybackState.IDLE
    elif change == "other_provider":
        queue.current_item = _item(queue_item_id="qi2", provider="filesystem--1")
    else:
        reporter.mass.player_queues.all.return_value = ()
    await reporter._report_queues()
    clock.return_value = 200.0
    await reporter._report_queues()

    assert [(params["state"], params["time"]) for params, _ in _sent(reporter)] == [
        ("playing", "30000"),
        ("stopped", "34000"),
    ]


async def test_mirrored_queue_is_reported_within_its_play_queue(clock: MagicMock) -> None:
    """A queue that Plex Connect mirrors is reported within its Plex play queue until removed."""
    queue = _queue()
    other_queue = _queue(queue_id="player2")
    reporter = _reporter(queue, other_queue)
    play_queue_item_ids = {"qi1": "901", "qi2": "902"}

    remove = reporter.add_play_queue(
        "player1",
        lambda queue: {
            "containerKey": "/playQueues/77",
            "playQueueItemID": play_queue_item_ids[queue.current_item.queue_item_id],
        },
    )
    await reporter._report_queues()
    clock.return_value = 101.0
    queue.current_item = _item(queue_item_id="qi2", key="/library/metadata/102")
    await reporter._report_queues()
    remove()
    clock.return_value = 111.0
    await reporter._report_queues()

    queue_ids = {
        compute_client_id(INSTANCE_ID, q.queue_id): q.queue_id for q in (queue, other_queue)
    }
    assert [
        (
            queue_ids[headers["X-Plex-Client-Identifier"]],
            params["state"],
            params.get("playQueueItemID"),
        )
        for params, headers in _sent(reporter)
    ] == [
        ("player1", "playing", "901"),
        ("player2", "playing", None),
        # the previous track is stopped within its own play queue item
        ("player1", "stopped", "901"),
        ("player1", "playing", "902"),
        ("player1", "playing", None),
        ("player2", "playing", None),
    ]


async def test_failed_report_waits_for_the_interval(clock: MagicMock) -> None:
    """A report the Plex server did not take is not retried before the next interval."""
    reporter = _reporter(_queue())
    reporter.provider._plex_server.query.side_effect = requests.exceptions.ConnectionError

    await reporter._report_queues()
    clock.return_value = 101.0
    await reporter._report_queues()

    assert len(_sent(reporter)) == 1
    assert reporter._next_report_delay() == 9.0


@pytest.mark.usefixtures("clock")
def test_next_report_delay() -> None:
    """The next report is due when the earliest playing track reaches the interval."""
    reporter = _reporter()
    assert reporter._next_report_delay() == playback_reporter.TIMELINE_INTERVAL

    reporter._timelines["paused"] = MagicMock(state="paused", reported_at=91.0)
    reporter._timelines["playing"] = MagicMock(state="playing", reported_at=94.0)
    assert reporter._next_report_delay() == 4.0

    reporter._timelines["overdue"] = MagicMock(state="playing", reported_at=80.0)
    assert reporter._next_report_delay() == 0.0


async def test_started_reporter_reports_queue_changes() -> None:
    """A started reporter reports a queue as soon as a queue event comes in."""
    queue = _queue(state=PlaybackState.IDLE)
    reporter = _reporter(queue)
    reporter.mass.create_task = lambda coro, **_kwargs: asyncio.create_task(coro)

    reporter.start()
    try:
        [(_callback, events), _] = reporter.mass.subscribe.call_args
        assert events == (EventType.QUEUE_UPDATED, EventType.QUEUE_TIME_UPDATED)
        await asyncio.sleep(0)
        queue.state = PlaybackState.PLAYING
        reporter._on_queue_event(MagicMock())
        async with asyncio.timeout(2):
            while not reporter.provider._plex_server.query.called:
                await asyncio.sleep(0.01)
    finally:
        task = reporter._task
        reporter.stop()

    assert [params["state"] for params, _ in _sent(reporter)] == ["playing"]
    reporter.mass.subscribe.return_value.assert_called_once_with()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.parametrize(("library_type", "started"), [("music", True), ("audiobooks", False)])
async def test_provider_reports_playback_of_a_music_library(
    library_type: str, started: bool
) -> None:
    """A music library reports its playback while the provider is loaded."""
    mass = MagicMock()
    config = MagicMock()
    config.instance_id = INSTANCE_ID
    config.get_value = {"library_type": library_type, "log_level": "INFO"}.get
    setup_data = {"library_type": library_type, "token": "local_auth"}
    mass.config.get = lambda key, default=None: (
        setup_data if str(key).endswith("/setup_data") else default
    )
    mass.config.decrypt_string = lambda value: value
    provider = PlexProvider(mass, MagicMock(domain="plex"), config, SUPPORTED_FEATURES)
    assert provider.playback_reporter.provider is provider
    provider.playback_reporter = MagicMock()

    await provider.loaded_in_mass()
    assert provider.playback_reporter.start.called is started
    await provider.unload()

    provider.playback_reporter.stop.assert_called_once_with()
