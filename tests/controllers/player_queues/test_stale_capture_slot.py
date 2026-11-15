"""
Tests for the stream slot of a music service that allows one source stream at a time.

The server prepares the queue's next track ahead of time. For a source that delivers its audio
at playback pace, that capture holds the service's only stream slot until the track has played
out. When the queue changes so that the prepared track is no longer the one that plays next,
the track that does play next needs that slot, and the capture nobody needs has to give it up.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Awaitable
from contextlib import suppress
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.enums import (
    ContentType,
    MediaType,
    PlaybackState,
    ProviderType,
    QueueOption,
    StreamType,
)
from music_assistant_models.media_items import AudioFormat, ProviderMapping, Track
from music_assistant_models.player_queue import PlayerQueue
from music_assistant_models.queue_item import QueueItem
from music_assistant_models.streamdetails import StreamDetails

import music_assistant.controllers.streams.audio as audio_mod
from music_assistant.controllers.player_queues import PlayerQueuesController
from music_assistant.controllers.player_queues.state import PlayerQueueData
from music_assistant.controllers.streams.audio import CrossfadeHandover, StreamsAudio
from music_assistant.controllers.streams.audio_buffer import AudioBuffer
from music_assistant.models.music_provider import MusicProvider

INSTANCE = "single--account_x"
QUEUE_ID = "queue_a"
SESSION_ID = "session-a"
PCM_FORMAT = AudioFormat(content_type=ContentType.PCM_S16LE, sample_rate=8000, bit_depth=16)
# seconds a speaker's request may wait for the slot in these tests
SHORT_WAIT = 2.0


class _SingleStreamProvider(MusicProvider):
    """Streaming provider that allows one source stream per account."""

    @property
    def max_concurrent_streams(self) -> int:
        """Return one source slot."""
        return 1


class _TwoStreamProvider(MusicProvider):
    """Streaming provider that allows two source streams per account."""

    @property
    def max_concurrent_streams(self) -> int:
        """Return two source slots."""
        return 2


async def _realtime_capture(*_args: Any, **_kwargs: Any) -> AsyncGenerator[bytes]:
    """Deliver the first seconds of a track, then keep capturing it far from its end."""
    for _ in range(3):
        yield b"\x00" * PCM_FORMAT.pcm_sample_size
    await asyncio.Event().wait()


def _track_item(name: str) -> QueueItem:
    """
    Build a queue item of a track on the single-slot provider, with its stream details resolved.

    :param name: Neutral name of the track, also used for its ids.
    """
    mapping = ProviderMapping(
        item_id=f"track-{name}",
        provider_domain="single",
        provider_instance=INSTANCE,
        audio_format=PCM_FORMAT,
    )
    item = QueueItem(
        queue_id=QUEUE_ID,
        queue_item_id=f"item-{name}",
        name=name,
        duration=198,
        media_item=Track(
            item_id=mapping.item_id,
            provider=INSTANCE,
            name=name,
            provider_mappings={mapping},
        ),
    )
    item.streamdetails = StreamDetails(
        provider=INSTANCE,
        item_id=mapping.item_id,
        audio_format=PCM_FORMAT,
        media_type=MediaType.TRACK,
        stream_type=StreamType.CUSTOM,
        duration=198,
        queue_id=QUEUE_ID,
        is_realtime=True,
    )
    return item


class _Rig:
    """One playing queue, with the real queue and buffer logic over a single-slot provider."""

    def __init__(self, provider_cls: type[MusicProvider] = _SingleStreamProvider) -> None:
        """
        Wire a bare queue controller and the streams audio side to one provider.

        :param provider_cls: The provider class, which sets how many source streams it allows.
        """
        manifest = MagicMock(domain="single", type=ProviderType.MUSIC)
        manifest.name = "Single"
        config = MagicMock(instance_id=INSTANCE)
        config.name = "Single"
        config.get_value.return_value = "GLOBAL"
        self.provider = provider_cls(MagicMock(), manifest, config)
        self.provider.available = True
        self.items: list[QueueItem] = []
        self.tasks: list[asyncio.Future[Any]] = []
        mass = MagicMock()
        mass.get_provider.side_effect = lambda instance, **_kwargs: (
            self.provider if instance == INSTANCE else None
        )
        mass.providers = []
        mass.music.providers = []
        mass.config.get_raw_core_config_value.side_effect = lambda _core, _key, default: default
        mass.create_task.side_effect = self._create_task
        mass.streams.audio_analysis.start_analysis = AsyncMock()
        self.queues = PlayerQueuesController.__new__(PlayerQueuesController)
        self.queues.mass = mass
        self.queues.logger = MagicMock()
        self.queues._queue_data = {}
        self.queues.signal_update = MagicMock()  # type: ignore[method-assign]
        # the stream details are resolved up front, so loading the next item only finds it
        self.queues.load_next_queue_item = AsyncMock(  # type: ignore[method-assign]
            side_effect=lambda queue_id, current_id: self.queues.get_next_item(queue_id, current_id)
        )
        mass.player_queues = self.queues
        self.mass = mass
        self.audio = StreamsAudio(mass)
        self.audio._get_media_stream = _realtime_capture  # type: ignore[method-assign]
        mass.streams.audio = self.audio

    def play(self, *names: str) -> list[QueueItem]:
        """
        Put a queue on the rig that plays the first of the given tracks.

        The playing track's own capture has finished, as it has when the server prepares the
        track after it on a single-slot source.

        :param names: The tracks of the queue, the playing one first.
        """
        items = [self.track(name) for name in names]
        queue = PlayerQueue(
            queue_id=QUEUE_ID,
            active=True,
            display_name="Queue A",
            available=True,
            items=len(items),
            state=PlaybackState.PLAYING,
            current_index=0,
            current_item=items[0],
            index_in_buffer=0,
        )
        self.queues._queue_data[QUEUE_ID] = PlayerQueueData(
            queue=queue, items=list(items), session_id=SESSION_ID
        )
        return items

    def track(self, name: str) -> QueueItem:
        """
        Build a queue item the rig cleans up after the test.

        :param name: Neutral name of the track.
        """
        item = _track_item(name)
        self.items.append(item)
        return item

    async def prepare_next(self, current: QueueItem) -> AudioBuffer:
        """
        Let the server prepare the track after the given one, as it does ahead of playback.

        :param current: The track that is playing.
        :return: The buffer the next track's capture fills.
        """
        self.queues.prepare_next_audio_buffer(QUEUE_ID, current.queue_item_id)
        await self.settle()
        prepared = self.queues.get_next_item(QUEUE_ID, current.queue_item_id)
        assert prepared is not None
        assert prepared.streamdetails is not None
        buffer = prepared.streamdetails.buffer
        assert isinstance(buffer, AudioBuffer)
        return buffer

    async def capture(self, item: QueueItem) -> AudioBuffer:
        """
        Start the capture of the given item, which takes the provider's only slot.

        :param item: The queue item to capture.
        """
        buffer = await self.audio.get_audio_buffer(
            item, reason="prepare_next", capacity_wait_timeout=0
        )
        assert buffer is not None
        return buffer

    async def ask(self, item: QueueItem, wait: float = SHORT_WAIT) -> AudioBuffer | None:
        """
        Let the speaker ask for the audio of the given item, as its stream request does.

        :param item: The queue item the speaker asks for.
        :param wait: Seconds the request may wait for a source slot.
        """
        return await self.audio.get_audio_buffer(
            item, reason="streaming", capacity_wait_timeout=wait
        )

    async def settle(self) -> None:
        """Wait for the work the controller scheduled so far."""
        await asyncio.gather(*self.tasks)

    async def close(self) -> None:
        """Stop every capture and task the test left running."""
        for task in self.tasks:
            task.cancel()
        for item in self.items:
            if item.streamdetails and (buffer := item.streamdetails.buffer):
                await buffer.clear()

    def _create_task(self, target: Awaitable[Any], **_kwargs: Any) -> asyncio.Future[Any]:
        """Run a task the controller schedules, keeping it so a test can wait for it."""
        task = asyncio.ensure_future(target)
        self.tasks.append(task)
        return task


@pytest.fixture
async def rig(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> AsyncGenerator[_Rig]:
    """Build the rig with every music source open to the queue's playback."""
    monkeypatch.setattr(audio_mod, "playback_sources", AsyncMock(return_value=(None, [])))
    rig = _Rig(getattr(request, "param", _SingleStreamProvider))
    yield rig
    await rig.close()


