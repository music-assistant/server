"""Tests for the per-item voice over: a declared tail overlap mixed between two item streams."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.enums import CrossfadeMode, MediaType
from music_assistant_models.errors import QueueEmpty
from music_assistant_models.streamdetails import TailOverlap

from music_assistant.controllers.streams.audio import CrossfadeHandover, StreamsAudio
from music_assistant.controllers.streams.constants import MIN_VOICE_OVER_DURATION, VOICE_OVER_RAMP
from music_assistant.models.music_provider import ProviderStreamLimitError
from tests.controllers.streams.test_crossfade_transition import (
    STANDARD_CROSSFADE_DURATION,
    TEST_PCM_FORMAT,
    _reported,
)
from tests.controllers.streams.test_realtime_sources import (
    _FfmpegArgsCaptured,
    _single_item_handler,
)
from tests.controllers.streams.test_voice_over_transition import (
    CLIP_SECONDS,
    OVERLAP,
    SAMPLE_SIZE,
    TRACK_SECONDS,
    _clip,
    _drain,
    _track,
    _voice_over_audio,
)

CLIP_MARKER = 0x10
TRACK_MARKER = 0x40
FRAME_SIZE = TEST_PCM_FORMAT.bit_depth // 8 * TEST_PCM_FORMAT.channels
NO_STREAM_SLOT = ProviderStreamLimitError(
    cast("Any", SimpleNamespace(name="Provider", instance_id="p--1", max_concurrent_streams=1)),
    5.0,
)


def _item_audio(
    monkeypatch: pytest.MonkeyPatch, *, next_item: SimpleNamespace, load_next: Any
) -> tuple[StreamsAudio, MagicMock]:
    """Build a StreamsAudio that renders a voice over with a real VoiceOverFade timing."""
    audio, _queue, mass = _voice_over_audio(monkeypatch, next_item=next_item, load_next=load_next)
    # every item streams in the shared test format, whatever the player
    audio.select_pcm_format = AsyncMock(return_value=TEST_PCM_FORMAT)  # type: ignore[method-assign]
    mass.player_queues.index_by_id.return_value = 1
    return audio, mass


def _install_item_streams(
    monkeypatch: pytest.MonkeyPatch, audio: StreamsAudio
) -> list[tuple[str, float]]:
    """
    Serve each item from the requested position to the end of its duration.

    Returns every stream opened, as the item and the position it was opened at.
    """
    opened: list[tuple[str, float]] = []

    async def _item_stream(
        queue_item: SimpleNamespace,
        *_args: object,
        seek_position: float = 0.0,
        **_kwargs: object,
    ) -> AsyncGenerator[bytes]:
        opened.append((queue_item.queue_item_id, seek_position))
        marker = CLIP_MARKER if queue_item.queue_item_id == "item-1" else TRACK_MARKER
        remaining = int((queue_item.streamdetails.duration - seek_position) * SAMPLE_SIZE)
        remaining = remaining // FRAME_SIZE * FRAME_SIZE
        while remaining > 0:
            size = min(SAMPLE_SIZE, remaining)
            remaining -= size
            yield bytes([marker]) * size
            await asyncio.sleep(0)

    monkeypatch.setattr(audio, "get_queue_item_stream", _item_stream)
    return opened


def _request(
    audio: StreamsAudio,
    queue_item: SimpleNamespace,
    crossfade_mode: CrossfadeMode,
    session_id: str = "session-1",
) -> AsyncGenerator[bytes]:
    """Open the item's own stream, as the player's request for its url does."""
    return audio.get_queue_item_stream_with_smartfade(
        cast("Any", SimpleNamespace(player_id="player-1", name="Player")),
        cast("Any", queue_item),
        TEST_PCM_FORMAT,
        crossfade_mode=crossfade_mode,
        standard_crossfade_duration=STANDARD_CROSSFADE_DURATION,
        session_id=session_id,
    )


def _declared_track() -> SimpleNamespace:
    """Build the track the clip's overlap was planned against."""
    track = _track("item-2", "Track")
    track.streamdetails.duration = TRACK_SECONDS
    return track


