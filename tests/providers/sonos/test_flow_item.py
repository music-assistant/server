"""
Tests for a queue in flow mode that a Sonos speaker plays as one cloud queue item.

A lossless flow stream is the only item of the speaker's cloud queue: it has no duration, no
track of its own and no next item. A speaker that runs out of the stream says so by reading
its cloud queue, which restarts a flow that is not finished.
"""

import json
import logging
import time
from collections import deque
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiosonos.api.models import PlayBackState as SonosPlayBackState
from music_assistant_models.enums import MediaType
from music_assistant_models.player import PlayerMedia
from music_assistant_models.player_queue import PlayerQueue

from music_assistant.controllers.player_queues import PlayerQueuesController
from music_assistant.controllers.player_queues.constants import LOST_FLOW_RESTART_LIMIT
from music_assistant.controllers.player_queues.state import PlayerQueueData
from music_assistant.providers.sonos.player import SonosPlayer
from tests.providers.sonos.test_cloud_queue import (
    QUEUE_ID,
    _error_report,
    _make_cloud_queue,
    _make_media_queue_item,
)
from tests.providers.sonos.test_stream_route import BASE_URL, FLOW_URL, _make_speaker, _media

SESSION_ID = "session-2"


async def _play_flow(
    monkeypatch: pytest.MonkeyPatch, playback_quality: str | None = None
) -> tuple[SonosPlayer, MagicMock, PlayerQueueData]:
    """
    Create a speaker that plays a lossless queue in flow mode, held by the real queue controller.

    :param monkeypatch: The fixture that sets the flow mode of the speaker.
    :param playback_quality: The playback quality level of the speaker, None for its default.
    :return: The speaker, its mocked group and the record of its queue.
    """
    player, group = _make_speaker(monkeypatch, playback_quality, flow_mode=True)
    queue = PlayerQueue(
        queue_id=QUEUE_ID, active=True, display_name="Party", available=True, items=2
    )
    queue.current_index = 0
    queue_data = PlayerQueueData(queue=queue)
    queue_data.session_id = SESSION_ID
    queue_data.items = [
        _make_media_queue_item("item0", MediaType.TRACK),
        _make_media_queue_item("item1", MediaType.TRACK),
    ]
    queues = cast("Any", PlayerQueuesController.__new__(PlayerQueuesController))
    queues.logger = logging.getLogger("test.sonos.player_queues")
    queues.mass = player.mass
    queues._queue_data = {QUEUE_ID: queue_data}
    # what a restart does is the queue controller's: only that one is asked for counts here
    queues._restart_lost_flow_stream = MagicMock()
    mass = cast("MagicMock", player.mass)
    mass.player_queues = queues
    mass.players.get_player.return_value.offline_by_power_control = False
    # the media of a queue in flow mode is the one of the first track of its stream
    media = _media(MediaType.TRACK)
    media.duration, media.stream_duration = 180, 120
    await player.play_media(media)
    return player, group, queue_data


async def _read_window(player: SonosPlayer, reason: str, item_id: str | None = None) -> Any:
    """
    Let the speaker read its cloud queue, and return the answer.

    :param player: The speaker.
    :param reason: The reason the speaker gives for the request.
    :param item_id: The item it asks about, the one item of the current load when omitted.
    """
    request = MagicMock()
    request.query = {"itemId": item_id or player.wire_item_id("flow"), "reason": reason}
    response = await _make_cloud_queue()._handle_sonos_queue_itemwindow(player, request)
    return json.loads(response.text or "{}")