@pytest.mark.parametrize(
    "option",
    [QueueOption.REPLACE_NEXT, QueueOption.NEXT, QueueOption.REPLACE],
    ids=["replace_next", "next", "replace"],
)
async def test_the_track_that_plays_next_after_a_queue_change_gets_the_slot(
    rig: _Rig, option: QueueOption
) -> None:
    """The prepared track that no longer plays next gives its capture up for the one that does."""
    playing, _prepared, _later = rig.play("track 0", "track 1", "track 2")
    stale_capture = await rig.prepare_next(playing)
    assert not rig.provider.has_available_stream_slot
    new_next, new_after = rig.track("track 5"), rig.track("track 6")
    # a replace starts the new content itself; only what the content change leaves behind counts
    rig.queues.play_index = AsyncMock()  # type: ignore[method-assign]

    await rig.queues._enqueue_with_option(QUEUE_ID, [new_next, new_after], option)
    buffer = await rig.ask(new_next)

    assert buffer is not None
    assert buffer.is_buffering
    assert not stale_capture.is_buffering


@pytest.mark.parametrize("edit", ["delete", "move"])
async def test_the_track_after_a_removed_or_moved_next_track_gets_the_slot(
    rig: _Rig, edit: str
) -> None:
    """Taking the prepared track off the queue, or moving it back, hands its slot over."""
    playing, prepared, following = rig.play("track 0", "track 1", "track 2")
    stale_capture = await rig.prepare_next(playing)

    if edit == "delete":
        rig.queues.delete_item(QUEUE_ID, prepared.queue_item_id)
    else:
        rig.queues.move_item(QUEUE_ID, prepared.queue_item_id, pos_shift=1)
    assert rig.queues.get_next_item(QUEUE_ID, playing.queue_item_id) is following
    buffer = await rig.ask(following)

    assert buffer is not None
    assert buffer.is_buffering
    assert not stale_capture.is_buffering


