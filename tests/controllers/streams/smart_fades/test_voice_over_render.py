"""End-to-end render check: a voice over ducks the incoming track in ffmpeg."""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator

import numpy as np
from music_assistant_models.enums import ContentType
from music_assistant_models.media_items import AudioFormat

from music_assistant.controllers.streams.constants import VOICE_OVER_RAMP
from music_assistant.controllers.streams.smart_fades.fades import VoiceOverFade
from music_assistant.controllers.streams.smart_fades.filters import (
    MIX_CEILING_DB,
    VOICE_OVER_DUCK_DEPTH,
)

PCM = AudioFormat(content_type=ContentType.PCM_F32LE, sample_rate=44100, bit_depth=32, channels=2)
SR = 44100
TAIL_SECONDS = 3.0
OVERLAP_SECONDS = 2.0


def _constant(level: float, seconds: float) -> bytes:
    """Return a constant-amplitude stereo signal (a full-scale square wave at 1.0)."""
    frames = int(SR * seconds)
    # a square wave keeps the RMS equal to the level, wherever it is measured
    mono = np.where(np.arange(frames) % 100 < 50, level, -level).astype(np.float32)
    return np.repeat(mono, 2).tobytes()


async def _render(fade_out: bytes, fade_in: bytes) -> tuple[np.ndarray, VoiceOverFade]:
    """Build and apply a VoiceOverFade, returning the rendered mix and the fade."""
    fade = VoiceOverFade(logging.getLogger())
    fade.build(len(fade_out), len(fade_in), PCM)
    chunks = [chunk async for chunk in fade.apply(fade_out, fade_in, PCM)]
    return np.frombuffer(b"".join(chunks), dtype=np.float32), fade


def _rms(mix: np.ndarray, start_s: float, end_s: float) -> float:
    """RMS of the mix between two points in seconds."""
    window = mix[int(start_s * SR) * 2 : int(end_s * SR) * 2]
    return float(np.sqrt(np.mean(window.astype(np.float64) ** 2)))


async def test_voice_over_output_is_tail_plus_ramp() -> None:
    """The voice plays out in full and the track joins for the overlap and the ramp."""
    silence = bytes(len(_constant(0.0, TAIL_SECONDS)))
    fade_in = _constant(0.2, OVERLAP_SECONDS + VOICE_OVER_RAMP)
    mix, fade = await _render(silence, fade_in)

    timing = fade.timing_info
    assert timing.crossfade_duration == OVERLAP_SECONDS
    assert timing.pre_crossfade_duration == TAIL_SECONDS - OVERLAP_SECONDS
    expected_frames = int(SR * TAIL_SECONDS) + int(SR * VOICE_OVER_RAMP)
    assert len(mix) == expected_frames * 2


async def test_voice_over_ducks_the_track_and_ramps_it_back() -> None:
    """The track is at the ducked level under the voice and back at full level after."""
    silence = bytes(len(_constant(0.0, TAIL_SECONDS)))
    level = 0.2
    # a longer incoming part leaves its remainder to play on untouched after the ramp
    fade_in = _constant(level, OVERLAP_SECONDS + VOICE_OVER_RAMP + 1.0)
    fade = VoiceOverFade(logging.getLogger())
    fade.build(len(silence), len(_constant(level, OVERLAP_SECONDS + VOICE_OVER_RAMP)), PCM)
    chunks = [chunk async for chunk in fade.apply(silence, fade_in, PCM)]
    mix = np.frombuffer(b"".join(chunks), dtype=np.float32)

    pre = TAIL_SECONDS - OVERLAP_SECONDS
    assert _rms(mix, 0.0, pre) == 0.0
    ducked = _rms(mix, pre + 0.2, pre + OVERLAP_SECONDS - 0.2)
    assert abs(ducked - level * (1 - VOICE_OVER_DUCK_DEPTH)) < 0.01
    restored = _rms(mix, TAIL_SECONDS + VOICE_OVER_RAMP + 0.1, TAIL_SECONDS + VOICE_OVER_RAMP + 0.9)
    assert abs(restored - level) < 0.01


async def test_voice_over_stays_under_the_ceiling() -> None:
    """A full-scale voice over a full-scale track is limited instead of clipping."""
    mix, fade = await _render(
        _constant(1.0, TAIL_SECONDS), _constant(1.0, OVERLAP_SECONDS + VOICE_OVER_RAMP)
    )

    # the voice alone before the overlap is the clip's own audio, untouched
    blend = mix[int(fade.timing_info.pre_crossfade_duration * SR) * 2 :]
    peak_db = 20 * np.log10(float(np.max(np.abs(blend))))
    assert peak_db <= MIX_CEILING_DB + 0.05


async def test_a_streamed_incoming_part_blends_only_the_overlap_and_ramp() -> None:
    """A streamed incoming part renders like the same bytes: the rest passes through as is."""
    fade_out = _constant(0.3, TAIL_SECONDS)
    blend_part = _constant(0.2, OVERLAP_SECONDS + VOICE_OVER_RAMP)
    remainder = _constant(0.5, 1.5)
    fade_in = blend_part + remainder

    async def _chunks() -> AsyncGenerator[bytes]:
        # chunk edges that never meet the blend's end, so the split falls inside a chunk
        step = 4000 * 8 + 8
        for start in range(0, len(fade_in), step):
            yield fade_in[start : start + step]

    from_bytes = VoiceOverFade(logging.getLogger())
    from_bytes.build(len(fade_out), len(blend_part), PCM)
    expected = b"".join([chunk async for chunk in from_bytes.apply(fade_out, fade_in, PCM)])
    streamed = VoiceOverFade(logging.getLogger())
    streamed.build(len(fade_out), len(blend_part), PCM)
    output = b"".join([chunk async for chunk in streamed.apply(fade_out, _chunks(), PCM)])

    assert output == expected
    assert output.endswith(fade_in[streamed.blend_in_size :])
