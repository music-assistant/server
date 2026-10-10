"""Tests for the flow stream's voice over: a declared tail overlap into the next track."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import MagicMock

from music_assistant_models.enums import CrossfadeMode, MediaType
from music_assistant_models.errors import QueueEmpty
from music_assistant_models.streamdetails import TailOverlap

from music_assistant.controllers.streams import audio as audio_module
from music_assistant.controllers.streams.audio import StreamsAudio
from music_assistant.controllers.streams.constants import MIN_VOICE_OVER_DURATION, VOICE_OVER_RAMP
from music_assistant.controllers.streams.smart_fades.fades import VoiceOverFade
from tests.controllers.streams.test_crossfade_transition import (
    STANDARD_CROSSFADE_DURATION,
    TEST_PCM_FORMAT,
    _flow_audio,
    _queue_item,
    _reported,
)

if TYPE_CHECKING:
    import pytest

SAMPLE_SIZE = TEST_PCM_FORMAT.pcm_sample_size
OVERLAP = 3.0
CLIP_SECONDS = 10
TRACK_SECONDS = 40


def _clip(next_queue_item_id: str = "item-2") -> SimpleNamespace:
    """Build a DJ clip that declares a voice over into the given queue item."""
    clip = _queue_item("item-1", "Clip", duration=CLIP_SECONDS)
    clip.media_type = MediaType.SOUND_EFFECT
    clip.streamdetails.tail_overlap = TailOverlap(
        duration=OVERLAP, next_queue_item_id=next_queue_item_id
    )
    return clip


def _track(item_id: str, name: str) -> SimpleNamespace:
    """Build a track without a declared overlap."""
    track = _queue_item(item_id, name)
    track.streamdetails.tail_overlap = None
    return track


def _voice_over_audio(
    monkeypatch: pytest.MonkeyPatch,
    *,
    next_item: SimpleNamespace,
    load_next: Any,
    crossfade_mode: CrossfadeMode = CrossfadeMode.STANDARD_CROSSFADE,
) -> tuple[StreamsAudio, SimpleNamespace, MagicMock]:
    """Build a flow StreamsAudio that renders a voice over with a real VoiceOverFade timing."""
    audio, queue, mass = _flow_audio(
        monkeypatch, next_item=next_item, load_next=load_next, crossfade_mode=crossfade_mode
    )
    # the real rule decides whether the declared overlap applies
    del audio.crossfade_allowed

    async def _build(**kwargs: Any) -> object:
        if kwargs["mode"] == CrossfadeMode.VOICE_OVER:
            fade = VoiceOverFade(logger=audio.logger)
            fade.build(len(kwargs["fade_out_data"]), kwargs["fade_in_bytes_len"], TEST_PCM_FORMAT)
            return fade
        return SimpleNamespace(
            timing_info=SimpleNamespace(
                fadein_trimmed_duration=0.0,
                crossfade_duration=float(STANDARD_CROSSFADE_DURATION),
                pre_crossfade_duration=0.0,
            )
        )

    build = cast("Any", audio.smart_fades_mixer.build)
    build.side_effect = _build

    async def _mix(
        smart_fade: Any,
        *,
        fade_in_part: AsyncGenerator[bytes],
        fade_out_part: bytes,
        **_kwargs: object,
    ) -> AsyncGenerator[bytes]:
        # a stand-in that keeps the real output length: the overlapped part of the
        # outgoing tail is summed into the incoming audio, everything else concatenates
        overlap = int(smart_fade.timing_info.crossfade_duration * SAMPLE_SIZE)
        yield fade_out_part[: len(fade_out_part) - overlap]
        async for fade_in_chunk in fade_in_part:
            yield fade_in_chunk

    monkeypatch.setattr(audio.smart_fades_mixer, "mix", _mix)
    return audio, queue, mass


def _install_item_streams(monkeypatch: pytest.MonkeyPatch, audio: StreamsAudio) -> None:
    """Serve each queue item for the length of its declared duration."""

    async def _item_stream(
        queue_item: SimpleNamespace, *_args: object, **_kwargs: object
    ) -> AsyncGenerator[bytes]:
        seconds = queue_item.streamdetails.duration - int(queue_item.streamdetails.seek_position)
        marker = 0x10 if queue_item.queue_item_id == "item-1" else 0x40
        for _ in range(seconds):
            yield bytes([marker]) * SAMPLE_SIZE
            await asyncio.sleep(0)

    monkeypatch.setattr(audio, "get_queue_item_stream", _item_stream)


async def _drain(stream: AsyncGenerator[bytes]) -> bytes:
    """Collect a flow stream, giving the loop a turn after every chunk."""
    output = bytearray()
    async for chunk in stream:
        output.extend(chunk)
        await asyncio.sleep(0)
    return bytes(output)


def _flow(audio: StreamsAudio, queue: SimpleNamespace, start: SimpleNamespace) -> Any:
    """Open the flow stream for the queue, starting at the given item."""
    return audio.get_queue_flow_stream(
        cast("Any", queue), cast("Any", start), TEST_PCM_FORMAT, session_id="session-1"
    )


async def test_clip_plays_over_the_start_of_the_declared_track(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The clip's declared tail is held back and mixed over the track, credited to the clip."""
    clip = _clip()
    track = _track("item-2", "Track")
    track.streamdetails.duration = TRACK_SECONDS
    audio, queue, mass = _voice_over_audio(
        monkeypatch, next_item=track, load_next=[track, QueueEmpty]
    )
    _install_item_streams(monkeypatch, audio)

    output = await _drain(_flow(audio, queue, clip))

    build = cast("Any", audio.smart_fades_mixer.build)
    build.assert_awaited_once()
    kwargs = build.await_args.kwargs
    assert kwargs["mode"] == CrossfadeMode.VOICE_OVER
    assert len(kwargs["fade_out_data"]) == OVERLAP * SAMPLE_SIZE
    assert kwargs["fade_in_bytes_len"] == int((OVERLAP + VOICE_OVER_RAMP) * SAMPLE_SIZE)
    # the overlap is heard once: the clip's tail is summed into the track's start
    assert len(output) == (CLIP_SECONDS + TRACK_SECONDS - OVERLAP) * SAMPLE_SIZE
    flow_log = mass.player_queues.queue_data.return_value.flow_mode_stream_log
    assert flow_log[0].seconds_streamed == CLIP_SECONDS
    assert track.streamdetails.seek_position == OVERLAP
    assert _reported(mass)[-2:] == [
        ("item-2", CrossfadeMode.VOICE_OVER),
        ("item-1", CrossfadeMode.VOICE_OVER),
    ]


