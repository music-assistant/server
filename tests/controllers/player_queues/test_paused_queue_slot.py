"""
Tests for a paused queue handing its provider stream slot to another queue.

A queue paused on a player that really pauses keeps its session and its source buffers, and
with them a provider's stream slot, until the pause watcher stops it. A provider that allows
one stream would make a play on any other player fail in that window.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.enums import ContentType, MediaType, PlaybackState, StreamType
from music_assistant_models.errors import PlayerUnavailableError
from music_assistant_models.media_items import AudioFormat, ProviderMapping, SoundEffect
from music_assistant_models.player_queue import PlayerQueue
from music_assistant_models.queue_item import QueueItem
from music_assistant_models.streamdetails import StreamDetails

import music_assistant.controllers.streams.audio as audio_mod
from music_assistant.constants import ATTR_ANNOUNCEMENT_IN_PROGRESS
from music_assistant.controllers.player_queues import PlayerQueuesController
from music_assistant.controllers.player_queues.state import PlayerQueueData
from music_assistant.controllers.streams.audio import StreamsAudio
from music_assistant.controllers.streams.audio_buffer import AudioBuffer
from music_assistant.models.music_provider import MusicProvider, ProviderStreamLimitError

INSTANCE = "spotify--one"
PAUSED_QUEUE = "living_room"
STARTING_QUEUE = "kitchen"
PCM_FORMAT = AudioFormat(content_type=ContentType.PCM_S16LE, sample_rate=8000, bit_depth=16)


class _SingleStreamProvider(MusicProvider):
    """Streaming provider with one source slot."""

    @property
    def max_concurrent_streams(self) -> int:
        """Return one source slot."""
        return 1


def _provider() -> _SingleStreamProvider:
    """Construct a provider that lets one source stream run at a time."""
    manifest = MagicMock(domain="spotify")
    manifest.name = "Spotify"
    config = MagicMock(instance_id=INSTANCE)
    config.name = "Spotify"
    config.get_value.return_value = "GLOBAL"
    return _SingleStreamProvider(MagicMock(), manifest, config)


def _queue_item(queue_id: str) -> QueueItem:
    """Build a queue item of the provider, with its stream details resolved."""
    mapping = ProviderMapping(
        item_id=f"track-{queue_id}",
        provider_domain="spotify",
        provider_instance=INSTANCE,
        audio_format=PCM_FORMAT,
    )
    item = QueueItem(
        queue_id=queue_id,
        queue_item_id=f"item-{queue_id}",
        name=f"Track on {queue_id}",
        duration=600,
        media_item=SoundEffect(
            item_id=mapping.item_id,
            provider=INSTANCE,
            name=f"Track on {queue_id}",
            provider_mappings={mapping},
        ),
    )
    item.streamdetails = StreamDetails(
        provider=INSTANCE,
        item_id=mapping.item_id,
        audio_format=PCM_FORMAT,
        media_type=MediaType.SOUND_EFFECT,
        stream_type=StreamType.CUSTOM,
        duration=600,
        queue_id=queue_id,
    )
    return item


async def _endless_source(*_args: Any, **_kwargs: Any) -> AsyncGenerator[bytes]:
    """Deliver a few seconds of audio, then keep the source open like a track far from its end."""
    for _ in range(3):
        yield b"\x00" * PCM_FORMAT.pcm_sample_size
    await asyncio.Event().wait()


class _Rig:
    """A queue controller and the streams side of it, sharing one single-slot provider."""

    def __init__(self) -> None:
        self.provider = _provider()
        self.players: dict[str, SimpleNamespace] = {}
        self.tasks: list[asyncio.Future[Any]] = []
        mass = MagicMock()
        mass.get_provider.side_effect = lambda instance, **_kwargs: (
            self.provider if instance == INSTANCE else None
        )
        mass.config.get_raw_core_config_value.side_effect = lambda _core, _key, default: default
        mass.create_task.side_effect = self._create_task
        mass.players.get_player.side_effect = self.players.get
        self.stop_device = AsyncMock(side_effect=self._device_stopped)
        mass.players._handle_cmd_stop = self.stop_device
        self._locks: dict[str, asyncio.Lock] = {}
        self._lock_owners: dict[str, asyncio.Task[Any] | None] = {}
        mass.players.get_group_and_player_lock = self.playback_lock
        self.mass = mass
        self.queues = PlayerQueuesController.__new__(PlayerQueuesController)
        self.queues.mass = mass
        self.queues.logger = MagicMock()
        self.queues._queue_data = {}
        self.queues.signal_update = MagicMock()  # type: ignore[method-assign]
        self.queues.on_player_update = MagicMock()  # type: ignore[method-assign]
        mass.player_queues = self.queues
        self.audio = StreamsAudio(mass)
        self.audio._get_media_stream = _endless_source  # type: ignore[method-assign]
        mass.streams.audio = self.audio

    def add_queue(self, queue_id: str, state: PlaybackState) -> QueueItem:
        """Register a queue and its player in the given state, holding one item."""
        item = _queue_item(queue_id)
        queue = PlayerQueue(
            queue_id=queue_id,
            active=state != PlaybackState.IDLE,
            display_name=queue_id,
            available=True,
            items=1,
            state=state,
            current_index=0,
            current_item=item,
        )
        self.queues._queue_data[queue_id] = PlayerQueueData(
            queue=queue, items=[item], session_id=f"session-{queue_id}"
        )
        self.players[queue_id] = SimpleNamespace(
            player_id=queue_id,
            state=SimpleNamespace(playback_state=state),
            extra_data={},
        )
        return item

    async def fill(self, item: QueueItem) -> AudioBuffer:
        """Start the item's source buffer, which takes the provider's slot."""
        assert item.streamdetails is not None
        item.streamdetails.queue_session_id = self.queues._queue_data[item.queue_id].session_id
        return await AudioBuffer.get_buffer(
            self.mass, item.streamdetails, wait_ready=True, source_wait_timeout=0
        )

    @contextlib.asynccontextmanager
    async def playback_lock(self, player_id: str) -> AsyncIterator[None]:
        """Hold a player's playback lock, re-entrant within one task like the real one."""
        task = asyncio.current_task()
        if self._lock_owners.get(player_id) is task:
            yield
            return
        async with self._locks.setdefault(player_id, asyncio.Lock()):
            self._lock_owners[player_id] = task
            try:
                yield
            finally:
                self._lock_owners[player_id] = None

    def _create_task(self, target: Awaitable[Any], **_kwargs: Any) -> asyncio.Future[Any]:
        """Run a task the controller schedules, keeping it so a test can wait for it."""
        task = asyncio.ensure_future(target)
        self.tasks.append(task)
        return task

    async def _device_stopped(self, queue_id: str) -> None:
        """Let the player report idle once it was told to stop."""
        self.players[queue_id].state.playback_state = PlaybackState.IDLE