async def test_clip_plays_over_the_start_of_the_track_through_two_requests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    The clip's stream carries the voice over the track's intro, the track's request the rest.

    The clip streams exactly its own length. The track's request plays the ramp back to
    full level, then its body from where the mix stopped reading it.
    """
    clip = _clip()
    track = _declared_track()
    audio, mass = _item_audio(monkeypatch, next_item=track, load_next=[track, QueueEmpty])
    opened = _install_item_streams(monkeypatch, audio)

    clip_output = await _drain(_request(audio, clip, CrossfadeMode.VOICE_OVER))

    build = cast("Any", audio.smart_fades_mixer.build)
    build.assert_awaited_once()
    assert build.await_args.kwargs["mode"] == CrossfadeMode.VOICE_OVER
    assert len(build.await_args.kwargs["fade_out_data"]) == OVERLAP * SAMPLE_SIZE
    assert len(clip_output) == CLIP_SECONDS * SAMPLE_SIZE
    handover = audio._crossfade_handover["queue-1"]
    assert handover.queue_item_id == "item-2"
    assert handover.crossfade_mode == CrossfadeMode.VOICE_OVER
    assert handover.elapsed_time_offset == OVERLAP

    track_output = await _drain(_request(audio, track, CrossfadeMode.DISABLED))

    # the overlap is heard once, in the clip's stream
    assert len(track_output) == (TRACK_SECONDS - OVERLAP) * SAMPLE_SIZE
    assert opened == [
        ("item-1", 0),
        ("item-2", 0),
        ("item-2", pytest.approx(OVERLAP + VOICE_OVER_RAMP)),
    ]
    assert track.streamdetails.seek_position == OVERLAP
    assert clip.streamdetails.duration == CLIP_SECONDS
    assert track.streamdetails.duration == TRACK_SECONDS
    assert "queue-1" not in audio._crossfade_handover
    assert _reported(mass) == [
        ("item-1", CrossfadeMode.DISABLED),
        ("item-1", CrossfadeMode.VOICE_OVER),
        ("item-2", CrossfadeMode.VOICE_OVER),
    ]


@pytest.mark.parametrize(
    "overlap",
    [2.0, 6.0],
    ids=["shorter than a crossfade", "more than half the clip"],
)
async def test_a_declared_overlap_is_held_back_exactly(
    monkeypatch: pytest.MonkeyPatch, overlap: float
) -> None:
    """The declaration sizes the hold-back, not the crossfade minimum or the clip's length."""
    clip = _clip()
    clip.streamdetails.tail_overlap = TailOverlap(duration=overlap, next_queue_item_id="item-2")
    track = _declared_track()
    audio, _mass = _item_audio(monkeypatch, next_item=track, load_next=[track, QueueEmpty])
    _install_item_streams(monkeypatch, audio)

    output = await _drain(_request(audio, clip, CrossfadeMode.VOICE_OVER))

    build = cast("Any", audio.smart_fades_mixer.build)
    build.assert_awaited_once()
    assert len(build.await_args.kwargs["fade_out_data"]) == overlap * SAMPLE_SIZE
    assert len(output) == CLIP_SECONDS * SAMPLE_SIZE
    await audio._crossfade_handover.pop("queue-1").close()


