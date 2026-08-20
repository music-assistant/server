"""Tests for the flow stream's transition: incoming prefetch and crossfade reporting."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock, MagicMock

from music_assistant_models.enums import ContentType, CrossfadeMode, MediaType
from music_assistant_models.errors import QueueEmpty
from music_assistant_models.media_items import AudioFormat

from music_assistant.controllers.streams.audio import StreamsAudio
from music_assistant.controllers.streams.audio_buffer import AudioBuffer
from music_assistant.controllers.streams.smart_fades.fades import StandardCrossFade

if TYPE_CHECKING:
    import pytest

TEST_PCM_FORMAT = AudioFormat(
    content_type=ContentType.PCM_S16LE,
    sample_rate=8000,
    bit_depth=16,
    channels=2,
)
# deliberately not a whole second and not frame-aligned, so any assumption about
# chunk boundaries in the transition path shows up as wrong audio
CHUNK_SIZE = TEST_PCM_FORMAT.pcm_sample_size // 3 + 2
STANDARD_CROSSFADE_DURATION = 8


def _buffer(*, duration_available: float = 45.0, eof: bool = True) -> AudioBuffer:
    """Build a valid, fully resident buffer."""
    audio_buffer = MagicMock(spec=AudioBuffer)
    audio_buffer.has_error = False
    audio_buffer.cancelled = False
    audio_buffer.eof = eof
    audio_buffer.max_size_seconds = 300
    audio_buffer.is_valid.return_value = True
    audio_buffer.duration_available = duration_available
    audio_buffer.ready = MagicMock()
    audio_buffer.ready.is_set.return_value = True
    return audio_buffer


def _queue_item(item_id: str, name: str, duration: int = 300) -> SimpleNamespace:
    """Build a flow-streamable track with a prepared buffer."""
    streamdetails = SimpleNamespace(
        audio_format=TEST_PCM_FORMAT,
        buffer=_buffer(),
        fade_in=False,
        stream_error=False,
        uri=f"test://{item_id}",
        seek_position=0,
        seconds_streamed=0,
        duration=300,
        is_realtime=False,
        volume_normalization_mode=None,
    )
    streamdetails.duration = duration
    return SimpleNamespace(
        queue_id="queue-1",
        queue_item_id=item_id,
        name=name,
        media_type=MediaType.TRACK,
        media_item=None,
        streamdetails=streamdetails,
        duration=duration,
        extra_attributes={},
    )


def _flow_audio(
    monkeypatch: pytest.MonkeyPatch,
    *,
    next_item: SimpleNamespace | None,
    load_next: Any,
    crossfade_mode: CrossfadeMode = CrossfadeMode.STANDARD_CROSSFADE,
    crossfade_allowed: bool = True,
    build_result: object | None = None,
) -> tuple[StreamsAudio, SimpleNamespace, MagicMock]:
    """Build a StreamsAudio wired for a two-track flow stream."""
    queue = SimpleNamespace(
        queue_id="queue-1",
        display_name="Queue",
        flow_mode=False,
        overlay_enabled=False,
        overlay_source=None,
    )
    mass = MagicMock()
    mass.player_queues.queue_data.return_value = SimpleNamespace(
        session_id="session-1", flow_mode_stream_log=[]
    )
    mass.player_queues.load_next_queue_item = AsyncMock(side_effect=load_next)
    mass.player_queues.get.return_value = queue
    mass.player_queues.get_next_item.return_value = next_item
    mass.streams.get_crossfade_mode.return_value = crossfade_mode
    mass.config.get_raw_core_config_value.return_value = STANDARD_CROSSFADE_DURATION
    mass.streams.audio_processing.update_item_context = MagicMock()
    player = MagicMock()
    player.config.get_value.return_value = "fixed_48000"
    player.get_supported_sample_rates.return_value = []
    mass.players.get_player.return_value = player

    audio = StreamsAudio(cast("Any", mass))
    audio.setup()
    audio.crossfade_allowed = MagicMock(return_value=crossfade_allowed)  # type: ignore[method-assign]
    monkeypatch.setattr(
        audio.smart_fades_mixer,
        "build",
        AsyncMock(
            return_value=build_result
            or SimpleNamespace(
                timing_info=SimpleNamespace(
                    fadein_trimmed_duration=0.0,
                    crossfade_duration=float(STANDARD_CROSSFADE_DURATION),
                    pre_crossfade_duration=0.0,
                )
            )
        ),
    )

    async def _concat_mix(
        _smart_fade: object,
        *,
        fade_in_part: bytes,
        fade_out_part: bytes,
        **_kwargs: object,
    ) -> AsyncGenerator[bytes]:
        # a lossless stand-in for the mixer, so the emitted total stays checkable
        yield fade_out_part
        yield fade_in_part

    monkeypatch.setattr(audio.smart_fades_mixer, "mix", _concat_mix)
    return audio, queue, mass


def _install_item_streams(
    monkeypatch: pytest.MonkeyPatch,
    audio: StreamsAudio,
    seconds_per_item: dict[str, int],
) -> tuple[list[str], dict[str, int], dict[str, dict[str, int]]]:
    """
    Serve each queue item unaligned chunks.

    Returns the order in which streams were opened, how much of each item was read, and
    a snapshot of that reading taken the moment each item's stream ran out.
    """
    opened: list[str] = []
    consumed: dict[str, int] = dict.fromkeys(seconds_per_item, 0)
    exhausted_at: dict[str, dict[str, int]] = {}

    async def _item_stream(
        queue_item: SimpleNamespace, *_args: object, **_kwargs: object
    ) -> AsyncGenerator[bytes]:
        item_id = queue_item.queue_item_id
        opened.append(item_id)
        total = TEST_PCM_FORMAT.pcm_sample_size * seconds_per_item[item_id]
        sent = 0
        while sent < total:
            size = min(CHUNK_SIZE, total - sent)
            sent += size
            consumed[item_id] += size
            yield bytes(size)
            await asyncio.sleep(0)
        exhausted_at[item_id] = dict(consumed)

    monkeypatch.setattr(audio, "get_queue_item_stream", _item_stream)
    return opened, consumed, exhausted_at


def _reported(mass: MagicMock) -> list[tuple[str, CrossfadeMode]]:
    """Return the crossfade modes published for each queue item, in order."""
    return [
        (call.kwargs["queue_item_id"], call.kwargs["queue_processing"].crossfade_mode)
        for call in mass.streams.audio_processing.update_item_context.call_args_list
    ]


async def _drain(stream: AsyncGenerator[bytes]) -> int:
    """Consume a flow stream, yielding to the loop like a real consumer does."""
    total = 0
    async for chunk in stream:
        total += len(chunk)
        await asyncio.sleep(0)
    return total


async def test_flow_prefetches_the_incoming_fade_in_during_the_holdback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The incoming overlap is gathered while the outgoing tail is still being held back."""
    first_item = _queue_item("item-1", "First")
    second_item = _queue_item("item-2", "Second")
    audio, queue, _mass = _flow_audio(
        monkeypatch, next_item=second_item, load_next=[second_item, QueueEmpty]
    )
    opened, _consumed, exhausted_at = _install_item_streams(
        monkeypatch, audio, {"item-1": 40, "item-2": 20}
    )

    stream = audio.get_queue_flow_stream(
        cast("Any", queue), cast("Any", first_item), TEST_PCM_FORMAT, session_id="session-1"
    )
    emitted = await _drain(stream)

    # the whole overlap was already in hand when the outgoing track ran out
    overlap_size = TEST_PCM_FORMAT.pcm_sample_size * STANDARD_CROSSFADE_DURATION
    assert exhausted_at["item-1"]["item-2"] >= overlap_size
    # the prefetched stream is adopted, so the incoming track is only ever opened once
    assert opened == ["item-1", "item-2"]
    assert emitted == TEST_PCM_FORMAT.pcm_sample_size * 60


