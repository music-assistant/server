"""
A rig for tests on who uses an audio source and who holds a provider's stream slot.

It runs the real queue controller, the real streams audio side, real audio buffers and the
provider's own stream slots over sources the test controls. Only the audio decoding, the
crossfade mixer and the players are left out.
"""

from __future__ import annotations

import asyncio
from collections import Counter
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import nullcontext, suppress
from functools import partial
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock, MagicMock

from music_assistant_models.enums import (
    ContentType,
    CrossfadeMode,
    MediaType,
    PlaybackState,
    PlayerType,
    ProviderType,
    RepeatMode,
    StreamType,
    VolumeNormalizationMode,
)
from music_assistant_models.errors import AudioError, QueueEmpty
from music_assistant_models.media_items import AudioFormat, ProviderMapping, Track
from music_assistant_models.player_queue import PlayerQueue
from music_assistant_models.queue_item import QueueItem
from music_assistant_models.streamdetails import StreamDetails

from music_assistant.controllers.player_queues import PlayerQueuesController
from music_assistant.controllers.player_queues.state import PlayerQueueData
from music_assistant.controllers.streams.audio import StreamsAudio, _ItemClaim
from music_assistant.controllers.streams.audio_buffer import AudioBuffer
from music_assistant.mass import MusicAssistant
from music_assistant.models.music_provider import MusicProvider

if TYPE_CHECKING:
    import pytest

INSTANCE = "single--account_x"
QUEUE_ID = "queue_a"
OTHER_QUEUE_ID = "queue_b"
SESSION_ID = "session-a"
PCM_FORMAT = AudioFormat(content_type=ContentType.PCM_S16LE, sample_rate=8000, bit_depth=16)
# seconds a speaker's request may wait for the slot in these tests
SHORT_WAIT = 2.0
# seconds a source that is slow to stop holds on at most
SLOW_STOP_LIMIT = 3.0

Source = Callable[[], AsyncGenerator[bytes]]


class SingleStreamProvider(MusicProvider):
    """Streaming provider that allows one source stream per account."""

    @property
    def max_concurrent_streams(self) -> int:
        """Return one source slot."""
        return 1


class TwoStreamProvider(MusicProvider):
    """Streaming provider that allows two source streams per account."""

    @property
    def max_concurrent_streams(self) -> int:
        """Return two source slots."""
        return 2


class UnlimitedProvider(MusicProvider):
    """Provider that does not limit its source streams."""

    @property
    def max_concurrent_streams(self) -> None:
        """Return no limit."""
        return


