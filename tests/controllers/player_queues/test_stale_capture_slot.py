"""
Tests for the stream slot of a music service that allows one source stream at a time.

The server prepares the queue's next track ahead of time. For a source that delivers its audio
at playback pace, that capture holds the service's only stream slot until the track has played
out. When the queue changes so that the prepared track is no longer the one that plays next,
the track that does play next needs that slot, and the capture nobody needs has to give it up.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import suppress
from functools import partial
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.enums import (
    ContentType,
    CrossfadeMode,
    MediaType,
    PlaybackState,
    ProviderType,
    QueueOption,
    RepeatMode,
    StreamType,
)
from music_assistant_models.errors import QueueEmpty
from music_assistant_models.media_items import AudioFormat, ProviderMapping, Track
from music_assistant_models.player_queue import PlayerQueue
from music_assistant_models.queue_item import QueueItem
from music_assistant_models.streamdetails import StreamDetails

import music_assistant.controllers.streams.audio as audio_mod
from music_assistant.controllers.player_queues import PlayerQueuesController
from music_assistant.controllers.player_queues.state import PlayerQueueData
from music_assistant.controllers.streams.audio import CrossfadeHandover, StreamsAudio
from music_assistant.controllers.streams.audio_buffer import AudioBuffer
from music_assistant.mass import MusicAssistant
from music_assistant.models.music_provider import MusicProvider

INSTANCE = "single--account_x"
QUEUE_ID = "queue_a"
SESSION_ID = "session-a"
PCM_FORMAT = AudioFormat(content_type=ContentType.PCM_S16LE, sample_rate=8000, bit_depth=16)
# seconds a speaker's request may wait for the slot in these tests
SHORT_WAIT = 2.0

Source = Callable[[], AsyncGenerator[bytes]]


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


def _seconds(count: int) -> bytes:
    """Return the given number of seconds of audible PCM audio."""
    # not silence: a held-back tail reads zero-filled audio as trailing silence
    return b"\x10\x20" * (PCM_FORMAT.pcm_sample_size // 2 * count)


async def _realtime_capture() -> AsyncGenerator[bytes]:
    """Deliver the first seconds of a track, then keep capturing it far from its end."""
    for _ in range(3):
        yield _seconds(1)
    await asyncio.Event().wait()


async def _source(
    before: int, after: int = 0, gate: asyncio.Event | None = None
) -> AsyncGenerator[bytes]:
    """
    Deliver a track of the given seconds, holding at the gate after the first part.

    :param before: Seconds delivered right away.
    :param after: Seconds delivered once the gate opens.
    :param gate: The gate between the two parts, if the source holds there.
    """
    for _ in range(before):
        yield _seconds(1)
    if gate is not None:
        await gate.wait()
    for _ in range(after):
        yield _seconds(1)


def _track_item(name: str, realtime: bool = True) -> QueueItem:
    """
    Build a queue item of a track on the single-slot provider, with its stream details resolved.

    :param name: Neutral name of the track, also used for its ids.
    :param realtime: Whether its source delivers the audio at playback pace.
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
        is_realtime=realtime,
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
        self.tasks: list[asyncio.Task[Any]] = []
        # the sources of the tracks by name; a track without one is captured in real time
        self.sources: dict[str, Source] = {}
        # schedules tasks with the server's own create_task: started eagerly, tracked by id
        self._task_host = SimpleNamespace(
            _tracked_tasks={},
            loop=asyncio.get_running_loop(),
            verify_event_loop_thread=lambda _what: None,
        )
        mass = MagicMock()
        mass.get_provider.side_effect = lambda instance, **_kwargs: (
            self.provider if instance == INSTANCE else None
        )
        mass.providers = []
        mass.music.providers = []
        mass.config.get_raw_core_config_value.side_effect = lambda _core, _key, default: default
        mass.create_task.side_effect = self._create_task
        mass.cancel_task.side_effect = partial(
            MusicAssistant.cancel_task, cast("Any", self._task_host)
        )
        mass.streams.audio_analysis.start_analysis = AsyncMock()
        self.queues = PlayerQueuesController.__new__(PlayerQueuesController)
        self.queues.mass = mass
        self.queues.logger = MagicMock()
        self.queues._queue_data = {}
        self.queues.signal_update = MagicMock()  # type: ignore[method-assign]
        self.queues.load_next_queue_item = AsyncMock(  # type: ignore[method-assign]
            side_effect=self._load_next
        )
        mass.player_queues = self.queues
        self.mass = mass
        self.audio = StreamsAudio(mass)
        self.audio._get_media_stream = self._media_stream  # type: ignore[method-assign,assignment]
        mass.streams.audio = self.audio

    def play(
        self,
        *names: str,
        current: int = 0,
        repeat: RepeatMode = RepeatMode.OFF,
        realtime: bool = True,
    ) -> list[QueueItem]:
        """
        Put a queue on the rig that plays one of the given tracks.

        The playing track's own capture has finished, as it has when the server prepares the
        track after it on a single-slot source.

        :param names: The tracks of the queue, in queue order.
        :param current: Index of the track that plays.
        :param repeat: The queue's repeat mode.
        :param realtime: Whether the sources of the tracks deliver their audio at playback pace.
        """
        items = [self.track(name, realtime) for name in names]
        queue = PlayerQueue(
            queue_id=QUEUE_ID,
            active=True,
            display_name="Queue A",
            available=True,
            items=len(items),
            state=PlaybackState.PLAYING,
            current_index=current,
            current_item=items[current],
            index_in_buffer=current,
            repeat_mode=repeat,
        )
        self.queues._queue_data[QUEUE_ID] = PlayerQueueData(
            queue=queue, items=list(items), session_id=SESSION_ID
        )
        return items

    def track(self, name: str, realtime: bool = True) -> QueueItem:
        """
        Build a queue item the rig cleans up after the test.

        :param name: Neutral name of the track.
        :param realtime: Whether its source delivers the audio at playback pace.
        """
        item = _track_item(name, realtime)
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
        """Wait for the work the controller scheduled, including what that work schedules."""
        while pending := [task for task in self.tasks if not task.done()]:
            await asyncio.gather(*pending)

    async def close(self) -> None:
        """Stop every capture and task the test left running."""
        for task in self.tasks:
            task.cancel()
        for item in self.items:
            if item.streamdetails and (buffer := item.streamdetails.buffer):
                await buffer.clear()

    def _create_task(self, target: Awaitable[Any], *args: Any, **kwargs: Any) -> asyncio.Task[Any]:
        """Start a task the controller schedules as the server does, keeping it for settle."""
        task = MusicAssistant.create_task(cast("Any", self._task_host), target, *args, **kwargs)
        self.tasks.append(task)
        return task

    async def _load_next(self, queue_id: str, current_item_id: str) -> QueueItem:
        """Find the item after the given one; its stream details are resolved up front."""
        if (next_item := self.queues.get_next_item(queue_id, current_item_id)) is None:
            raise QueueEmpty("No more tracks left in the queue.")
        return next_item

    def _media_stream(self, streamdetails: StreamDetails, *_args: Any) -> AsyncGenerator[bytes]:
        """Open the source of a track, as the provider would."""
        name = streamdetails.item_id.removeprefix("track-")
        return self.sources.get(name, _realtime_capture)()