async def test_flow_falls_back_when_the_next_item_changed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A prefetch for another item is dropped and the real next item is streamed."""
    first_item = _queue_item("item-1", "First")
    second_item = _queue_item("item-2", "Second")
    other_item = _queue_item("item-3", "Other")
    audio, queue, _mass = _flow_audio(
        monkeypatch, next_item=other_item, load_next=[second_item, QueueEmpty]
    )
    opened, consumed, _exhausted_at = _install_item_streams(
        monkeypatch, audio, {"item-1": 40, "item-2": 20, "item-3": 20}
    )

    stream = audio.get_queue_flow_stream(
        cast("Any", queue), cast("Any", first_item), TEST_PCM_FORMAT, session_id="session-1"
    )
    emitted = await _drain(stream)

    # the stale prefetch is dropped and the real next item is opened once
    assert opened[:3] == ["item-1", "item-3", "item-2"]
    assert opened.count("item-2") == 1
    # the discarded prefetch never reaches the listener
    assert emitted == TEST_PCM_FORMAT.pcm_sample_size * 60
    assert consumed["item-2"] == TEST_PCM_FORMAT.pcm_sample_size * 20


async def test_flow_reports_the_crossfade_that_actually_happens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fade is reported on both of its sides, once the boundary has decided."""
    first_item = _queue_item("item-1", "First")
    second_item = _queue_item("item-2", "Second")
    audio, queue, mass = _flow_audio(
        monkeypatch, next_item=second_item, load_next=[second_item, QueueEmpty]
    )
    _install_item_streams(monkeypatch, audio, {"item-1": 40, "item-2": 20})

    stream = audio.get_queue_flow_stream(
        cast("Any", queue), cast("Any", first_item), TEST_PCM_FORMAT, session_id="session-1"
    )
    await _drain(stream)

    assert _reported(mass) == [
        # each track starts out crediting no fade
        ("item-1", CrossfadeMode.DISABLED),
        ("item-2", CrossfadeMode.DISABLED),
        # both sides are only credited once the blend has really been rendered
        ("item-2", CrossfadeMode.STANDARD_CROSSFADE),
        ("item-1", CrossfadeMode.STANDARD_CROSSFADE),
    ]