async def test_clip_tail_plays_plainly_when_another_item_follows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A declared overlap never plays into an item it was not planned against."""
    clip = _clip()
    other = _track("item-3", "Other")
    other.streamdetails.duration = TRACK_SECONDS
    audio, _mass = _item_audio(monkeypatch, next_item=other, load_next=[other, QueueEmpty])
    _install_item_streams(monkeypatch, audio)

    output = await _drain(_request(audio, clip, CrossfadeMode.VOICE_OVER))

    cast("Any", audio.smart_fades_mixer.build).assert_not_awaited()
    assert output == bytes([CLIP_MARKER]) * CLIP_SECONDS * SAMPLE_SIZE
    assert "queue-1" not in audio._crossfade_handover


async def test_an_overlap_below_the_floor_is_a_plain_cut(monkeypatch: pytest.MonkeyPatch) -> None:
    """A declared overlap too short to be heard holds nothing back and mixes nothing."""
    clip = _clip()
    clip.streamdetails.tail_overlap = TailOverlap(
        duration=MIN_VOICE_OVER_DURATION - 0.2, next_queue_item_id="item-2"
    )
    track = _declared_track()
    audio, _mass = _item_audio(monkeypatch, next_item=track, load_next=[track, QueueEmpty])
    _install_item_streams(monkeypatch, audio)

    output = await _drain(_request(audio, clip, CrossfadeMode.VOICE_OVER))

    cast("Any", audio.smart_fades_mixer.build).assert_not_awaited()
    assert output == bytes([CLIP_MARKER]) * CLIP_SECONDS * SAMPLE_SIZE
    assert "queue-1" not in audio._crossfade_handover


@pytest.mark.parametrize(
    "load_next",
    [
        pytest.param([NO_STREAM_SLOT], id="no stream slot"),
        pytest.param(
            [SimpleNamespace(queue_item_id="item-2", streamdetails=None)], id="no details"
        ),
    ],
)
async def test_the_voice_plays_whole_when_the_next_item_fails_to_load(
    monkeypatch: pytest.MonkeyPatch, load_next: list[Any]
) -> None:
    """A next item that cannot be loaded costs the voice over, never the end of the voice."""
    clip = _clip()
    audio, _mass = _item_audio(monkeypatch, next_item=_declared_track(), load_next=load_next)
    _install_item_streams(monkeypatch, audio)

    output = await _drain(_request(audio, clip, CrossfadeMode.VOICE_OVER))

    cast("Any", audio.smart_fades_mixer.build).assert_not_awaited()
    assert output == bytes([CLIP_MARKER]) * CLIP_SECONDS * SAMPLE_SIZE
    assert clip.streamdetails.duration == CLIP_SECONDS


async def test_a_track_without_a_fade_out_never_loads_the_next_item(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A request that only picks up the fade into its item leaves the next item to its own."""
    track = _declared_track()
    following = _track("item-3", "Following")
    audio, mass = _item_audio(monkeypatch, next_item=following, load_next=[following])
    _install_item_streams(monkeypatch, audio)

    output = await _drain(_request(audio, track, CrossfadeMode.DISABLED))

    mass.player_queues.load_next_queue_item.assert_not_awaited()
    assert output == bytes([TRACK_MARKER]) * TRACK_SECONDS * SAMPLE_SIZE
    assert "queue-1" not in audio._crossfade_pending


async def test_a_fade_mixed_in_another_session_is_discarded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A skip during the clip starts a new session, where the track plays from its start."""
    clip = _clip()
    track = _declared_track()
    audio, _mass = _item_audio(monkeypatch, next_item=track, load_next=[track, QueueEmpty])
    _install_item_streams(monkeypatch, audio)
    await _drain(_request(audio, clip, CrossfadeMode.VOICE_OVER, session_id="session-1"))
    stale = audio._crossfade_handover["queue-1"]

    output = await _drain(_request(audio, track, CrossfadeMode.DISABLED, session_id="session-2"))

    assert output == bytes([TRACK_MARKER]) * TRACK_SECONDS * SAMPLE_SIZE
    assert track.streamdetails.seek_position == 0
    assert "queue-1" not in audio._crossfade_handover
    # released, not left to the garbage collector
    assert stale.stream is None
    assert stale.source is None


async def test_a_fade_landing_from_another_session_while_waiting_is_discarded() -> None:
    """A request that waits for its fade does not take one a previous session mixed."""
    audio = StreamsAudio(MagicMock())
    queue = cast("Any", SimpleNamespace(queue_id="queue-1", display_name="Queue"))
    item = cast("Any", SimpleNamespace(queue_item_id="item-2", name="Track"))
    handoff = asyncio.Event()
    audio._crossfade_pending["queue-1"] = ("item-2", handoff)

    async def _mix() -> AsyncGenerator[bytes]:
        yield b"\x00" * FRAME_SIZE

    stale = CrossfadeHandover(
        stream=_mix(),
        fade_in_media_duration=0.0,
        pcm_format=TEST_PCM_FORMAT,
        queue_item_id="item-2",
        session_id="session-1",
    )

    async def _land_it() -> None:
        await asyncio.sleep(0)
        audio._crossfade_handover["queue-1"] = stale
        handoff.set()

    task = asyncio.create_task(_land_it())
    assert await audio._await_pending_crossfade(queue, item, "session-2") is None
    await task

    assert "queue-1" not in audio._crossfade_handover
    assert stale.stream is None


def test_a_fade_into_an_item_is_pending_once_mixed_or_being_mixed() -> None:
    """Only a fade into this very item, published or being mixed, counts as pending."""
    audio = StreamsAudio(MagicMock())
    assert not audio.fade_into_pending("queue-1", "item-2")

    audio._crossfade_pending["queue-1"] = ("item-2", asyncio.Event())
    assert audio.fade_into_pending("queue-1", "item-2")
    assert not audio.fade_into_pending("queue-1", "item-3")
    assert not audio.fade_into_pending("queue-2", "item-2")

    del audio._crossfade_pending["queue-1"]
    audio._crossfade_handover["queue-1"] = CrossfadeHandover(
        stream=None, fade_in_media_duration=0.0, pcm_format=TEST_PCM_FORMAT, queue_item_id="item-2"
    )
    assert audio.fade_into_pending("queue-1", "item-2")
    assert not audio.fade_into_pending("queue-1", "item-3")


# -- StreamsController.serve_queue_item_stream routing --


def _declare_clip(controller: Any) -> None:
    """Turn the handler's queue item into a clip that declares a tail overlap."""
    queue_item = controller.mass.player_queues.get_item.return_value
    queue_item.media_type = MediaType.SOUND_EFFECT
    queue_item.streamdetails.tail_overlap = TailOverlap(
        duration=OVERLAP, next_queue_item_id="item-2"
    )