def _install_flow(
    rig: _Rig, monkeypatch: pytest.MonkeyPatch, while_building: Callable[[], Awaitable[None]]
) -> None:
    """
    Make the rig play flow streams with crossfades, with only the audio decoding left out.

    :param rig: The rig whose queue the flow streams play.
    :param monkeypatch: The test's monkeypatch.
    :param while_building: Runs while a flow stream builds its crossfade into the next track.
    """
    rig.mass.streams.get_crossfade_mode.return_value = CrossfadeMode.STANDARD_CROSSFADE
    # the player reports no progress here, so nothing would ever let these finish
    rig.queues._preload_next_item = MagicMock()  # type: ignore[method-assign]
    rig.queues.queue_buffer_completed = MagicMock()  # type: ignore[method-assign]
    rig.audio.setup()

    async def _item_stream(
        queue_item: QueueItem,
        seek_position: int = 0,
        prepared_buffer: AudioBuffer | None = None,
        **_kwargs: Any,
    ) -> AsyncGenerator[bytes]:
        # the real stream without its decoding: the item's own buffer, refused like the
        # real one refuses a prepared buffer that is no longer the item's or no longer valid
        assert queue_item.streamdetails is not None
        if prepared_buffer is not None and (
            queue_item.streamdetails.buffer is not prepared_buffer
            or not prepared_buffer.is_valid(seek_position * 1000)
        ):
            return
        buffer = prepared_buffer or await rig.ask(queue_item)
        assert buffer is not None
        async for chunk in buffer.get_raw_stream(seek_position * 1000):
            yield chunk

    async def _build(**_kwargs: Any) -> SimpleNamespace:
        await while_building()
        return SimpleNamespace(
            timing_info=SimpleNamespace(
                fadein_trimmed_duration=0.0, crossfade_duration=8.0, pre_crossfade_duration=0.0
            )
        )

    async def _concat_mix(
        _smart_fade: object, *, fade_in_part: AsyncGenerator[bytes], fade_out_part: bytes, **_: Any
    ) -> AsyncGenerator[bytes]:
        # a lossless stand-in for the mixer, so every byte of both tracks stays countable
        yield fade_out_part
        async for chunk in fade_in_part:
            yield chunk

    monkeypatch.setattr(rig.audio, "get_queue_item_stream", _item_stream)
    monkeypatch.setattr(rig.audio.smart_fades_mixer, "build", AsyncMock(side_effect=_build))
    monkeypatch.setattr(rig.audio.smart_fades_mixer, "mix", _concat_mix)