async def test_clip_tail_plays_plainly_when_another_item_follows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A declared overlap never plays into an item it was not planned against."""
    clip = _clip()
    other = _track("item-3", "Other")
    other.streamdetails.duration = TRACK_SECONDS
    audio, queue, _mass = _voice_over_audio(
        monkeypatch, next_item=other, load_next=[other, QueueEmpty]
    )
    _install_item_streams(monkeypatch, audio)

    output = await _drain(_flow(audio, queue, clip))

    cast("Any", audio.smart_fades_mixer.build).assert_not_awaited()
    assert output == bytes([0x10]) * CLIP_SECONDS * SAMPLE_SIZE + (
        bytes([0x40]) * TRACK_SECONDS * SAMPLE_SIZE
    )


async def test_clip_tail_plays_plainly_when_the_loaded_item_is_not_the_declared_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A queue that changed after the tail was held back gets the held tail plainly."""
    clip = _clip()
    track = _track("item-2", "Track")
    track.streamdetails.duration = TRACK_SECONDS
    other = _track("item-3", "Other")
    other.streamdetails.duration = TRACK_SECONDS
    # the declared track is next while the tail is held back; another one is loaded
    audio, queue, _mass = _voice_over_audio(
        monkeypatch, next_item=track, load_next=[other, QueueEmpty]
    )
    _install_item_streams(monkeypatch, audio)

    output = await _drain(_flow(audio, queue, clip))

    cast("Any", audio.smart_fades_mixer.build).assert_not_awaited()
    assert output == bytes([0x10]) * CLIP_SECONDS * SAMPLE_SIZE + (
        bytes([0x40]) * TRACK_SECONDS * SAMPLE_SIZE
    )
    assert other.streamdetails.seek_position == 0