@pytest.mark.parametrize("rig", [_TwoStreamProvider], indirect=True)
async def test_a_provider_with_a_free_slot_keeps_the_capture_of_a_moved_track(rig: _Rig) -> None:
    """A prepared track moved back keeps its capture while the provider has a slot to spare."""
    playing, _prepared, _later = rig.play("track 0", "track 1", "track 2")
    prepared_capture = await rig.prepare_next(playing)
    new_next = rig.track("track 5")

    await rig.queues._enqueue_with_option(QUEUE_ID, [new_next], QueueOption.NEXT)
    await rig.settle()
    buffer = await rig.ask(new_next)

    assert buffer is not None
    assert prepared_capture.is_buffering


async def test_a_change_that_keeps_the_next_track_starts_no_release(rig: _Rig) -> None:
    """Items added behind the prepared track, or a refresh of the same items, change nothing."""
    playing, _prepared, _later = rig.play("track 0", "track 1", "track 2")
    prepared_capture = await rig.prepare_next(playing)
    tasks_before = rig.mass.create_task.call_count
    queue_data = rig.queues._queue_data[QUEUE_ID]

    rig.queues.update_items(QUEUE_ID, [*queue_data.items, rig.track("track 5")])
    rig.queues.update_items(QUEUE_ID, queue_data.items)

    assert rig.mass.create_task.call_count == tasks_before
    assert prepared_capture.is_buffering