def seconds(count: int) -> bytes:
    """Return the given number of seconds of audible PCM audio."""
    # not silence: a held-back tail reads zero-filled audio as trailing silence
    return b"\x10\x20" * (PCM_FORMAT.pcm_sample_size // 2 * count)


async def realtime_capture() -> AsyncGenerator[bytes]:
    """Deliver the first seconds of a track, then keep capturing it far from its end."""
    for _ in range(3):
        yield seconds(1)
    await asyncio.Event().wait()


async def slow_to_stop_capture(stopped: asyncio.Event) -> AsyncGenerator[bytes]:
    """
    Capture a track in real time, and keep its slot after it is told to stop until allowed to go.

    :param stopped: Set by the test once the source may finish unwinding.
    """
    try:
        for _ in range(3):
            yield seconds(1)
        await asyncio.Event().wait()
    finally:
        # however often it is told to stop meanwhile, though never longer than a test may take
        deadline = asyncio.get_running_loop().time() + SLOW_STOP_LIMIT
        while not stopped.is_set() and asyncio.get_running_loop().time() < deadline:
            with suppress(asyncio.CancelledError, TimeoutError):
                async with asyncio.timeout_at(deadline):
                    await stopped.wait()


async def source(
    before: int, after: int = 0, gate: asyncio.Event | None = None, fails: bool = False
) -> AsyncGenerator[bytes]:
    """
    Deliver a track of the given seconds, holding at the gate after the first part.

    :param before: Seconds delivered right away.
    :param after: Seconds delivered once the gate opens.
    :param gate: The gate between the two parts, if the source holds there.
    :param fails: Whether the source breaks off with an error where it would deliver the
        second part.
    """
    for _ in range(before):
        yield seconds(1)
    if gate is not None:
        await gate.wait()
    if fails:
        raise AudioError("the source broke off")
    for _ in range(after):
        yield seconds(1)


def track_item(name: str, realtime: bool = True, queue_id: str = QUEUE_ID) -> QueueItem:
    """
    Build a queue item of a track on the rig's provider, with its stream details resolved.

    :param name: Neutral name of the track, also used for its ids.
    :param realtime: Whether its source delivers the audio at playback pace.
    :param queue_id: The queue the item is for.
    """
    mapping = ProviderMapping(
        item_id=f"track-{name}",
        provider_domain="single",
        provider_instance=INSTANCE,
        audio_format=PCM_FORMAT,
    )
    item = QueueItem(
        queue_id=queue_id,
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
        # a track's buffer keeps its audio for every reader, unlike the short window of a
        # live stream
        can_seek=True,
        allow_seek=True,
        queue_id=queue_id,
        is_realtime=realtime,
    )
    return item


class SlotRig:
    """One playing queue, with the real queue and buffer logic over one provider."""

    def __init__(self, provider_cls: type[MusicProvider] = SingleStreamProvider) -> None:
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
        # how often the source of each track was opened
        self.opened: Counter[str] = Counter()
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
        mass.streams.audio_analysis.get_audio_analysis = AsyncMock(return_value=None)
        mass.streams.source_normalizes_audio.return_value = False
        self.queues = PlayerQueuesController.__new__(PlayerQueuesController)
        self.queues.mass = mass
        self.queues.logger = MagicMock()
        self.queues._queue_data = {}
        self.queues.signal_update = MagicMock()  # type: ignore[method-assign]
        self.queues.load_next_queue_item = AsyncMock(  # type: ignore[method-assign]
            side_effect=self._load_next
        )
        # the players report no progress here, so nothing would ever let these finish
        self.queues._preload_next_item = MagicMock()  # type: ignore[method-assign]
        self.queues.queue_buffer_completed = MagicMock()  # type: ignore[method-assign]
        mass.player_queues = self.queues
        self.mass = mass
        self.audio = StreamsAudio(mass)
        self.audio._get_media_stream = self._media_stream  # type: ignore[method-assign,assignment]
        # the item stream is the real one; only what it reports to the outside is left out
        self.audio._get_volume_normalization_preference = MagicMock(  # type: ignore[method-assign]
            return_value=VolumeNormalizationMode.MEASUREMENT_ONLY
        )
        self.audio._notify_provider_streamed = MagicMock()  # type: ignore[method-assign]
        mass.streams.audio = self.audio

    def play(
        self,
        *names: str,
        current: int = 0,
        repeat: RepeatMode = RepeatMode.OFF,
        realtime: bool = True,
        queue_id: str = QUEUE_ID,
        session_id: str = SESSION_ID,
    ) -> list[QueueItem]:
        """
        Put a queue on the rig that plays one of the given tracks.

        The playing track's own capture has finished, as it has when the server prepares the
        track after it on a single-slot source.

        :param names: The tracks of the queue, in queue order.
        :param current: Index of the track that plays.
        :param repeat: The queue's repeat mode.
        :param realtime: Whether the sources of the tracks deliver their audio at playback pace.
        :param queue_id: The queue to put on the rig.
        :param session_id: The playback session of that queue.
        """
        items = [self.track(name, realtime, queue_id) for name in names]
        queue = PlayerQueue(
            queue_id=queue_id,
            active=True,
            display_name=f"Queue {queue_id[-1].upper()}",
            available=True,
            items=len(items),
            state=PlaybackState.PLAYING,
            current_index=current,
            current_item=items[current],
            index_in_buffer=current,
            repeat_mode=repeat,
        )
        self.queues._queue_data[queue_id] = PlayerQueueData(
            queue=queue, items=list(items), session_id=session_id
        )
        return items

    def track(self, name: str, realtime: bool = True, queue_id: str = QUEUE_ID) -> QueueItem:
        """
        Build a queue item the rig cleans up after the test.

        :param name: Neutral name of the track.
        :param realtime: Whether its source delivers the audio at playback pace.
        :param queue_id: The queue the item is for.
        """
        item = track_item(name, realtime, queue_id)
        self.items.append(item)
        return item

    async def prepare_next(self, current: QueueItem) -> AudioBuffer:
        """
        Let the server prepare the track after the given one, as it does ahead of playback.

        :param current: The track that is playing.
        :return: The buffer the next track's capture fills.
        """
        self.queues.prepare_next_audio_buffer(current.queue_id, current.queue_item_id)
        await self.settle()
        prepared = self.queues.get_next_item(current.queue_id, current.queue_item_id)
        assert prepared is not None
        return self.buffer(prepared)

    async def capture(self, item: QueueItem) -> AudioBuffer:
        """
        Start the capture of the given item, which takes one of the provider's slots.

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

        The request claims the item for the playback session of its queue while it asks.

        :param item: The queue item the speaker asks for.
        :param wait: Seconds the request may wait for a source slot.
        """
        claim = _ItemClaim(f"request for {item.name}", self.audio._playback_session(item))
        self.audio._claim_item(claim, item)
        try:
            return await self.audio.get_audio_buffer(
                item, reason="streaming", capacity_wait_timeout=wait
            )
        finally:
            self.audio._claim_item(claim, None)

    def buffer(self, item: QueueItem) -> AudioBuffer:
        """
        Return the buffer attached to the given item.

        :param item: The queue item whose source to look at.
        """
        assert item.streamdetails is not None
        buffer = item.streamdetails.buffer
        assert isinstance(buffer, AudioBuffer)
        return buffer

    def fills(self, buffer: AudioBuffer) -> bool:
        """
        Return whether the source of the given buffer still delivers audio.

        :param buffer: The buffer to look at.
        """
        return buffer.is_buffering

    def slot_free(self) -> bool:
        """Return whether the provider has a source-stream slot to spare."""
        return self.provider.has_available_stream_slot

    async def delivered(self, item: QueueItem) -> AudioBuffer:
        """
        Wait until the source of the given item has delivered everything and let go of its slot.

        :param item: The queue item whose source to wait for.
        :return: The buffer that holds the item's audio.
        """
        buffer = self.buffer(item)
        while buffer.is_buffering:
            await asyncio.sleep(0)
        return buffer

    async def settle(self) -> None:
        """Wait for the work the controller scheduled and for sources that are being stopped."""
        # a stop that was only just scheduled starts with the next turn of the loop
        await asyncio.sleep(0)
        while pending := self._pending():
            await asyncio.gather(*pending, return_exceptions=True)
            await asyncio.sleep(0)

    async def transfer_to(self, target_id: str) -> None:
        """
        Hand the playing queue over to another player with the queue's own transfer.

        :param target_id: The queue of the player the items move to.
        """
        self.queues._queue_data[target_id] = PlayerQueueData(
            queue=PlayerQueue(
                queue_id=target_id, active=True, display_name="Target", available=True, items=0
            )
        )
        self.mass.players.get_player.return_value = SimpleNamespace(
            type=PlayerType.PLAYER,
            synced_to=None,
            active_group=None,
            offline_by_power_control=False,
            extra_data={},
        )
        self.mass.players.get_playback_locks = lambda *_player_ids: nullcontext()
        self.mass.players._handle_cmd_stop = AsyncMock()
        self.queues.is_smart_shuffle_active = MagicMock(return_value=False)  # type: ignore[method-assign]
        # the player double reports nothing back after the stop
        self.queues.on_player_update = MagicMock()  # type: ignore[method-assign]
        self.queues._managed_pool = MagicMock()
        await self.queues.transfer_queue(QUEUE_ID, target_id, auto_play=False)
        await self.settle()

    async def close(self) -> None:
        """Stop every capture and task the test left running."""
        for task in self.tasks:
            task.cancel()
        for item in self.items:
            if item.streamdetails and (buffer := item.streamdetails.buffer):
                await buffer.clear()

    def _pending(self) -> list[asyncio.Task[Any]]:
        """Return the controller's running tasks and the sources that are being stopped."""
        pending = [task for task in self.tasks if not task.done()]
        for item in self.items:
            if (
                item.streamdetails
                and isinstance(buffer := item.streamdetails.buffer, AudioBuffer)
                and (producer := buffer._producer_task) is not None
                and not producer.done()
                and producer.cancelling()
            ):
                pending.append(producer)
        return pending

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
        self.opened[name] += 1
        return self.sources.get(name, realtime_capture)()


def count_starts(rig: SlotRig, name: str) -> list[str]:
    """
    Record every time the source of the given track really starts to deliver.

    :param rig: The rig whose source to watch.
    :param name: Neutral name of the track.
    :return: A list that gets the name for every start of the source.
    """
    started: list[str] = []
    inner = rig.sources.get(name)

    async def _source() -> AsyncGenerator[bytes]:
        started.append(name)
        async for chunk in (inner or partial(source, 3, 0, asyncio.Event()))():
            yield chunk

    rig.sources[name] = _source
    return started


def allow_commands(rig: SlotRig) -> None:
    """Let the rig's queue take commands without players, users or settings behind it."""
    rig.queues._check_player_permission = MagicMock()  # type: ignore[method-assign]
    rig.queues._resolve_queue_id = lambda queue_id: queue_id  # type: ignore[method-assign]
    rig.queues._resolve_default_toggles = MagicMock()  # type: ignore[method-assign]
    rig.queues._enqueue_next_item = MagicMock()  # type: ignore[method-assign]


def allow_starts(rig: SlotRig) -> None:
    """Let the rig's queue start a track without a player behind it."""
    allow_commands(rig)
    rig.queues.player_media_from_queue_item = AsyncMock()  # type: ignore[method-assign]
    rig.mass.players.get_player.return_value.offline_by_power_control = False
    rig.mass.players.play_media = AsyncMock()


def install_mixer(
    rig: SlotRig,
    monkeypatch: pytest.MonkeyPatch,
    while_building: Callable[[], Awaitable[None]] | None = None,
    crossfade_duration: float = 8.0,
) -> None:
    """
    Make the rig crossfade, with a lossless stand-in for the mixer.

    The flow stream, the item streams and the incoming fade prefetcher are the real ones.

    :param rig: The rig whose queues crossfade.
    :param monkeypatch: The test's monkeypatch.
    :param while_building: Runs while a stream builds its crossfade into the next track.
    :param crossfade_duration: Seconds of the mix that go to the outgoing track's stream.
    """
    rig.mass.streams.get_crossfade_mode.return_value = CrossfadeMode.STANDARD_CROSSFADE
    rig.audio.setup()

    async def _build(**_kwargs: Any) -> SimpleNamespace:
        if while_building is not None:
            await while_building()
        return SimpleNamespace(
            timing_info=SimpleNamespace(
                fadein_trimmed_duration=0.0,
                crossfade_duration=crossfade_duration,
                pre_crossfade_duration=0.0,
                post_crossfade_duration=0.0,
            )
        )

    async def _concat_mix(
        _smart_fade: object, *, fade_in_part: AsyncGenerator[bytes], fade_out_part: bytes, **_: Any
    ) -> AsyncGenerator[bytes]:
        # every byte of both tracks stays countable
        yield fade_out_part
        async for chunk in fade_in_part:
            yield chunk

    monkeypatch.setattr(rig.audio.smart_fades_mixer, "build", AsyncMock(side_effect=_build))
    monkeypatch.setattr(rig.audio.smart_fades_mixer, "mix", _concat_mix)


async def drain(stream: AsyncGenerator[bytes]) -> int:
    """Consume a stream as a player does and return how many bytes it delivered."""
    emitted = 0
    async for chunk in stream:
        emitted += len(chunk)
        await asyncio.sleep(0)
    return emitted