async def test_flow_reports_no_crossfade_when_the_transition_is_denied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A transition that never happens is not reported as a crossfade."""
    first_item = _queue_item("item-1", "First")
    second_item = _queue_item("item-2", "Second")
    audio, queue, mass = _flow_audio(
        monkeypatch,
        next_item=second_item,
        load_next=[second_item, QueueEmpty],
        crossfade_mode=CrossfadeMode.SMART_CROSSFADE,
        crossfade_allowed=False,
    )
    _install_item_streams(monkeypatch, audio, {"item-1": 40, "item-2": 20})

    stream = audio.get_queue_flow_stream(
        cast("Any", queue), cast("Any", first_item), TEST_PCM_FORMAT, session_id="session-1"
    )
    await _drain(stream)

    assert _reported(mass) == [
        ("item-1", CrossfadeMode.DISABLED),
        ("item-2", CrossfadeMode.DISABLED),
    ]


async def test_flow_reports_a_smart_fade_that_degraded_to_standard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A smart fade the mixer could not plan is reported as the standard one it became."""
    first_item = _queue_item("item-1", "First")
    second_item = _queue_item("item-2", "Second")
    degraded = StandardCrossFade(logger=MagicMock(), crossfade_duration=STANDARD_CROSSFADE_DURATION)
    overlap_size = TEST_PCM_FORMAT.pcm_sample_size * STANDARD_CROSSFADE_DURATION
    degraded.build(overlap_size, overlap_size, TEST_PCM_FORMAT)
    audio, queue, mass = _flow_audio(
        monkeypatch,
        next_item=second_item,
        load_next=[second_item, QueueEmpty],
        crossfade_mode=CrossfadeMode.SMART_CROSSFADE,
        build_result=degraded,
    )
    _install_item_streams(monkeypatch, audio, {"item-1": 60, "item-2": 60})

    stream = audio.get_queue_flow_stream(
        cast("Any", queue), cast("Any", first_item), TEST_PCM_FORMAT, session_id="session-1"
    )
    await _drain(stream)

    assert _reported(mass) == [
        ("item-1", CrossfadeMode.DISABLED),
        ("item-2", CrossfadeMode.DISABLED),
        ("item-2", CrossfadeMode.STANDARD_CROSSFADE),
        ("item-1", CrossfadeMode.STANDARD_CROSSFADE),
    ]