async def test_a_fade_being_mixed_into_a_removed_track_finishes_before_its_release(
    rig: _Rig,
) -> None:
    """The playing track's stream mixes the start of the removed track until its fade is done."""
    playing, prepared, _later = rig.play("track 0", "track 1", "track 2")
    stale_capture = await rig.prepare_next(playing)
    # the playing track's stream is mixing its fade into the prepared track
    handoff = asyncio.Event()
    rig.audio._crossfade_pending[QUEUE_ID] = (prepared.queue_item_id, handoff)

    await rig.queues._enqueue_with_option(
        QUEUE_ID, [rig.track("track 5")], QueueOption.REPLACE_NEXT
    )
    await asyncio.sleep(0.1)
    assert not rig.provider.has_available_stream_slot

    # the mix is done and leaves its continuation for the removed track's stream
    rig.audio._crossfade_handover[QUEUE_ID] = CrossfadeHandover(
        stream=None,
        fade_in_media_duration=2.0,
        pcm_format=PCM_FORMAT,
        queue_item_id=prepared.queue_item_id,
    )
    rig.audio._release_handoff(QUEUE_ID, handoff)
    await rig.settle()

    assert not stale_capture.is_buffering
    assert QUEUE_ID not in rig.audio._crossfade_handover


async def test_a_removed_track_a_speaker_still_streams_keeps_its_capture(rig: _Rig) -> None:
    """A speaker that asked for the prepared track before the change keeps receiving it."""
    playing, prepared, _later = rig.play("track 0", "track 1", "track 2")
    prepared_capture = await rig.prepare_next(playing)
    rig.mass.streams.requested_item_ids.return_value = {prepared.queue_item_id}

    await rig.queues._enqueue_with_option(
        QUEUE_ID, [rig.track("track 5")], QueueOption.REPLACE_NEXT
    )
    await rig.settle()

    assert prepared_capture.is_buffering


async def test_a_moved_track_a_fade_leads_into_keeps_its_capture(rig: _Rig) -> None:
    """The stream before the prepared track already plays the start of a fade into it."""
    playing, prepared, _later = rig.play("track 0", "track 1", "track 2")
    prepared_capture = await rig.prepare_next(playing)
    rig.audio._crossfade_pending[QUEUE_ID] = (prepared.queue_item_id, asyncio.Event())

    await rig.queues._enqueue_with_option(QUEUE_ID, [rig.track("track 5")], QueueOption.NEXT)
    await rig.settle()

    assert prepared_capture.is_buffering


@pytest.mark.parametrize(
    "option", [QueueOption.REPLACE_NEXT, QueueOption.NEXT], ids=["replace_next", "next"]
)
async def test_the_playing_track_keeps_its_capture_when_what_follows_it_changes(
    rig: _Rig, option: QueueOption
) -> None:
    """A capture the speaker still plays from is never given up for the queue's next track."""
    playing, _next = rig.play("track 0", "track 1")
    playing_capture = await rig.capture(playing)

    await rig.queues._enqueue_with_option(QUEUE_ID, [rig.track("track 5")], option)
    await rig.settle()

    assert playing_capture.is_buffering
    assert not rig.provider.has_available_stream_slot


@pytest.mark.parametrize(
    "option", [QueueOption.REPLACE_NEXT, QueueOption.NEXT], ids=["replace_next", "next"]
)
async def test_a_prepared_track_that_plays_keeps_its_capture_when_what_follows_it_changes(
    rig: _Rig, option: QueueOption
) -> None:
    """Once the prepared track plays, a change of the tracks after it leaves its capture alone."""
    first, prepared, _later = rig.play("track 0", "track 1", "track 2")
    prepared_capture = await rig.prepare_next(first)
    # the speaker moved on to the prepared track, which now plays from that capture
    queue = rig.queues._queue_data[QUEUE_ID].queue
    queue.current_index = queue.index_in_buffer = 1
    queue.current_item = prepared

    await rig.queues._enqueue_with_option(QUEUE_ID, [rig.track("track 5")], option)
    await rig.settle()

    assert prepared_capture.is_buffering


async def test_asking_for_the_next_track_leaves_the_playing_tracks_capture_alone(
    rig: _Rig,
) -> None:
    """The next track waits for the slot the playing track holds instead of taking it."""
    playing, upcoming = rig.play("track 0", "track 1")
    playing_capture = await rig.capture(playing)

    with suppress(Exception):
        await rig.ask(upcoming, wait=0.5)

    assert playing_capture.is_buffering