async def test_the_flow_item_has_no_end_and_no_track_of_its_own(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The one item is the stream itself: without a duration, a seek or the first track's name."""
    player, _, _ = await _play_flow(monkeypatch)

    body = await _read_window(player, "load")

    assert (body["includesBeginningOfQueue"], body["includesEndOfQueue"]) == (True, True)
    [item] = body["items"]
    assert item["id"] == player.wire_item_id("flow")
    assert item["track"]["mediaUrl"] == f"{FLOW_URL}.wav"
    assert item["track"]["contentType"] == "audio/wav"
    assert item["track"]["durationMillis"] == 0
    assert item["track"]["name"] == "Music Assistant"
    assert item["track"]["artist"] is None
    assert "policies" not in item


async def test_the_flow_item_is_no_file_whatever_its_first_track_is(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The first track of the stream alone would be a file the speaker pauses and seeks in."""
    player, _, _ = await _play_flow(monkeypatch)
    mass = cast("MagicMock", player.mass)
    mass.streams.audio.fade_into_pending.return_value = False

    assert player.is_served_as_file("item0") is True
    assert player.is_served_as_file("flow") is False


@pytest.mark.parametrize("playback_quality", [None, "lossless_compressed"])
async def test_a_stream_the_speaker_ran_out_of_restarts_an_unfinished_flow(
    monkeypatch: pytest.MonkeyPatch, playback_quality: str | None
) -> None:
    """A stream that ended, was cut or stalled is not asked for again: the queue restarts it."""
    player, _, _ = await _play_flow(monkeypatch, playback_quality)

    body = await _read_window(player, "queueCompleted")

    queues = cast("Any", player.mass.player_queues)
    queues._restart_lost_flow_stream.assert_called_once_with(
        QUEUE_ID, SESSION_ID, only_while_playing=False, queue_item_id=None
    )
    # the speaker plays what it holds of the stream meanwhile
    assert [x["id"] for x in body["items"]] == [player.wire_item_id("flow")]


async def test_a_finished_flow_ends_by_itself(monkeypatch: pytest.MonkeyPatch) -> None:
    """A flow that streamed all it had is played out, and what follows is the queue's to start."""
    player, _, queue_data = await _play_flow(monkeypatch)
    queue_data.flow_buffer_completed = SESSION_ID

    await _read_window(player, "queueCompleted")

    cast("Any", player.mass.player_queues)._restart_lost_flow_stream.assert_not_called()


async def test_a_flow_that_keeps_failing_stops_restarting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A queue that restarted its stream often enough of late is left where it stopped."""
    player, _, queue_data = await _play_flow(monkeypatch)
    queue_data.flow_restart_times = [time.monotonic()] * LOST_FLOW_RESTART_LIMIT

    await _read_window(player, "queueCompleted")

    cast("Any", player.mass.player_queues)._restart_lost_flow_stream.assert_not_called()


@pytest.mark.parametrize("reason", ["load", "refresh", None])
async def test_another_read_of_the_cloud_queue_restarts_nothing(
    monkeypatch: pytest.MonkeyPatch, reason: str | None
) -> None:
    """Only a speaker that ran out of the stream asks for a restart."""
    player, _, _ = await _play_flow(monkeypatch)

    await _read_window(player, cast("str", reason))

    cast("Any", player.mass.player_queues)._restart_lost_flow_stream.assert_not_called()


async def test_a_replaced_load_that_ran_out_restarts_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A speaker that still reports on the load before a seek does not restart the new one."""
    player, _, _ = await _play_flow(monkeypatch)
    replaced = cast("str", player.wire_item_id("flow"))
    await player.play_media(_media(MediaType.TRACK))

    await _read_window(player, "queueCompleted", replaced)

    cast("Any", player.mass.player_queues)._restart_lost_flow_stream.assert_not_called()


async def test_the_end_of_a_queue_of_tracks_restarts_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A speaker that played its cloud queue of tracks to the end reads it the same way."""
    player, _ = _make_speaker(monkeypatch)
    queues = cast("Any", player.mass.player_queues)
    queues.restart_lost_flow_stream = MagicMock()
    await player.play_media(_media(MediaType.TRACK))

    await _read_window(player, "queueCompleted", player.wire_item_id("item0"))

    queues.restart_lost_flow_stream.assert_not_called()


async def test_a_new_load_gives_the_stream_a_new_item_and_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A restart, a seek or a step loads the stream of the new session under a new id."""
    player, group, _ = await _play_flow(monkeypatch)
    first_id = player.wire_item_id("flow")
    media = _media(MediaType.TRACK, "item1")
    media.queue_session_id = "session-3"

    await player.play_media(media)

    new_id = player.wire_item_id("flow")
    assert new_id != first_id
    assert group.play_cloud_queue.await_args.kwargs["item_id"] == new_id
    body = await _read_window(player, "load")
    assert [x["id"] for x in body["items"]] == [new_id]
    assert body["items"][0]["track"]["mediaUrl"] == (
        f"{BASE_URL}/flow/session-3/{QUEUE_ID}/item1/sonos_player.wav"
    )


async def test_a_late_failure_of_the_replaced_stream_leaves_the_new_one_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The speaker reports the stream it gave up on seconds after it plays the restarted one."""
    player, _, _ = await _play_flow(monkeypatch)
    replaced = cast("str", player.wire_item_id("flow"))
    await player.play_media(_media(MediaType.TRACK))
    player._attr_current_media = PlayerMedia(uri=f"{FLOW_URL}.wav", queue_item_id="flow")
    player._attr_elapsed_time = 3.0
    player.reported_playback_errors = deque(maxlen=16)
    mass = cast("MagicMock", player.mass)
    mass.create_task.reset_mock()
    request = MagicMock()
    request.json = AsyncMock(return_value={"items": [{**_error_report("final"), "id": replaced}]})

    await _make_cloud_queue()._handle_sonos_queue_time_played(player, request)

    mass.create_task.assert_not_called()
    assert player._attr_elapsed_time == 3.0


@pytest.mark.parametrize("report", ["time_played", "playback_error"])
async def test_a_failure_of_the_stream_restarts_an_unfinished_flow(
    monkeypatch: pytest.MonkeyPatch, report: str
) -> None:
    """A speaker that fails on the stream stops, as on its own play after a pause in FLAC."""
    player, _, _ = await _play_flow(monkeypatch, "lossless_compressed")
    wire_id = cast("str", player.wire_item_id("flow"))
    player._attr_current_media = PlayerMedia(uri=f"{FLOW_URL}.flac", queue_item_id="flow")
    player.reported_playback_errors = deque(maxlen=16)
    mass = cast("MagicMock", player.mass)
    mass.create_task.reset_mock()

    if report == "time_played":
        request = MagicMock()
        request.json = AsyncMock(
            return_value={"items": [{**_error_report("final"), "id": wire_id}]}
        )
        await _make_cloud_queue()._handle_sonos_queue_time_played(player, request)
    else:
        mass.streams.base_url = BASE_URL
        error = {
            "errorCode": "ERROR_PLAYBACK_FAILED",
            "reason": "ERROR_CORRUPT_FILE",
            "itemId": wire_id,
        }
        player._on_playback_error(MagicMock(data=error))

    queues = cast("Any", player.mass.player_queues)
    queues._restart_lost_flow_stream.assert_called_once_with(
        QUEUE_ID, SESSION_ID, only_while_playing=False, queue_item_id=None
    )
    # the one restart is all: the stream is no queue item with a source to release
    mass.create_task.assert_called_once()


async def test_a_failed_announcement_restarts_no_flow_under_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An announcement plays over the queue: its failure is not one of the stream."""
    player, _, _ = await _play_flow(monkeypatch)
    player._announcement_media = PlayerMedia(uri="announce.mp3", queue_item_id="announcement")
    cast("Any", player.mass.player_queues).release_failed_item_source = MagicMock()

    player.release_failed_item(cast("str", player.wire_item_id("announcement")))
    await _read_window(player, "queueCompleted", player.wire_item_id("announcement"))

    cast("Any", player.mass.player_queues)._restart_lost_flow_stream.assert_not_called()


async def test_the_position_in_the_flow_item_is_the_position_in_the_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The speaker's report on the one item moves the stream position the queue reads."""
    player, _, _ = await _play_flow(monkeypatch)
    player._attr_current_media = PlayerMedia(uri=f"{FLOW_URL}.wav", queue_item_id="flow")
    monkeypatch.setattr(player, "update_state", MagicMock())
    request = MagicMock()
    request.json = AsyncMock(
        return_value={
            "items": [
                {"id": player.wire_item_id("flow"), "type": "update", "positionMillis": 215500}
            ]
        }
    )

    await _make_cloud_queue()._handle_sonos_queue_time_played(player, request)

    assert player._attr_elapsed_time == 215.5


@pytest.mark.parametrize(("reason", "step"), [("skipNext", "next"), ("skipPrev", "previous")])
async def test_a_skip_on_the_speaker_steps_the_queue(
    monkeypatch: pytest.MonkeyPatch, reason: str, step: str
) -> None:
    """The speaker has no item to skip to in its cloud queue, so the queue takes the step."""
    player, _, _ = await _play_flow(monkeypatch)
    queues = cast("Any", player.mass.player_queues)
    queues.next, queues.previous = MagicMock(), MagicMock()
    mass = cast("MagicMock", player.mass)
    mass.create_task.reset_mock()

    await _read_window(player, reason)

    getattr(queues, step).assert_called_once_with(QUEUE_ID)
    mass.create_task.assert_called_once()
    assert queues.next.call_count + queues.previous.call_count == 1


async def test_the_next_item_is_not_enqueued_on_a_speaker_that_plays_a_flow_item(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The flow stream holds the next item itself: there is nothing to tell the speaker."""
    player, _, _ = await _play_flow(monkeypatch)
    refresh = AsyncMock()
    monkeypatch.setattr(player, "refresh_cloud_queue", refresh)
    player._loaded_session_id = "loaded"

    await player.enqueue_next_media(_media(MediaType.TRACK, "item1"))

    refresh.assert_not_awaited()
    assert player._loaded_session_id == "loaded"


async def test_a_stop_forgets_the_flow_item(monkeypatch: pytest.MonkeyPatch) -> None:
    """A stopped speaker describes no stream to a speaker that still reads its cloud queue."""
    player, group, _ = await _play_flow(monkeypatch)
    group.stop = AsyncMock()
    monkeypatch.setattr(player, "update_state", MagicMock())
    monkeypatch.setattr(player, "update_attributes", MagicMock())
    player._player = MagicMock()

    await player.stop()

    assert player.plays_flow_item is False
    window = await player.build_cloud_queue_window(None)
    assert window.items == []


async def test_a_track_loaded_after_a_flow_item_is_a_cloud_queue_of_tracks_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A queue that leaves flow mode is read by the speaker item by item again."""
    player, _, _ = await _play_flow(monkeypatch)
    monkeypatch.setattr(SonosPlayer, "flow_mode", property(lambda _self: False))

    await player.play_media(_media(MediaType.TRACK))

    assert player.plays_flow_item is False
    window = await player.build_cloud_queue_window(player.wire_item_id("item0"))
    assert [x.queue_item_id for x in window.items] == ["item0", "item1"]


def _speaker_reports(player: SonosPlayer, group: MagicMock, state: str, was_playing: bool) -> bool:
    """
    Let the speaker report a playback state on the flow item of its current load.

    :param player: The speaker.
    :param group: Its mocked group.
    :param state: The state the speaker reports: PLAYING, BUFFERING or PAUSED.
    :param was_playing: Whether the speaker read as playing before this report.
    :return: Whether the speaker waits for the queue to resume its stream.
    """
    group.playback_item_id = player.wire_item_id("flow")
    group.playback_state = SonosPlayBackState(f"PLAYBACK_STATE_{state}")
    return player._awaits_flow_resume(group, was_playing)


@pytest.mark.parametrize("plays_again_as", ["BUFFERING", "PLAYING"])
async def test_a_speaker_that_plays_again_after_a_pause_has_its_queue_resume(
    monkeypatch: pytest.MonkeyPatch, plays_again_as: str
) -> None:
    """A pause at the speaker ends the stream: its own play makes the queue resume, once."""
    player, group, _ = await _play_flow(monkeypatch)
    resume = MagicMock()
    monkeypatch.setattr(player, "_resume_paused_flow", resume)
    mass = cast("MagicMock", player.mass)
    mass.create_task.reset_mock()

    assert _speaker_reports(player, group, "PLAYING", was_playing=False) is False
    assert _speaker_reports(player, group, "PAUSED", was_playing=True) is True
    mass.create_task.assert_not_called()
    # the speaker asks for the stream again, which it can not go on in
    assert _speaker_reports(player, group, plays_again_as, was_playing=False) is True
    assert _speaker_reports(player, group, "PLAYING", was_playing=False) is True

    resume.assert_called_once_with(QUEUE_ID, SESSION_ID)
    mass.create_task.assert_called_once()
    # the new load of the resume is a stream the speaker plays as any other
    await player.play_media(_media(MediaType.TRACK))
    assert _speaker_reports(player, group, "PLAYING", was_playing=False) is False


async def test_a_speaker_that_never_played_its_load_is_not_resumed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A woken speaker reports a pause before it plays the load it just got."""
    player, group, _ = await _play_flow(monkeypatch)
    mass = cast("MagicMock", player.mass)
    mass.create_task.reset_mock()

    assert _speaker_reports(player, group, "PAUSED", was_playing=False) is False
    assert _speaker_reports(player, group, "PLAYING", was_playing=False) is False

    mass.create_task.assert_not_called()


async def test_a_pause_in_a_track_of_a_cloud_queue_resumes_no_queue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A speaker pauses and plays on in a track of its cloud queue by itself."""
    player, group = _make_speaker(monkeypatch)
    await player.play_media(_media(MediaType.TRACK))
    mass = cast("MagicMock", player.mass)
    mass.create_task.reset_mock()
    group.playback_item_id = player.wire_item_id("item0")

    for state, was_playing in (("PAUSED", True), ("PLAYING", False)):
        group.playback_state = SonosPlayBackState(f"PLAYBACK_STATE_{state}")
        assert player._awaits_flow_resume(group, was_playing) is False

    mass.create_task.assert_not_called()


@pytest.mark.parametrize("report", ["failure", "ran_out"])
async def test_a_stream_paused_at_the_speaker_is_no_lost_stream(
    monkeypatch: pytest.MonkeyPatch, report: str
) -> None:
    """What a speaker reports on its own play after a pause does not count as a lost stream."""
    player, group, _ = await _play_flow(monkeypatch, "lossless_compressed")
    monkeypatch.setattr(player, "_resume_paused_flow", MagicMock())
    _speaker_reports(player, group, "PAUSED", was_playing=True)
    _speaker_reports(player, group, "BUFFERING", was_playing=False)

    if report == "failure":
        player.release_failed_item(cast("str", player.wire_item_id("flow")))
    else:
        await _read_window(player, "queueCompleted")

    cast("Any", player.mass.player_queues)._restart_lost_flow_stream.assert_not_called()


@pytest.mark.parametrize("loaded_anew", [False, True])
async def test_the_resume_after_a_pause_leaves_a_queue_that_was_loaded_anew(
    monkeypatch: pytest.MonkeyPatch, loaded_anew: bool
) -> None:
    """A restart or command that came first has the queue playing again already."""
    player, _, queue_data = await _play_flow(monkeypatch)
    queues = cast("Any", player.mass.player_queues)
    queues.resume = AsyncMock()
    locked: list[str] = []

    @asynccontextmanager
    async def _lock(player_id: str) -> AsyncIterator[None]:
        locked.append(player_id)
        yield

    cast("MagicMock", player.mass).players.get_group_and_player_lock = _lock
    if loaded_anew:
        queue_data.session_id = "session-3"

    await player._resume_paused_flow(QUEUE_ID, SESSION_ID)

    assert locked == [QUEUE_ID]
    if loaded_anew:
        queues.resume.assert_not_awaited()
    else:
        queues.resume.assert_awaited_once_with(QUEUE_ID, fade_in=False)