async def test_flow_reopens_the_incoming_track_when_the_prefetch_broke(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A prefetch whose source failed is dropped so the track gets a fresh attempt."""
    first_item = _queue_item("item-1", "First")
    second_item = _queue_item("item-2", "Second")
    audio, queue, _mass = _flow_audio(
        monkeypatch, next_item=second_item, load_next=[second_item, QueueEmpty]
    )
    opened: list[str] = []

    async def _item_stream(
        queue_item: SimpleNamespace, *_args: object, **_kwargs: object
    ) -> AsyncGenerator[bytes]:
        queue_item.streamdetails.stream_error = False
        opened.append(queue_item.queue_item_id)
        if queue_item is second_item and opened.count("item-2") == 1:
            # the source dies before handing over any audio
            queue_item.streamdetails.stream_error = True
            return
        total = TEST_PCM_FORMAT.pcm_sample_size * 40
        sent = 0
        while sent < total:
            size = min(CHUNK_SIZE, total - sent)
            sent += size
            yield bytes(size)
            await asyncio.sleep(0)

    monkeypatch.setattr(audio, "get_queue_item_stream", _item_stream)
    stream = audio.get_queue_flow_stream(
        cast("Any", queue), cast("Any", first_item), TEST_PCM_FORMAT, session_id="session-1"
    )
    emitted = await _drain(stream)

    assert opened == ["item-1", "item-2", "item-2"]
    # the retry serves the whole track, so nothing is lost to the failed prefetch
    assert emitted == TEST_PCM_FORMAT.pcm_sample_size * 80


async def test_flow_drops_a_prefetch_opened_at_another_position(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A prefetch started at a stale seek position is not adopted."""
    first_item = _queue_item("item-1", "First")
    second_item = _queue_item("item-2", "Second")
    # a leftover from an earlier crossfade into this track
    second_item.streamdetails.seek_position = 8

    loads = {"count": 0}

    async def _load_next(*_args: object, **_kwargs: object) -> SimpleNamespace:
        loads["count"] += 1
        if loads["count"] > 1:
            raise QueueEmpty
        # loading the item resolves its stream details again, back to the track start
        second_item.streamdetails.seek_position = 0
        return second_item

    audio, queue, _mass = _flow_audio(monkeypatch, next_item=second_item, load_next=_load_next)
    opened, _consumed, _exhausted_at = _install_item_streams(
        monkeypatch, audio, {"item-1": 40, "item-2": 20}
    )

    stream = audio.get_queue_flow_stream(
        cast("Any", queue), cast("Any", first_item), TEST_PCM_FORMAT, session_id="session-1"
    )
    emitted = await _drain(stream)

    assert opened == ["item-1", "item-2", "item-2"]
    assert emitted == TEST_PCM_FORMAT.pcm_sample_size * 60


async def test_flow_never_prefetches_a_short_track_to_its_end(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The prefetch stops short of the end, so the track is not reported as streamed."""
    first_item = _queue_item("item-1", "First")
    second_item = _queue_item("item-2", "Second", duration=20)
    audio, queue, _mass = _flow_audio(
        monkeypatch,
        next_item=second_item,
        load_next=[second_item, QueueEmpty],
        crossfade_mode=CrossfadeMode.SMART_CROSSFADE,
    )
    _opened, _consumed, exhausted_at = _install_item_streams(
        monkeypatch, audio, {"item-1": 40, "item-2": 20}
    )

    stream = audio.get_queue_flow_stream(
        cast("Any", queue), cast("Any", first_item), TEST_PCM_FORMAT, session_id="session-1"
    )
    await _drain(stream)

    # the requested 45s window is clamped to half the incoming track
    assert exhausted_at["item-1"]["item-2"] <= TEST_PCM_FORMAT.pcm_sample_size * 10 + CHUNK_SIZE


async def test_flow_skips_the_prefetch_without_a_known_duration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A track of unknown length is not prefetched, since its end cannot be avoided."""
    first_item = _queue_item("item-1", "First")
    second_item = _queue_item("item-2", "Second")
    second_item.streamdetails.duration = None
    audio, queue, _mass = _flow_audio(
        monkeypatch, next_item=second_item, load_next=[second_item, QueueEmpty]
    )
    opened, _consumed, exhausted_at = _install_item_streams(
        monkeypatch, audio, {"item-1": 40, "item-2": 20}
    )

    stream = audio.get_queue_flow_stream(
        cast("Any", queue), cast("Any", first_item), TEST_PCM_FORMAT, session_id="session-1"
    )
    await _drain(stream)

    # opened once, at the transition, with nothing read while the tail was held back
    assert opened == ["item-1", "item-2"]
    assert exhausted_at["item-1"]["item-2"] == 0


async def test_flow_reopens_a_track_whose_prefetch_ran_out_early(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A source that stops short of the clamp is not trusted to serve the track."""
    first_item = _queue_item("item-1", "First")
    second_item = _queue_item("item-2", "Second")
    audio, queue, _mass = _flow_audio(
        monkeypatch, next_item=second_item, load_next=[second_item, QueueEmpty]
    )
    opened: list[str] = []

    async def _item_stream(
        queue_item: SimpleNamespace, *_args: object, **_kwargs: object
    ) -> AsyncGenerator[bytes]:
        opened.append(queue_item.queue_item_id)
        # the incoming source ends cleanly long before the prefetch target
        seconds = 2 if queue_item is second_item and opened.count("item-2") == 1 else 40
        total = TEST_PCM_FORMAT.pcm_sample_size * seconds
        sent = 0
        while sent < total:
            size = min(CHUNK_SIZE, total - sent)
            sent += size
            yield bytes(size)
            await asyncio.sleep(0)

    monkeypatch.setattr(audio, "get_queue_item_stream", _item_stream)
    stream = audio.get_queue_flow_stream(
        cast("Any", queue), cast("Any", first_item), TEST_PCM_FORMAT, session_id="session-1"
    )
    emitted = await _drain(stream)

    assert opened == ["item-1", "item-2", "item-2"]
    # the truncated prefetch is discarded rather than played as the whole track
    assert emitted == TEST_PCM_FORMAT.pcm_sample_size * 80


async def test_flow_keeps_the_prefetch_clear_of_the_end_after_a_seek(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The clamp follows the seek position, so a near-the-end start is not read to EOF."""
    first_item = _queue_item("item-1", "First")
    second_item = _queue_item("item-2", "Second")
    # resuming with only 10 seconds of the track left
    second_item.streamdetails.seek_position = 290
    audio, queue, _mass = _flow_audio(
        monkeypatch, next_item=second_item, load_next=[second_item, QueueEmpty]
    )
    opened, _consumed, exhausted_at = _install_item_streams(
        monkeypatch, audio, {"item-1": 40, "item-2": 10}
    )

    stream = audio.get_queue_flow_stream(
        cast("Any", queue), cast("Any", first_item), TEST_PCM_FORMAT, session_id="session-1"
    )
    await _drain(stream)

    # half of the 10s that remain, so the source is never read to its end in the background
    assert exhausted_at["item-1"]["item-2"] <= TEST_PCM_FORMAT.pcm_sample_size * 5 + CHUNK_SIZE
    assert opened.count("item-2") == 1