@pytest.fixture
def rig(monkeypatch: pytest.MonkeyPatch) -> _Rig:
    """Build the rig with every music source open to the queues' playback."""
    monkeypatch.setattr(audio_mod, "playback_sources", AsyncMock(return_value=(None, [])))
    return _Rig()


async def test_a_paused_queue_hands_its_slot_to_a_start_on_another_queue(rig: _Rig) -> None:
    """The paused queue is stopped for real, and the other queue takes the only slot."""
    paused_item = rig.add_queue(PAUSED_QUEUE, PlaybackState.PAUSED)
    paused_buffer = await rig.fill(paused_item)
    assert not rig.provider.has_available_stream_slot
    starting_item = rig.add_queue(STARTING_QUEUE, PlaybackState.IDLE)

    buffer = await rig.audio.get_audio_buffer(
        starting_item, reason="prepare", capacity_wait_timeout=2
    )

    assert buffer.is_buffering
    rig.stop_device.assert_awaited_once_with(PAUSED_QUEUE)
    assert rig.queues._queue_data[PAUSED_QUEUE].session_id is None
    assert paused_buffer.cancelled
    await buffer.clear()


async def test_a_paused_player_that_cannot_be_reached_still_hands_over_its_slot(
    rig: _Rig,
) -> None:
    """The paused queue's session is torn down anyway, so its player's failure stays its own."""
    paused_item = rig.add_queue(PAUSED_QUEUE, PlaybackState.PAUSED)
    paused_buffer = await rig.fill(paused_item)
    rig.stop_device.side_effect = PlayerUnavailableError("gone")
    starting_item = rig.add_queue(STARTING_QUEUE, PlaybackState.IDLE)

    buffer = await rig.audio.get_audio_buffer(
        starting_item, reason="prepare", capacity_wait_timeout=2
    )

    assert buffer.is_buffering
    assert rig.queues._queue_data[PAUSED_QUEUE].session_id is None
    assert paused_buffer.cancelled
    await buffer.clear()


async def test_a_stop_that_ended_no_session_reports_no_freed_slot(rig: _Rig) -> None:
    """Only a paused queue whose session really ended counts as a handed over slot."""
    paused_item = rig.add_queue(PAUSED_QUEUE, PlaybackState.PAUSED)
    paused_buffer = await rig.fill(paused_item)
    rig.queues._handle_stop = AsyncMock(side_effect=RuntimeError("broken"))  # type: ignore[method-assign]

    assert not await rig.queues.release_paused_stream_slot(INSTANCE, STARTING_QUEUE)

    assert rig.queues._queue_data[PAUSED_QUEUE].session_id is not None
    await paused_buffer.clear()


async def test_a_playing_queue_keeps_its_slot(rig: _Rig) -> None:
    """Only a paused queue gives up its slot; a start elsewhere waits for it as before."""
    playing_item = rig.add_queue(PAUSED_QUEUE, PlaybackState.PLAYING)
    playing_buffer = await rig.fill(playing_item)
    starting_item = rig.add_queue(STARTING_QUEUE, PlaybackState.IDLE)

    with pytest.raises(ProviderStreamLimitError):
        await rig.audio.get_audio_buffer(starting_item, reason="prepare", capacity_wait_timeout=0.2)

    rig.stop_device.assert_not_awaited()
    assert playing_buffer.is_buffering
    await playing_buffer.clear()