async def test_an_overlap_below_the_floor_is_a_plain_cut(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A declared overlap too short to be heard holds nothing back and mixes nothing."""
    clip = _clip()
    clip.streamdetails.tail_overlap = TailOverlap(
        duration=MIN_VOICE_OVER_DURATION - 0.2, next_queue_item_id="item-2"
    )
    track = _track("item-2", "Track")
    track.streamdetails.duration = TRACK_SECONDS
    audio, queue, _mass = _voice_over_audio(
        monkeypatch, next_item=track, load_next=[track, QueueEmpty]
    )
    _install_item_streams(monkeypatch, audio)

    output = await _drain(_flow(audio, queue, clip))

    cast("Any", audio.smart_fades_mixer.build).assert_not_awaited()
    assert output == bytes([0x10]) * CLIP_SECONDS * SAMPLE_SIZE + (
        bytes([0x40]) * TRACK_SECONDS * SAMPLE_SIZE
    )
    assert track.streamdetails.seek_position == 0


def _prefetcher(next_item: SimpleNamespace) -> tuple[Any, list[str]]:
    """Build a prefetcher whose queue has the given next item, logging the streams it opens."""
    audio = MagicMock()
    audio.mass.player_queues.get_next_item.return_value = next_item
    opened: list[str] = []

    def _item_stream(queue_item: SimpleNamespace, **_kwargs: object) -> AsyncGenerator[bytes]:
        opened.append(queue_item.queue_item_id)

        async def _stream() -> AsyncGenerator[bytes]:
            for _ in range(TRACK_SECONDS):
                yield bytes([0x40]) * SAMPLE_SIZE
                await asyncio.sleep(0)

        return _stream()

    audio.get_queue_item_stream.side_effect = _item_stream
    prefetcher = audio_module._IncomingFadePrefetcher(audio, TEST_PCM_FORMAT, "session-1")
    return prefetcher, opened


async def test_prefetch_gathers_the_declared_overlap_and_ramp() -> None:
    """The incoming prefetch covers the declared overlap plus the ramp back to full level."""
    track = _track("item-2", "Track")
    track.streamdetails.duration = TRACK_SECONDS
    prefetcher, opened = _prefetcher(track)
    queue = SimpleNamespace(queue_id="queue-1")

    prefetcher.ensure_started(cast("Any", queue), cast("Any", _clip()), CrossfadeMode.VOICE_OVER, 8)

    assert opened == ["item-2"]
    assert prefetcher._target == int(SAMPLE_SIZE * (OVERLAP + VOICE_OVER_RAMP))
    await prefetcher.close()


async def test_no_prefetch_when_the_next_item_is_not_the_declared_one() -> None:
    """A declared overlap never prefetches an item it was not planned against."""
    other = _track("item-3", "Other")
    other.streamdetails.duration = TRACK_SECONDS
    prefetcher, opened = _prefetcher(other)
    queue = SimpleNamespace(queue_id="queue-1")

    prefetcher.ensure_started(cast("Any", queue), cast("Any", _clip()), CrossfadeMode.VOICE_OVER, 8)

    assert opened == []
    assert prefetcher._task is None


async def test_clip_plays_over_the_track_with_crossfade_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The voice over is the clip's own transition, not the queue's crossfade."""
    clip = _clip()
    track = _track("item-2", "Track")
    track.streamdetails.duration = TRACK_SECONDS
    audio, queue, _mass = _voice_over_audio(
        monkeypatch,
        next_item=track,
        load_next=[track, QueueEmpty],
        crossfade_mode=CrossfadeMode.DISABLED,
    )
    _install_item_streams(monkeypatch, audio)

    output = await _drain(_flow(audio, queue, clip))

    build = cast("Any", audio.smart_fades_mixer.build)
    build.assert_awaited_once()
    assert build.await_args.kwargs["mode"] == CrossfadeMode.VOICE_OVER
    assert len(output) == (CLIP_SECONDS + TRACK_SECONDS - OVERLAP) * SAMPLE_SIZE
    assert track.streamdetails.seek_position == OVERLAP


async def test_track_after_the_voice_over_crossfades_as_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The boundary after the voice over is the queue's own crossfade."""
    clip = _clip()
    track = _track("item-2", "Track")
    track.streamdetails.duration = TRACK_SECONDS
    third = _track("item-3", "Third")
    third.streamdetails.duration = TRACK_SECONDS
    audio, queue, mass = _voice_over_audio(
        monkeypatch, next_item=track, load_next=[track, third, QueueEmpty]
    )
    mass.player_queues.get_next_item.side_effect = lambda _queue_id, item_id: (
        track if item_id == "item-1" else third
    )
    _install_item_streams(monkeypatch, audio)

    await _drain(_flow(audio, queue, clip))

    build = cast("Any", audio.smart_fades_mixer.build)
    assert [call.kwargs["mode"] for call in build.await_args_list] == [
        CrossfadeMode.VOICE_OVER,
        CrossfadeMode.STANDARD_CROSSFADE,
    ]


async def test_clip_cuts_to_the_track_when_its_audio_is_late(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A track that shows up with less than the floor of the tail left gets a plain cut."""
    clip = _clip()
    track = _track("item-2", "Track")
    track.streamdetails.duration = TRACK_SECONDS
    track.streamdetails.buffer.ready.clear()
    audio, queue, _mass = _voice_over_audio(
        monkeypatch, next_item=track, load_next=[track, QueueEmpty]
    )
    _install_item_streams(monkeypatch, audio)
    # the play-out paces itself at playback speed; this test is about what is left of it
    real_sleep = asyncio.sleep

    async def _no_wait(_delay: float) -> None:
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", _no_wait)

    output = bytearray()
    async for chunk in _flow(audio, queue, clip):
        output.extend(chunk)
        # the track's audio turns up with only half a second of the tail left
        if len(output) >= (CLIP_SECONDS - 0.5) * SAMPLE_SIZE:
            track.streamdetails.buffer.ready.set()

    cast("Any", audio.smart_fades_mixer.build).assert_not_awaited()
    assert bytes(output) == bytes([0x10]) * CLIP_SECONDS * SAMPLE_SIZE + (
        bytes([0x40]) * TRACK_SECONDS * SAMPLE_SIZE
    )