async def _drain(stream: AsyncGenerator[bytes]) -> int:
    """Consume a stream as a player does and return how many bytes it delivered."""
    emitted = 0
    async for chunk in stream:
        emitted += len(chunk)
        await asyncio.sleep(0)
    return emitted


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


async def test_the_track_after_the_wrap_gives_its_slot_to_a_track_played_next(rig: _Rig) -> None:
    """With repeat on, the first track prepared to follow the last one makes way for play next."""
    first, _second, _third, playing = rig.play(
        "track 0", "track 1", "track 2", "track 3", current=3, repeat=RepeatMode.ALL
    )
    prepared_capture = await rig.prepare_next(playing)
    assert rig.queues.get_next_item(QUEUE_ID, playing.queue_item_id) is first
    new_next = rig.track("track 5")

    await rig.queues._enqueue_with_option(QUEUE_ID, [new_next], QueueOption.NEXT)
    buffer = await rig.ask(new_next)

    assert buffer is not None
    assert buffer.is_buffering
    assert not prepared_capture.is_buffering


async def test_a_track_the_flow_stream_took_plays_in_full_when_the_queue_changes(
    rig: _Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    A track the flow stream took to play next keeps its source through a change of the queue.

    The change lands while the flow stream builds its crossfade into that track, before any of
    the track's audio is out.
    """
    outgoing, incoming = rig.play("track 0", "track 1", realtime=False)
    queue = rig.queues._queue_data[QUEUE_ID].queue
    gate = asyncio.Event()
    rig.sources["track 0"] = partial(_source, 30)
    rig.sources["track 1"] = partial(_source, 20, 10, gate)

    async def _play_next_while_the_fade_is_built() -> None:
        await rig.queues._enqueue_with_option(
            QUEUE_ID, [rig.track("track 5", realtime=False)], QueueOption.NEXT
        )
        await rig.settle()
        gate.set()

    _install_flow(rig, monkeypatch, _play_next_while_the_fade_is_built)
    emitted = await _drain(rig.audio.get_queue_flow_stream(queue, outgoing, PCM_FORMAT, SESSION_ID))

    # all of both tracks, 30 seconds each
    assert emitted == 60 * PCM_FORMAT.pcm_sample_size
    assert incoming.streamdetails is not None
    assert incoming.streamdetails.buffer is not None
    assert not incoming.streamdetails.buffer.cancelled


async def test_a_superseded_flow_stream_leaves_the_track_of_the_active_one_alone(
    rig: _Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    A flow stream that a newer one took over does not touch what the newer one plays next.

    The old stream is still loading its next track when a skip starts a new flow stream, which
    takes its own next track and builds the crossfade into it while the old load returns.
    """
    first, _old_next, playing, incoming = rig.play(
        "track 0", "track 1", "track 2", "track 3", realtime=False
    )
    queue_data = rig.queues._queue_data[QUEUE_ID]
    gate, old_load_waits, old_load_returns = asyncio.Event(), asyncio.Event(), asyncio.Event()
    rig.sources["track 0"] = partial(_source, 2)
    rig.sources["track 2"] = partial(_source, 30)
    rig.sources["track 3"] = partial(_source, 20, 10, gate)

    async def _load_next(queue_id: str, current_item_id: str) -> QueueItem:
        if current_item_id == first.queue_item_id:
            # the old stream's next track takes its time to load
            old_load_waits.set()
            await old_load_returns.wait()
        return await rig._load_next(queue_id, current_item_id)

    rig.queues.load_next_queue_item = AsyncMock(side_effect=_load_next)  # type: ignore[method-assign]
    old_stream = asyncio.ensure_future(
        _drain(rig.audio.get_queue_flow_stream(queue_data.queue, first, PCM_FORMAT, SESSION_ID))
    )

    async def _old_load_returns_then_play_next() -> None:
        old_load_returns.set()
        await old_stream
        await rig.queues._enqueue_with_option(
            QUEUE_ID, [rig.track("track 5", realtime=False)], QueueOption.NEXT
        )
        await rig.settle()
        gate.set()

    _install_flow(rig, monkeypatch, _old_load_returns_then_play_next)
    await old_load_waits.wait()
    # a skip starts a new session, whose flow stream plays from the third track on
    queue_data.session_id = "session-b"
    queue_data.queue.current_index = queue_data.queue.index_in_buffer = 2
    queue_data.queue.current_item = playing
    emitted = await _drain(
        rig.audio.get_queue_flow_stream(queue_data.queue, playing, PCM_FORMAT, "session-b")
    )

    # all of the playing track and of the one it took to play next, 30 seconds each
    assert emitted == 60 * PCM_FORMAT.pcm_sample_size
    assert incoming.streamdetails is not None
    assert incoming.streamdetails.buffer is not None
    assert not incoming.streamdetails.buffer.cancelled


async def test_a_track_handed_to_another_queue_keeps_its_capture_after_a_fade_into_it(
    rig: _Rig,
) -> None:
    """A track that went from this queue to another one is that queue's to play."""
    playing, prepared, _later = rig.play("track 0", "track 1", "track 2")
    prepared_capture = await rig.prepare_next(playing)
    handoff = asyncio.Event()
    rig.audio._crossfade_pending[QUEUE_ID] = (prepared.queue_item_id, handoff)
    await rig.queues._enqueue_with_option(
        QUEUE_ID, [rig.track("track 5")], QueueOption.REPLACE_NEXT
    )

    # while the fade into it is still being mixed, another queue takes the track over and
    # plays on from its capture
    rig.queues._queue_data["queue_b"] = PlayerQueueData(
        queue=PlayerQueue(
            queue_id="queue_b", active=True, display_name="Queue B", available=True, items=1
        ),
        items=[prepared],
        session_id="session-b",
    )
    prepared.queue_id = "queue_b"
    rig.audio._release_handoff(QUEUE_ID, handoff)
    await rig.settle()

    assert prepared_capture.is_buffering
    assert prepared.streamdetails is not None
    assert prepared.streamdetails.buffer is prepared_capture


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
    ("current_index", "index_in_buffer"),
    [(1, 1), (0, 1), (1, 2)],
    # it plays; the player took it while the one before still plays; it plays while the player
    # already took the one after it
    ids=["playing", "taken_by_the_player", "playing_with_the_next_taken"],
)
@pytest.mark.parametrize(
    "option", [QueueOption.REPLACE_NEXT, QueueOption.NEXT], ids=["replace_next", "next"]
)
async def test_a_prepared_track_that_plays_keeps_its_capture_when_what_follows_it_changes(
    rig: _Rig, option: QueueOption, current_index: int, index_in_buffer: int
) -> None:
    """Once the prepared track plays or the player took it, changes after it leave it alone."""
    first, _prepared, _later = rig.play("track 0", "track 1", "track 2")
    prepared_capture = await rig.prepare_next(first)
    queue = rig.queues._queue_data[QUEUE_ID].queue
    queue.current_index = current_index
    queue.current_item = rig.queues.get_item(QUEUE_ID, current_index)
    queue.index_in_buffer = index_in_buffer

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