@pytest.mark.parametrize(
    ("asking_queue", "queue_state", "player_state", "announcing"),
    [
        (PAUSED_QUEUE, PlaybackState.PAUSED, PlaybackState.PAUSED, False),
        (STARTING_QUEUE, PlaybackState.PAUSED, PlaybackState.PLAYING, False),
        (STARTING_QUEUE, PlaybackState.IDLE, PlaybackState.PAUSED, False),
        (STARTING_QUEUE, PlaybackState.PAUSED, PlaybackState.PAUSED, True),
    ],
    ids=[
        "the-asking-queue-itself",
        "player-playing-again",
        "player-paused-on-another-source",
        "announcement-in-progress",
    ],
)
async def test_only_another_queue_paused_on_its_player_gives_up_its_slot(
    rig: _Rig,
    asking_queue: str,
    queue_state: PlaybackState,
    player_state: PlaybackState,
    announcing: bool,
) -> None:
    """A queue that is not paused for sure, or is the one asking, keeps its slot."""
    paused_item = rig.add_queue(PAUSED_QUEUE, PlaybackState.PAUSED)
    paused_buffer = await rig.fill(paused_item)
    rig.queues._queue_data[PAUSED_QUEUE].queue.state = queue_state
    player = rig.players[PAUSED_QUEUE]
    player.state.playback_state = player_state
    player.extra_data[ATTR_ANNOUNCEMENT_IN_PROGRESS] = announcing

    assert not rig.queues.has_paused_stream_slot_holder(INSTANCE, asking_queue)
    assert not await rig.queues.release_paused_stream_slot(INSTANCE, asking_queue)

    await asyncio.gather(*rig.tasks)
    rig.stop_device.assert_not_awaited()
    assert paused_buffer.is_buffering
    await paused_buffer.clear()


async def test_a_queue_that_resumes_before_it_is_stopped_keeps_playing(rig: _Rig) -> None:
    """The stop waits for the queue's playback lock, and a resume holding it wins."""
    paused_item = rig.add_queue(PAUSED_QUEUE, PlaybackState.PAUSED)
    paused_buffer = await rig.fill(paused_item)
    lock_taken = asyncio.Event()
    resumed = asyncio.Event()

    async def _resume() -> None:
        async with rig.playback_lock(PAUSED_QUEUE):
            lock_taken.set()
            await resumed.wait()
            rig.players[PAUSED_QUEUE].state.playback_state = PlaybackState.PLAYING
            rig.queues._queue_data[PAUSED_QUEUE].queue.state = PlaybackState.PLAYING

    resume = asyncio.ensure_future(_resume())
    await lock_taken.wait()
    release = asyncio.ensure_future(rig.queues.release_paused_stream_slot(INSTANCE, STARTING_QUEUE))
    await asyncio.sleep(0)
    resumed.set()

    assert not await release
    await resume
    rig.stop_device.assert_not_awaited()
    assert paused_buffer.is_buffering
    await paused_buffer.clear()


async def test_a_holder_whose_source_finished_meanwhile_is_left_alone(rig: _Rig) -> None:
    """A paused queue that gave up its slot while its lock was held elsewhere keeps its pause."""
    paused_item = rig.add_queue(PAUSED_QUEUE, PlaybackState.PAUSED)
    paused_buffer = await rig.fill(paused_item)
    lock_taken = asyncio.Event()
    source_done = asyncio.Event()

    async def _hold_the_lock() -> None:
        async with rig.playback_lock(PAUSED_QUEUE):
            lock_taken.set()
            await source_done.wait()

    holder = asyncio.ensure_future(_hold_the_lock())
    await lock_taken.wait()
    release = asyncio.ensure_future(rig.queues.release_paused_stream_slot(INSTANCE, STARTING_QUEUE))
    await asyncio.sleep(0)
    await paused_buffer.clear()
    source_done.set()

    assert not await release
    await holder
    rig.stop_device.assert_not_awaited()
    assert rig.queues._queue_data[PAUSED_QUEUE].session_id is not None


async def test_a_member_starting_from_its_paused_group_takes_the_groups_slot(rig: _Rig) -> None:
    """
    A player that starts its own queue while its group is paused holds the group's lock.

    The group's stop runs within that start, so it gets the lock right away.
    """
    paused_item = rig.add_queue(PAUSED_QUEUE, PlaybackState.PAUSED)
    paused_buffer = await rig.fill(paused_item)
    starting_item = rig.add_queue(STARTING_QUEUE, PlaybackState.IDLE)

    async with rig.playback_lock(PAUSED_QUEUE):
        buffer = await rig.audio.get_audio_buffer(
            starting_item, reason="prepare", capacity_wait_timeout=2
        )

    assert buffer.is_buffering
    rig.stop_device.assert_awaited_once_with(PAUSED_QUEUE)
    assert paused_buffer.cancelled
    await buffer.clear()