async def test_a_declared_clip_is_mixed_on_a_gapless_player_with_crossfade_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The overlap is the clip's own transition, not the queue's crossfade."""
    controller, request, seen = _single_item_handler(is_realtime=False, capture_ffmpeg=monkeypatch)
    _declare_clip(controller)
    controller.get_crossfade_mode.return_value = CrossfadeMode.DISABLED

    with pytest.raises(_FfmpegArgsCaptured):
        await controller.serve_queue_item_stream(request)

    assert seen["crossfade_enabled"] is True
    smartfade = controller.audio.get_queue_item_stream_with_smartfade
    assert smartfade.call_args.kwargs["crossfade_mode"] == CrossfadeMode.VOICE_OVER
    controller.audio.get_queue_item_stream.assert_not_called()


async def test_a_declared_clip_plays_plainly_on_a_player_without_gapless(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A player that leaves a gap between two items gets the clip whole."""
    controller, request, seen = _single_item_handler(is_realtime=False, capture_ffmpeg=monkeypatch)
    _declare_clip(controller)
    controller.mass.players.get_player.return_value.state.supported_features = set()

    with pytest.raises(_FfmpegArgsCaptured):
        await controller.serve_queue_item_stream(request)

    assert seen["crossfade_enabled"] is False
    controller.audio.get_queue_item_stream_with_smartfade.assert_not_called()
    controller.audio.get_queue_item_stream.assert_called_once()


@pytest.mark.parametrize("fade_in_pending", [True, False], ids=["fade pending", "nothing pending"])
async def test_a_track_with_crossfade_off_takes_the_fade_into_it(
    monkeypatch: pytest.MonkeyPatch, fade_in_pending: bool
) -> None:
    """A track whose queue does not crossfade still plays the voice over the item before mixed."""
    controller, request, seen = _single_item_handler(is_realtime=False, capture_ffmpeg=monkeypatch)
    controller.get_crossfade_mode.return_value = CrossfadeMode.DISABLED
    controller.audio.fade_into_pending.return_value = fade_in_pending

    with pytest.raises(_FfmpegArgsCaptured):
        await controller.serve_queue_item_stream(request)

    controller.audio.fade_into_pending.assert_called_once_with("queue-1", "item-1")
    assert seen["crossfade_enabled"] is fade_in_pending
    smartfade = controller.audio.get_queue_item_stream_with_smartfade
    if fade_in_pending:
        assert smartfade.call_args.kwargs["crossfade_mode"] == CrossfadeMode.DISABLED
        controller.audio.get_queue_item_stream.assert_not_called()
    else:
        smartfade.assert_not_called()
        controller.audio.get_queue_item_stream.assert_called_once()
