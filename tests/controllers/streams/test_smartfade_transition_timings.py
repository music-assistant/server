"""
Tests for crossfade transition timing math.

Covers the ``CrossfadeTimingInfo`` contract that drives lyrics-sync correctness:

  pre_crossfade_duration  + crossfade_duration         = portion attributed to A
  fadein_trimmed_duration + crossfade_duration         = where B's listener actually is
                                                          when CF ends (the value the
                                                          flow loop writes into
                                                          streamdetails.seek_position)

Pure math tests use a fake ``SmartFade`` subclass that just assigns the test-provided
timing in its ``build``. Smart/standard end-to-end behavior is exercised through
``SmartFadesMixer.build``.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator
from unittest.mock import AsyncMock, MagicMock

import numpy as np
import pytest
from music_assistant_models.enums import ContentType, CrossfadeMode, MediaType, StreamType
from music_assistant_models.media_items import AudioFormat
from music_assistant_models.streamdetails import StreamDetails

import music_assistant.controllers.streams.smart_fades.mixer as mixer_module
from music_assistant.constants import VERBOSE_LOG_LEVEL
from music_assistant.controllers.streams.smart_fades.fades import (
    CrossfadeTimingInfo,
    SmartCrossFade,
    SmartFade,
    SmartFadeNotApplicable,
    StandardCrossFade,
)
from music_assistant.controllers.streams.smart_fades.filters import (
    CrossfadeFilter,
    FadeOutTrimFilter,
    GradualTimeStretchFilter,
)
from music_assistant.controllers.streams.smart_fades.helpers import SMART_CROSSFADE_DURATION
from music_assistant.controllers.streams.smart_fades.mixer import SmartFadesMixer
from music_assistant.models.audio_analysis import AudioAnalysisData

PCM = AudioFormat(
    content_type=ContentType.PCM_S16LE,
    sample_rate=44100,
    bit_depth=16,
    channels=2,
)
SAMPLE_SIZE = PCM.pcm_sample_size  # bytes per second


def _seconds(seconds: float) -> int:
    """Return the number of bytes that represent ``seconds`` of PCM audio."""
    return int(seconds * SAMPLE_SIZE)


def _streamdetails(item_id: str = "test", provider: str = "test") -> StreamDetails:
    """Return a minimal StreamDetails object for mixer.build() input."""
    return StreamDetails(
        provider=provider,
        item_id=item_id,
        audio_format=PCM,
        media_type=MediaType.TRACK,
        stream_type=StreamType.HTTP,
    )


def _make_mixer(analysis_for: dict[str, AudioAnalysisData] | None = None) -> SmartFadesMixer:
    """Build a SmartFadesMixer with a stubbed StreamsController."""
    analysis_for = analysis_for or {}
    streams = MagicMock()
    streams.logger = logging.getLogger("test_smartfade_transition_timings")
    streams.audio_analysis = MagicMock()

    async def _get_analysis(item_id: str, _provider: str, **_kwargs: object) -> object:
        return analysis_for.get(item_id)

    streams.audio_analysis.get_audio_analysis = AsyncMock(side_effect=_get_analysis)
    return SmartFadesMixer(streams)


def _beats(start: float, count: int, interval: float) -> np.ndarray:
    """Return ``count`` beat positions starting at ``start`` spaced by ``interval`` seconds."""
    return np.arange(count, dtype=np.float32) * interval + start


def _analysis(
    bpm: float,
    beats_start: float = 0.0,
    beats_count: int = 200,
    duration: float | None = None,
    rms_energy: np.ndarray | None = None,
) -> AudioAnalysisData:
    """Synthetic analysis data with enough beats for SmartCrossFade.build() to succeed."""
    interval = 60.0 / bpm  # seconds per beat
    # When an explicit duration is given, generate beats spanning the full track so the
    # buffer-local shift (duration - 45s) leaves real beats inside the 45s window.
    if duration is not None:
        count = max(beats_count, int(duration / interval) + 1)
        beats = _beats(beats_start, count, interval)
    else:
        beats = _beats(beats_start, beats_count, interval)
        duration = float(beats[-1] + interval)
    downbeats = beats[::4]  # 4/4 time signature
    return AudioAnalysisData(
        duration=duration,
        bpm=bpm,
        beats=beats.tolist(),
        downbeats=downbeats.tolist(),
        rms_energy=rms_energy.tolist() if rms_energy is not None else None,
    )


def _with_vocal_activity(
    analysis: AudioAnalysisData,
    windows: list[tuple[float, float]],
) -> AudioAnalysisData:
    """
    Add a valid 1800-bin vocal probability timeline to an analysis row.

    :param analysis: Analysis row to update.
    :param windows: Vocal windows in full-track media seconds.
    """
    assert analysis.duration is not None
    frame_duration = analysis.duration / 1800
    probabilities = [0.0] * 1800
    for start, end in windows:
        for index in range(int(start / frame_duration), int(end / frame_duration)):
            probabilities[index] = 0.9
    analysis.extra_data = {"vocal_activity": probabilities}
    return analysis


class _FixedTimingFade(SmartFade):
    """Test-only SmartFade whose build just assigns a caller-provided timing."""

    def __init__(self, timing: CrossfadeTimingInfo) -> None:
        super().__init__(logging.getLogger("test_fixed_timing_fade"))
        self._fixed_timing = timing

    def build(
        self,
        fade_out_bytes_len: int,
        fade_in_bytes_len: int,
        pcm_format: AudioFormat,
    ) -> None:
        """Assign the timing supplied at construction time."""
        self.filters = []  # non-empty would normally be required, but unused here
        self.timing_info = self._fixed_timing


# ---------------------------------------------------------------------------
# CrossfadeTimingInfo dataclass
# ---------------------------------------------------------------------------


class TestCrossfadeTimingInfo:
    """Cover the dataclass surface used by callers."""

    def test_fields_are_set(self) -> None:
        """Constructor stores every duration on the dataclass."""
        timing = CrossfadeTimingInfo(
            pre_crossfade_duration=1.0,
            crossfade_duration=2.0,
            fadein_trimmed_duration=3.0,
            post_crossfade_duration=4.0,
        )
        assert timing.pre_crossfade_duration == 1.0
        assert timing.crossfade_duration == 2.0
        assert timing.fadein_trimmed_duration == 3.0
        assert timing.post_crossfade_duration == 4.0

    def test_default_values(self) -> None:
        """All fields default to 0.0 so build can populate them incrementally."""
        timing = CrossfadeTimingInfo()
        assert timing.pre_crossfade_duration == 0.0
        assert timing.crossfade_duration == 0.0
        assert timing.fadein_trimmed_duration == 0.0
        assert timing.post_crossfade_duration == 0.0


# ---------------------------------------------------------------------------
# StandardCrossFade.build — timing math via the real subclass
# ---------------------------------------------------------------------------


class TestStandardCrossFadeBuild:
    """StandardCrossFade.build must produce the expected timing for given inputs."""

    def _build(
        self,
        crossfade_duration: float,
        fade_out_seconds: float,
        fade_in_seconds: float,
    ) -> CrossfadeTimingInfo:
        fade = StandardCrossFade(logger=logging.getLogger(), crossfade_duration=crossfade_duration)
        fade.build(_seconds(fade_out_seconds), _seconds(fade_in_seconds), PCM)
        return fade.timing_info

    def test_symmetric_buffers(self) -> None:
        """Standard with full symmetric buffers — TRIM stays 0."""
        timing = self._build(crossfade_duration=10.0, fade_out_seconds=30, fade_in_seconds=30)
        assert timing.crossfade_duration == pytest.approx(10.0)
        assert timing.fadein_trimmed_duration == 0.0
        assert timing.pre_crossfade_duration == pytest.approx(20.0)
        assert timing.post_crossfade_duration == pytest.approx(20.0)

    def test_buffer_equals_overlap(self) -> None:
        """Standard with X == CF leaves no PRE or POST — mix output is pure overlap."""
        timing = self._build(crossfade_duration=10.0, fade_out_seconds=10, fade_in_seconds=10)
        assert timing.pre_crossfade_duration == pytest.approx(0.0)
        assert timing.crossfade_duration == pytest.approx(10.0)
        assert timing.fadein_trimmed_duration == 0.0
        assert timing.post_crossfade_duration == pytest.approx(0.0)

    def test_short_fadein_clamps_overlap(self) -> None:
        """Fade-in shorter than the configured CF clamps the effective CF down."""
        timing = self._build(crossfade_duration=10.0, fade_out_seconds=20, fade_in_seconds=4)
        assert timing.crossfade_duration == pytest.approx(4.0)
        assert timing.pre_crossfade_duration == pytest.approx(16.0)
        assert timing.post_crossfade_duration == pytest.approx(0.0)
        assert timing.fadein_trimmed_duration == 0.0

    def test_short_fadeout_clamps_overlap(self) -> None:
        """Fade-out shorter than the configured CF clamps the effective CF down."""
        timing = self._build(crossfade_duration=10.0, fade_out_seconds=3, fade_in_seconds=20)
        assert timing.crossfade_duration == pytest.approx(3.0)
        assert timing.pre_crossfade_duration == pytest.approx(0.0)
        assert timing.post_crossfade_duration == pytest.approx(17.0)

    def test_filter_duration_matches_clamped_timing(self) -> None:
        """The acrossfade filter must use the clamped duration, not the configured one."""
        fade = StandardCrossFade(logger=logging.getLogger(), crossfade_duration=10.0)
        # only 6s of (stripped) fade-out audio available
        fade.build(_seconds(6), _seconds(45), PCM)
        assert fade.timing_info.crossfade_duration == pytest.approx(6.0)
        crossfade_filter = fade.filters[0]
        assert isinstance(crossfade_filter, CrossfadeFilter)
        assert crossfade_filter.crossfade_samples == int(6.0 * PCM.sample_rate)

    def test_fractional_overlap_keeps_filter_aligned_to_buffer(self) -> None:
        """
        A non-integer clamped overlap keeps acrossfade ``ns=`` aligned to the buffer.

        Regression for the silent "FFmpeg produced no output" fallback: a fractional
        effective crossfade made the byte slice a fraction of a sample shorter than the
        ``d=`` the filter requested, so ffmpeg's acrossfade emitted nothing.
        """
        frame_size = (PCM.bit_depth // 8) * PCM.channels
        # ~6.3333s of audible fade-out: a real PCM buffer is frame-aligned, yet still not a
        # whole number of seconds, so the effective crossfade stays fractional
        fade_out_len = _seconds(6.3333) // frame_size * frame_size
        fade = StandardCrossFade(logger=logging.getLogger(), crossfade_duration=10.0)
        fade.build(fade_out_len, _seconds(45), PCM)
        crossfade_filter = fade.filters[0]
        assert isinstance(crossfade_filter, CrossfadeFilter)
        # the source-of-truth byte size is frame-aligned ...
        assert fade.crossfade_size % frame_size == 0
        # ... and the acrossfade sample count is exactly that buffer, in samples
        assert crossfade_filter.crossfade_samples == fade.crossfade_size // frame_size
        # the timing duration round-trips from the same integer, never the other way
        assert fade.timing_info.crossfade_duration == pytest.approx(
            fade.crossfade_size / PCM.pcm_sample_size
        )


# ---------------------------------------------------------------------------
# StandardCrossFade.apply — byte slicing must follow the clamped timing
# ---------------------------------------------------------------------------


class TestStandardCrossFadeApplySlicing:
    """apply() must slice the fade-out buffer by the clamped duration, not the configured one."""

    @pytest.mark.asyncio
    async def test_apply_slices_with_clamped_duration(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A 6s fade-out with a 10s configured CF must hand the base mixer all 6s, no more."""
        captured: dict[str, bytes] = {}
        crossfade_marker = b"crossfade-output"

        async def fake_base_apply(
            _self: SmartFade,
            fade_out_part: bytes,
            _fade_in_part: bytes | AsyncGenerator[bytes],
            _pcm_format: AudioFormat,
        ) -> AsyncGenerator[bytes]:
            captured["fade_out"] = fade_out_part
            yield crossfade_marker

        monkeypatch.setattr(SmartFade, "apply", fake_base_apply)
        fade = StandardCrossFade(logger=logging.getLogger(), crossfade_duration=10.0)
        fade.build(_seconds(6), _seconds(45), PCM)
        chunks = [
            chunk async for chunk in fade.apply(b"\x00" * _seconds(6), b"\x00" * _seconds(45), PCM)
        ]
        assert len(captured["fade_out"]) == _seconds(6)
        # nothing precedes the crossfade — the 6s buffer is consumed entirely by the overlap
        assert chunks[0] == crossfade_marker

    @pytest.mark.asyncio
    async def test_apply_feeds_exactly_the_filter_sample_count(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        apply() must feed the base mixer exactly the acrossfade ``ns=`` sample count.

        Otherwise ffmpeg's acrossfade receives fewer samples than requested and emits
        nothing — the silent crossfade failure this regression guards against.
        """
        captured: dict[str, bytes] = {}

        async def fake_base_apply(
            _self: SmartFade,
            fade_out_part: bytes,
            fade_in_part: bytes | AsyncGenerator[bytes],
            _pcm_format: AudioFormat,
        ) -> AsyncGenerator[bytes]:
            captured["fade_out"] = fade_out_part
            assert isinstance(fade_in_part, bytes)
            captured["fade_in"] = fade_in_part
            yield b"crossfade-output"

        monkeypatch.setattr(SmartFade, "apply", fake_base_apply)
        frame_size = (PCM.bit_depth // 8) * PCM.channels
        # frame-aligned like a real PCM buffer, but a fractional number of seconds
        fade_out_len = _seconds(6.3333) // frame_size * frame_size
        fade = StandardCrossFade(logger=logging.getLogger(), crossfade_duration=10.0)
        fade.build(fade_out_len, _seconds(45), PCM)
        crossfade_filter = fade.filters[0]
        assert isinstance(crossfade_filter, CrossfadeFilter)
        assert crossfade_filter.crossfade_samples is not None
        async for _ in fade.apply(b"\x00" * fade_out_len, b"\x11" * _seconds(45), PCM):
            pass
        expected_bytes = crossfade_filter.crossfade_samples * frame_size
        assert len(captured["fade_out"]) == expected_bytes
        assert len(captured["fade_in"]) == expected_bytes

    @pytest.mark.asyncio
    async def test_apply_before_build_fails_fast(self) -> None:
        """apply() without a prior build() must error, not silently hard-cut."""
        fade = StandardCrossFade(logger=logging.getLogger(), crossfade_duration=10.0)
        with pytest.raises(RuntimeError, match="not built"):
            async for _ in fade.apply(b"\x00" * _seconds(5), b"\x11" * _seconds(5), PCM):
                pass

    @pytest.mark.asyncio
    async def test_zero_crossfade_skips_ffmpeg(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When crossfade_duration == 0, apply() must concatenate without calling ffmpeg."""
        base_apply_invoked: list[bool] = []

        async def _base_apply_sentinel(
            _self: SmartFade,
            _fade_out_part: bytes,
            _fade_in_part: bytes | AsyncGenerator[bytes],
            _pcm_format: AudioFormat,
        ) -> AsyncGenerator[bytes]:
            base_apply_invoked.append(True)
            yield b""

        monkeypatch.setattr(SmartFade, "apply", _base_apply_sentinel)

        fade = StandardCrossFade(logger=logging.getLogger(), crossfade_duration=10.0)
        # fade_out_bytes_len=0 → effective_cf = min(10, 0, 45) = 0 → crossfade_duration == 0
        fade.build(0, _seconds(45), PCM)
        assert fade.timing_info.crossfade_duration == 0.0

        fade_out_data = b"\x00" * _seconds(5)
        fade_in_data = b"\x11" * _seconds(5)
        chunks = [chunk async for chunk in fade.apply(fade_out_data, fade_in_data, PCM)]
        combined = b"".join(chunks)
        assert len(combined) == _seconds(10), f"Expected {_seconds(10)} bytes, got {len(combined)}"
        assert combined[0:1] == b"\x00", "fade_out bytes should come first"
        assert combined[-1:] == b"\x11", "fade_in bytes should come last"
        assert not base_apply_invoked, (
            "SmartFade.apply (ffmpeg path) must not be called for zero-length crossfade"
        )


# ---------------------------------------------------------------------------
# Pure math/invariant tests via _FixedTimingFade (no beat-alignment dependency)
# ---------------------------------------------------------------------------


class TestLyricsSyncInvariants:
    """Invariants the flow loop and per-track loop rely on."""

    def _continuation_offset(self, t: CrossfadeTimingInfo) -> float:
        """Return the value the flow loop writes into streamdetails.seek_position."""
        return t.fadein_trimmed_duration + t.crossfade_duration

    def _fadeout_share(self, t: CrossfadeTimingInfo) -> float:
        """Return the seconds of mix output attributed to the outgoing track."""
        return t.pre_crossfade_duration + t.crossfade_duration

    def test_continuation_offset_no_trim(self) -> None:
        """Without trim the listener is exactly CF seconds into the incoming track."""
        timing = CrossfadeTimingInfo(
            pre_crossfade_duration=20.0,
            crossfade_duration=10.0,
            fadein_trimmed_duration=0.0,
            post_crossfade_duration=20.0,
        )
        assert self._continuation_offset(timing) == pytest.approx(10.0)

    def test_continuation_offset_with_trim(self) -> None:
        """With trim the listener is TRIM + CF seconds into the incoming track."""
        timing = CrossfadeTimingInfo(
            pre_crossfade_duration=29.0,
            crossfade_duration=16.0,
            fadein_trimmed_duration=3.0,
            post_crossfade_duration=26.0,
        )
        assert self._continuation_offset(timing) == pytest.approx(19.0)

    def test_fadeout_share_accounts_for_full_outgoing_input(self) -> None:
        """PRE + CF equals the full outgoing input — A is fully accounted for."""
        timing = CrossfadeTimingInfo(
            pre_crossfade_duration=29.0,
            crossfade_duration=16.0,
            fadein_trimmed_duration=3.0,
            post_crossfade_duration=26.0,
        )
        # fade_out_seconds = PRE + CF = 45
        assert self._fadeout_share(timing) == pytest.approx(45.0)

    def test_fixed_timing_fade_round_trip(self) -> None:
        """_FixedTimingFade.timing_info returns whatever was passed in."""
        original = CrossfadeTimingInfo(
            pre_crossfade_duration=1.0,
            crossfade_duration=2.0,
            fadein_trimmed_duration=3.0,
            post_crossfade_duration=4.0,
        )
        fade = _FixedTimingFade(original)
        fade.build(0, 0, PCM)
        assert fade.timing_info == original


# ---------------------------------------------------------------------------
# SmartFadesMixer.build() — the entry point the flow loop calls
# ---------------------------------------------------------------------------


class TestMixerBuild:
    """build() resolves smart vs standard, primes filters, and returns a SmartFade."""

    @pytest.mark.asyncio
    async def test_standard_mode_returns_standard_crossfade(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Standard mode always builds a StandardCrossFade with the configured duration."""

        async def identity_strip(audio_data: bytes, **_kwargs: object) -> bytes:
            return audio_data  # no stripping — test pure timing math

        monkeypatch.setattr(mixer_module, "strip_silence", identity_strip)
        mixer = _make_mixer()
        fade = await mixer.build(
            fade_in_streamdetails=_streamdetails("in"),
            fade_out_streamdetails=_streamdetails("out"),
            pcm_format=PCM,
            standard_crossfade_duration=8,
            mode=CrossfadeMode.STANDARD_CROSSFADE,
            fade_out_data=b"\x00" * _seconds(20),
            fade_in_bytes_len=_seconds(20),
        )
        assert isinstance(fade, StandardCrossFade)
        timing = fade.timing_info
        assert timing.crossfade_duration == pytest.approx(8.0)
        assert timing.fadein_trimmed_duration == 0.0
        assert timing.pre_crossfade_duration == pytest.approx(12.0)
        assert timing.post_crossfade_duration == pytest.approx(12.0)
        # continuation offset for standard fades is just CF
        assert timing.fadein_trimmed_duration + timing.crossfade_duration == pytest.approx(8.0)

    @pytest.mark.asyncio
    async def test_smart_mode_returns_smart_crossfade_when_analysis_available(self) -> None:
        """With audio analysis on both tracks, build() returns a SmartCrossFade."""
        analysis_out = _analysis(120.0)
        analysis_in = _analysis(124.0, beats_start=0.4)
        mixer = _make_mixer({"out": analysis_out, "in": analysis_in})
        fade_out_data = b"\x00" * _seconds(SMART_CROSSFADE_DURATION)
        fade = await mixer.build(
            fade_in_streamdetails=_streamdetails("in"),
            fade_out_streamdetails=_streamdetails("out"),
            pcm_format=PCM,
            standard_crossfade_duration=10,
            mode=CrossfadeMode.SMART_CROSSFADE,
            fade_out_data=fade_out_data,
            fade_in_bytes_len=_seconds(SMART_CROSSFADE_DURATION),
        )
        assert isinstance(fade, SmartCrossFade)
        timing = fade.timing_info
        # SmartCrossFade applies a beat-aligned trim, so TRIM is non-zero.
        assert timing.fadein_trimmed_duration > 0
        assert timing.crossfade_duration > 0
        # Invariants the flow loop depends on:
        #   PRE + CF == rendered_fade_out_seconds  (A's audio fully accounted for)
        #   TRIM + CF + POST == fade_in_seconds    (B's audio fully accounted for)
        # When time-stretch is active, rendered_fade_out_seconds < buffer_duration because
        # rubberband compresses the tail; savings come from the plan's TempoPlan.
        rendered_fade_out_seconds = fade.effective_end - _savings_until(fade, fade.effective_end)
        fade_in_seconds = float(SMART_CROSSFADE_DURATION)
        assert timing.pre_crossfade_duration + timing.crossfade_duration == pytest.approx(
            rendered_fade_out_seconds, abs=0.05
        )
        assert (
            timing.fadein_trimmed_duration
            + timing.crossfade_duration
            + timing.post_crossfade_duration
            == pytest.approx(fade_in_seconds, abs=0.01)
        )

    @pytest.mark.asyncio
    async def test_smart_mode_falls_back_when_no_analysis(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No audio analysis -> build() falls back to StandardCrossFade."""

        async def identity_strip(audio_data: bytes, **_kwargs: object) -> bytes:
            return audio_data  # no stripping — test pure timing math

        monkeypatch.setattr(mixer_module, "strip_silence", identity_strip)
        mixer = _make_mixer()
        fade = await mixer.build(
            fade_in_streamdetails=_streamdetails("in"),
            fade_out_streamdetails=_streamdetails("out"),
            pcm_format=PCM,
            standard_crossfade_duration=10,
            mode=CrossfadeMode.SMART_CROSSFADE,
            fade_out_data=b"\x00" * _seconds(45),
            fade_in_bytes_len=_seconds(45),
        )
        assert isinstance(fade, StandardCrossFade)
        timing = fade.timing_info
        assert timing.crossfade_duration == pytest.approx(10.0)
        assert timing.fadein_trimmed_duration == 0.0
        assert timing.pre_crossfade_duration == pytest.approx(35.0)
        assert timing.post_crossfade_duration == pytest.approx(35.0)

    @pytest.mark.asyncio
    async def test_smart_mode_falls_back_when_analysis_missing_bpm(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Analysis present but missing bpm/beats -> falls back to standard."""

        async def identity_strip(audio_data: bytes, **_kwargs: object) -> bytes:
            return audio_data

        monkeypatch.setattr(mixer_module, "strip_silence", identity_strip)
        incomplete = AudioAnalysisData(duration=180.0, bpm=None, beats=None)
        mixer = _make_mixer({"out": incomplete, "in": incomplete})
        fade = await mixer.build(
            fade_in_streamdetails=_streamdetails("in"),
            fade_out_streamdetails=_streamdetails("out"),
            pcm_format=PCM,
            standard_crossfade_duration=10,
            mode=CrossfadeMode.SMART_CROSSFADE,
            fade_out_data=b"\x00" * _seconds(30),
            fade_in_bytes_len=_seconds(30),
        )
        assert isinstance(fade, StandardCrossFade)
        assert fade.timing_info.fadein_trimmed_duration == 0.0

    @pytest.mark.asyncio
    async def test_build_returns_fade_with_timing_info_readable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """timing_info is queryable immediately after build() — no apply() needed."""

        async def identity_strip(audio_data: bytes, **_kwargs: object) -> bytes:
            return audio_data

        monkeypatch.setattr(mixer_module, "strip_silence", identity_strip)
        mixer = _make_mixer()
        fade = await mixer.build(
            fade_in_streamdetails=_streamdetails("in"),
            fade_out_streamdetails=_streamdetails("out"),
            pcm_format=PCM,
            standard_crossfade_duration=10,
            mode=CrossfadeMode.STANDARD_CROSSFADE,
            fade_out_data=b"\x00" * _seconds(15),
            fade_in_bytes_len=_seconds(15),
        )
        assert isinstance(fade.timing_info, CrossfadeTimingInfo)

    @pytest.mark.asyncio
    async def test_standard_mode_strips_trailing_silence_before_timing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Timing must be computed from the stripped length; the plan is stored on the fade."""

        async def fake_strip(
            audio_data: bytes, *, reverse: bool = False, **_kwargs: object
        ) -> bytes:
            assert reverse is True
            return audio_data[: -_seconds(3)]  # pretend 3s of trailing silence

        monkeypatch.setattr(mixer_module, "strip_silence", fake_strip)
        mixer = _make_mixer()
        fade_out_data = b"\x00" * _seconds(45)
        smart_fade = await mixer.build(
            fade_in_streamdetails=_streamdetails("b"),
            fade_out_streamdetails=_streamdetails("a"),
            pcm_format=PCM,
            standard_crossfade_duration=10,
            mode=CrossfadeMode.STANDARD_CROSSFADE,
            fade_out_data=fade_out_data,
            fade_in_bytes_len=_seconds(45),
        )
        assert isinstance(smart_fade, StandardCrossFade)
        assert smart_fade.trailing_silence_bytes == _seconds(3)
        timing = smart_fade.timing_info
        assert timing.pre_crossfade_duration + timing.crossfade_duration == pytest.approx(42.0)

    @pytest.mark.asyncio
    async def test_smart_mode_never_measures_silence(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The smart path must never call strip_silence — beat coordinates map onto the full buffer."""

        async def fail_strip(*_args: object, **_kwargs: object) -> bytes:
            raise AssertionError("strip_silence must not be called on the smart path")

        monkeypatch.setattr(mixer_module, "strip_silence", fail_strip)
        mixer = _make_mixer(analysis_for={"a": _analysis(bpm=120.0), "b": _analysis(bpm=120.0)})
        fade_out_data = b"\x00" * _seconds(45)
        smart_fade = await mixer.build(
            fade_in_streamdetails=_streamdetails("b"),
            fade_out_streamdetails=_streamdetails("a"),
            pcm_format=PCM,
            standard_crossfade_duration=10,
            mode=CrossfadeMode.SMART_CROSSFADE,
            fade_out_data=fade_out_data,
            fade_in_bytes_len=_seconds(45),
        )
        assert isinstance(smart_fade, SmartCrossFade)

    @pytest.mark.asyncio
    async def test_smart_fallback_to_standard_strips(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Smart mode without analysis falls back to standard — which must measure silence."""

        async def fake_strip(audio_data: bytes, **_kwargs: object) -> bytes:
            return audio_data[: -_seconds(5)]

        monkeypatch.setattr(mixer_module, "strip_silence", fake_strip)
        mixer = _make_mixer(analysis_for={})  # no analysis available
        smart_fade = await mixer.build(
            fade_in_streamdetails=_streamdetails("b"),
            fade_out_streamdetails=_streamdetails("a"),
            pcm_format=PCM,
            standard_crossfade_duration=10,
            mode=CrossfadeMode.SMART_CROSSFADE,
            fade_out_data=b"\x00" * _seconds(45),
            fade_in_bytes_len=_seconds(45),
        )
        assert isinstance(smart_fade, StandardCrossFade)
        assert smart_fade.trailing_silence_bytes == _seconds(5)

    @pytest.mark.asyncio
    async def test_smart_fallback_retains_an_audible_outgoing_vocal(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Validated FireRed activity extends a fallback trim only within audible RMS energy."""

        async def fake_strip(audio_data: bytes, **_kwargs: object) -> bytes:
            return audio_data[: _seconds(30)]

        def fail_smart_build(*_args: object, **_kwargs: object) -> None:
            raise SmartFadeNotApplicable("forced fallback")

        monkeypatch.setattr(mixer_module, "strip_silence", fake_strip)
        monkeypatch.setattr(SmartCrossFade, "build", fail_smart_build)
        outgoing = _with_vocal_activity(
            _analysis(
                120.0,
                duration=240.0,
                rms_energy=_rms_with_silent_tail(240.0, 5.0),
            ),
            [(228.0, 232.0)],
        )
        mixer = _make_mixer({"a": outgoing, "b": _analysis(120.0, duration=240.0)})

        fade = await mixer.build(
            fade_in_streamdetails=_streamdetails("b"),
            fade_out_streamdetails=_streamdetails("a"),
            pcm_format=PCM,
            standard_crossfade_duration=10,
            mode=CrossfadeMode.SMART_CROSSFADE,
            fade_out_data=b"\x00" * _seconds(45),
            fade_in_bytes_len=_seconds(45),
        )

        assert isinstance(fade, StandardCrossFade)
        retained_seconds = (_seconds(45) - fade.trailing_silence_bytes) / SAMPLE_SIZE
        assert retained_seconds == pytest.approx(37.75, abs=1 / PCM.sample_rate)

    @pytest.mark.asyncio
    async def test_smart_fallback_invalid_vocal_data_matches_missing_data(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Missing and stale vocal metadata keep the exact standard silence trim."""

        async def fake_strip(audio_data: bytes, **_kwargs: object) -> bytes:
            return audio_data[: _seconds(30)]

        def fail_smart_build(*_args: object, **_kwargs: object) -> None:
            raise SmartFadeNotApplicable("forced fallback")

        monkeypatch.setattr(mixer_module, "strip_silence", fake_strip)
        monkeypatch.setattr(SmartCrossFade, "build", fail_smart_build)
        missing = _analysis(120.0, duration=240.0, rms_energy=_rms_with_silent_tail(240.0, 5.0))
        stale = _analysis(120.0, duration=240.0, rms_energy=_rms_with_silent_tail(240.0, 5.0))
        stale.extra_data = {
            "vocal_activity": {
                "model": "firered_aed",
                "frame_duration": 0.1,
                "probabilities": [0.9] * 2400,
            }
        }

        trims: list[int] = []
        for outgoing in (missing, stale):
            mixer = _make_mixer({"a": outgoing, "b": _analysis(120.0, duration=240.0)})
            fade = await mixer.build(
                fade_in_streamdetails=_streamdetails("b"),
                fade_out_streamdetails=_streamdetails("a"),
                pcm_format=PCM,
                standard_crossfade_duration=10,
                mode=CrossfadeMode.SMART_CROSSFADE,
                fade_out_data=b"\x00" * _seconds(45),
                fade_in_bytes_len=_seconds(45),
            )
            assert isinstance(fade, StandardCrossFade)
            trims.append(fade.trailing_silence_bytes)

        assert trims == [_seconds(15), _seconds(15)]

    @pytest.mark.asyncio
    async def test_smart_fallback_caps_vocal_retention_at_the_rms_boundary(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """FireRed cannot restore a long low-energy tail beyond the audible RMS boundary."""

        async def fake_strip(audio_data: bytes, **_kwargs: object) -> bytes:
            return audio_data[: _seconds(20)]

        def fail_smart_build(*_args: object, **_kwargs: object) -> None:
            raise SmartFadeNotApplicable("forced fallback")

        monkeypatch.setattr(mixer_module, "strip_silence", fake_strip)
        monkeypatch.setattr(SmartCrossFade, "build", fail_smart_build)
        outgoing = _with_vocal_activity(
            _analysis(
                120.0,
                duration=240.0,
                rms_energy=_rms_with_silent_tail(240.0, 20.0),
            ),
            [(225.0, 235.0)],
        )
        mixer = _make_mixer({"a": outgoing, "b": _analysis(120.0, duration=240.0)})

        fade = await mixer.build(
            fade_in_streamdetails=_streamdetails("b"),
            fade_out_streamdetails=_streamdetails("a"),
            pcm_format=PCM,
            standard_crossfade_duration=10,
            mode=CrossfadeMode.SMART_CROSSFADE,
            fade_out_data=b"\x00" * _seconds(45),
            fade_in_bytes_len=_seconds(45),
        )

        assert isinstance(fade, StandardCrossFade)
        retained_seconds = (_seconds(45) - fade.trailing_silence_bytes) / SAMPLE_SIZE
        assert retained_seconds == pytest.approx(20.0)

    @pytest.mark.asyncio
    async def test_standard_mode_fully_silent_tail(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A fully silent tail — timing collapses to zero; trailing_silence_bytes is the full input."""

        async def fake_strip(_audio_data: bytes, **_kwargs: object) -> bytes:
            return b""

        monkeypatch.setattr(mixer_module, "strip_silence", fake_strip)
        mixer = _make_mixer()
        smart_fade = await mixer.build(
            fade_in_streamdetails=_streamdetails("b"),
            fade_out_streamdetails=_streamdetails("a"),
            pcm_format=PCM,
            standard_crossfade_duration=10,
            mode=CrossfadeMode.STANDARD_CROSSFADE,
            fade_out_data=b"\x00" * _seconds(45),
            fade_in_bytes_len=_seconds(45),
        )
        assert isinstance(smart_fade, StandardCrossFade)
        assert smart_fade.trailing_silence_bytes == _seconds(45)
        timing = smart_fade.timing_info
        assert timing.pre_crossfade_duration == 0.0
        assert timing.crossfade_duration == 0.0

    @pytest.mark.asyncio
    async def test_standard_mode_measurement_failure_degrades_gracefully(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A strip_silence failure must not propagate — build degrades with trailing_silence_bytes=0."""

        async def broken_strip(*_args: object, **_kwargs: object) -> bytes:
            raise OSError("ffmpeg spawn failed")

        monkeypatch.setattr(mixer_module, "strip_silence", broken_strip)
        mixer = _make_mixer()
        fade_out_data = b"\x00" * _seconds(45)
        smart_fade = await mixer.build(
            fade_in_streamdetails=_streamdetails("b"),
            fade_out_streamdetails=_streamdetails("a"),
            pcm_format=PCM,
            standard_crossfade_duration=10,
            mode=CrossfadeMode.STANDARD_CROSSFADE,
            fade_out_data=fade_out_data,
            fade_in_bytes_len=_seconds(45),
        )
        assert isinstance(smart_fade, StandardCrossFade)
        assert smart_fade.trailing_silence_bytes == 0
        timing = smart_fade.timing_info
        assert timing.pre_crossfade_duration + timing.crossfade_duration == pytest.approx(45.0)

    @pytest.mark.asyncio
    async def test_apply_executes_silence_trim_plan(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """apply() must slice out trailing_silence_bytes before crossfading."""

        async def fake_strip(audio_data: bytes, **_kwargs: object) -> bytes:
            return audio_data[: -_seconds(3)]  # pretend 3s of trailing silence

        monkeypatch.setattr(mixer_module, "strip_silence", fake_strip)

        captured: dict[str, bytes] = {}
        crossfade_marker = b"crossfade-output"

        async def fake_base_apply(
            _self: SmartFade,
            fade_out_part: bytes,
            _fade_in_part: bytes | AsyncGenerator[bytes],
            _pcm_format: AudioFormat,
        ) -> AsyncGenerator[bytes]:
            captured["fade_out"] = fade_out_part
            yield crossfade_marker

        monkeypatch.setattr(SmartFade, "apply", fake_base_apply)

        mixer = _make_mixer()
        fade_out_data = b"\x00" * _seconds(45)
        smart_fade = await mixer.build(
            fade_in_streamdetails=_streamdetails("b"),
            fade_out_streamdetails=_streamdetails("a"),
            pcm_format=PCM,
            standard_crossfade_duration=10,
            mode=CrossfadeMode.STANDARD_CROSSFADE,
            fade_out_data=fade_out_data,
            fade_in_bytes_len=_seconds(45),
        )
        assert isinstance(smart_fade, StandardCrossFade)

        chunks = [
            chunk
            async for chunk in smart_fade.apply(
                fade_out_part=fade_out_data,
                fade_in_part=b"\x00" * _seconds(45),
                pcm_format=PCM,
            )
        ]

        # The yielded pre-crossfade bytes are where the trim actually lands:
        # 45s input - 3s measured silence - 10s clamped CF = 32s. Without the
        # trim apply() would yield 35s here.
        marker_idx = chunks.index(crossfade_marker)
        pre_crossfade_bytes = sum(len(chunk) for chunk in chunks[:marker_idx])
        assert pre_crossfade_bytes == _seconds(32)
        assert len(captured["fade_out"]) == _seconds(10)


# ---------------------------------------------------------------------------
# SmartCrossFade — silence-aware fade-out anchoring
# ---------------------------------------------------------------------------

LOGGER = logging.getLogger(__name__)


def _rms_with_silent_tail(track_duration: float, silent_tail: float) -> np.ndarray:
    bins = np.full(1800, 0.5, dtype=np.float32)
    bins[0] = 1.0
    if silent_tail > 0:
        bins[-int(silent_tail / track_duration * 1800) :] = 0.001
    return bins


class TestSilenceAwareAnchoring:
    """SmartCrossFade must anchor the fade where audible content ends."""

    def _build_fade(self, silent_tail: float) -> SmartCrossFade:
        duration = 240.0
        fade = SmartCrossFade(
            logger=LOGGER,
            fade_out_analysis=_analysis(
                bpm=120.0,
                duration=duration,
                rms_energy=_rms_with_silent_tail(duration, silent_tail),
            ),
            fade_in_analysis=_analysis(bpm=120.0, duration=duration),
        )
        fade.build(_seconds(45), _seconds(45), PCM)
        return fade

    def test_silent_tail_moves_the_anchor(self) -> None:
        """A 10s silent tail shortens the audible anchor to ~35s and inserts FadeOutTrimFilter."""
        fade = self._build_fade(silent_tail=10.0)
        assert fade.effective_end == pytest.approx(35.0, abs=0.3)
        # the rendered fade-out covers only the audible region
        timing = fade.timing_info
        assert timing.pre_crossfade_duration + timing.crossfade_duration == pytest.approx(
            fade.effective_end, abs=0.05
        )
        # tail trim is the FIRST filter so later schedules see the trimmed stream
        assert isinstance(fade.filters[0], FadeOutTrimFilter)
        assert fade.filters[0].fadeout_end_pos == pytest.approx(fade.effective_end)

    def test_no_silence_keeps_buffer_end_anchor(self) -> None:
        """Without silence, effective_end equals the full buffer and no trim filter is added."""
        fade = self._build_fade(silent_tail=0.0)
        assert fade.effective_end == pytest.approx(45.0, abs=0.3)
        assert not any(isinstance(f, FadeOutTrimFilter) for f in fade.filters)

    def test_sub_tolerance_silent_tail_snaps_anchor_to_buffer_end(self) -> None:
        """A silent tail below the trim tolerance keeps the anchor at the rendered buffer end."""
        fade = self._build_fade(silent_tail=0.4)
        assert fade.effective_end == pytest.approx(45.0)
        assert not any(isinstance(f, FadeOutTrimFilter) for f in fade.filters)

    def test_mostly_silent_tail_raises_for_fallback(self) -> None:
        """A tail with only ~5s of audible content raises SmartFadeNotApplicable so the caller falls back."""
        with pytest.raises(SmartFadeNotApplicable, match="silent"):
            self._build_fade(silent_tail=40.0)

    def test_partial_buffer_keeps_beats_aligned(self) -> None:
        """
        The live holdback buffer is rarely exactly 45s.

        Beat coordinates must use the actual buffer length or every downbeat snap
        is off by the difference.
        """
        duration = 240.0
        fade = SmartCrossFade(
            logger=LOGGER,
            fade_out_analysis=_analysis(bpm=120.0, duration=duration),
            fade_in_analysis=_analysis(bpm=120.0, duration=duration),
        )
        # 44.3s buffer: 0.7s short of the constant, like a real partial final chunk
        partial_bytes = int(PCM.pcm_sample_size * 44.3)
        frame_size = (PCM.bit_depth // 8) * PCM.channels
        partial_bytes = (partial_bytes // frame_size) * frame_size
        fade.build(partial_bytes, _seconds(45), PCM)
        buffer_duration = partial_bytes / PCM.pcm_sample_size
        # beats are on a strict 0.5s grid from t=0 in the analysis fixture;
        # in real buffer coordinates each beat must satisfy
        # (beat + duration - buffer_duration) % 0.5 == 0
        offset = duration - buffer_duration
        for beat in fade.fade_out_beats[:8]:
            track_pos = beat + offset
            assert abs(track_pos % 0.5) < 1e-3 or abs(track_pos % 0.5 - 0.5) < 1e-3, (
                f"beat {beat:.4f} maps to track_pos {track_pos:.4f}, "
                f"not on 0.5s grid (offset={offset:.4f})"
            )
        # the snapped crossfade start must land on a real downbeat (2s grid), not 0.7s off
        crossfade_start = fade.effective_end - fade.timing_info.crossfade_duration
        start_track_pos = crossfade_start + offset
        assert abs(start_track_pos % 2.0) < 0.02 or abs(start_track_pos % 2.0 - 2.0) < 0.02, (
            f"crossfade start {crossfade_start:.4f} maps to track_pos {start_track_pos:.4f}, "
            f"not on 2s downbeat grid (offset={offset:.4f})"
        )

    def test_fadeout_beats_are_masked_to_effective_end(self) -> None:
        """Beats in the silent tail are dropped so no downbeat sits beyond effective_end."""
        fade = self._build_fade(silent_tail=10.0)
        assert fade.fade_out_beats.min() >= 0.0
        assert fade.fade_out_beats.max() <= fade.effective_end + 0.01

    def test_short_audible_tail_keeps_crossfade_inside_it(self) -> None:
        """A crossfade longer than the audible tail is capped so no schedule goes negative."""
        duration = 240.0
        fade = SmartCrossFade(
            logger=LOGGER,
            fade_out_analysis=_analysis(
                bpm=120.0,
                duration=duration,
                rms_energy=_rms_with_silent_tail(duration, 33.0),
            ),
            # 115 vs 120 BPM is within the stretch threshold, so the tempo ramp is active
            fade_in_analysis=_analysis(bpm=115.0, duration=duration),
        )
        fade.build(_seconds(45), _seconds(45), PCM)
        assert fade.timing_info.crossfade_duration <= fade.effective_end + 1e-6
        # the capped crossfade consumes the whole audible tail, so the stretch is skipped
        assert not any(isinstance(f, GradualTimeStretchFilter) for f in fade.filters)


def _rms_with_mastered_fade(
    track_duration: float, fade_start: float, fade_end: float
) -> np.ndarray:
    """Steady energy with the record's own gradual fade-out between the given media times."""
    bins = np.full(1800, 0.5, dtype=np.float32)
    bin_seconds = track_duration / 1800
    t = (np.arange(1800) + 0.5) * bin_seconds
    ramp = 0.5 * (1.0 - (t - fade_start) / (fade_end - fade_start)) + 0.0005
    return np.where(t < fade_start, bins, np.where(t >= fade_end, 0.0005, ramp)).astype(np.float32)


class TestQuickFadeMasteredFadeDeadZone:
    """
    A mastered fade-out under a quick-fade tier lands in the audible-trim dead zone.

    The 70% mix-out floor anchors mid-fade while the 5% audible floor sits
    several seconds later; every quick-fade rung is far shorter than that gap,
    so AudibleTrimPolicy rejects the entire main candidate set. The rescue
    pass (ungated trim-closing ladder plus rescue rungs) then ships a
    late-anchored fade.
    """

    def _build_fade(
        self, caplog: pytest.LogCaptureFixture, level: int = logging.DEBUG
    ) -> SmartCrossFade:
        duration = 240.0
        fade = SmartCrossFade(
            logger=LOGGER,
            fade_out_analysis=_analysis(
                bpm=128.0,
                duration=duration,
                # the record fades itself out over 229s..237s: mix-out (70% floor)
                # anchors near 231s while audible content (5% floor) runs to ~237s
                rms_energy=_rms_with_mastered_fade(duration, 229.0, 237.0),
            ),
            # 17.2% BPM gap: QUICK_FADE with the [2, 1] rung ladder (3.75s / 1.88s)
            fade_in_analysis=_analysis(bpm=150.0, duration=duration),
        )
        with caplog.at_level(level):
            fade.build(_seconds(45), _seconds(45), PCM)
        return fade

    def test_main_pass_rejects_every_candidate_on_the_trim_guard_alone(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Every main-pass candidate dies on the one guard; the rescue pass ships the fade."""
        self._build_fade(caplog)
        assert (
            "all 2 candidates rejected (audible trim exceeds a short fade's own duration x2)"
            in caplog.text
        )
        assert (
            "shipping a rescue-pass candidate (source=rescue-anchor) instead of the "
            "emergency handoff" in caplog.text
        )

    def test_rescue_ships_a_late_anchored_chain_within_the_trim_bound(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The shipped chain anchors near the audible end, honoring the short-fade trim bound."""
        fade = self._build_fade(caplog)
        # rescue anchor: last protective downbeat at/after audio_end - 2 bars (128 BPM)
        assert fade.effective_end == pytest.approx(41.25, abs=0.05)
        assert isinstance(fade.filters[0], FadeOutTrimFilter)
        assert fade.filters[0].fadeout_end_pos == pytest.approx(fade.effective_end)
        # the guard's own invariant holds on the shipped plan: audible material
        # dropped past the anchor stays within the overlap length (~41.67s RMS boundary)
        audible_trim = 41.67 - fade.effective_end
        assert audible_trim <= fade.timing_info.crossfade_duration + 1e-6

    def test_rescue_pass_scores_trim_closing_candidates(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The rescue pass runs the audible-end ladder ungated, so the selector scores it."""
        self._build_fade(caplog, level=VERBOSE_LOG_LEVEL)
        assert "source=trim-closing-anchor" in caplog.text

    def test_trim_closing_wins_when_the_ladder_outgrows_the_rescue_rung(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A 4-bar dead zone ships the audible-end ladder rung, not the capped rescue rung."""
        duration = 240.0
        fade = SmartCrossFade(
            logger=LOGGER,
            fade_out_analysis=_analysis(
                bpm=128.0,
                duration=duration,
                # longer mastered fade: the 7.78s trim gap exceeds even the 4-bar
                # rung (7.5s) yet stays under the trim-closing generator's 8s gate
                rms_energy=_rms_with_mastered_fade(duration, 228.4, 238.9),
            ),
            # 9.4% BPM gap: QUICK_FADE with the [4, 2, 1] rung ladder
            fade_in_analysis=_analysis(bpm=140.0, duration=duration),
        )
        with caplog.at_level(logging.DEBUG):
            fade.build(_seconds(45), _seconds(45), PCM)
        assert "shipping a rescue-pass candidate (source=trim-closing-anchor)" in caplog.text
        # the audible-end anchor keeps the full 4-bar overlap and trims nothing audible
        assert fade.effective_end == pytest.approx(43.40, abs=0.05)
        assert fade.timing_info.crossfade_duration == pytest.approx(7.78, abs=0.05)


# ---------------------------------------------------------------------------
# SmartCrossFade — rubberband stretch savings compensation
# ---------------------------------------------------------------------------


def _savings_until(fade: SmartCrossFade, t: float) -> float:
    """Rendered-time savings of the built fade's tempo plan up to input time t."""
    assert fade.plan is not None
    return fade.plan.tempo_plan.savings_until(t)


class TestStretchSavings:
    """
    Rendered-time savings from the stretch must reach the timing bookkeeping.

    The savings integration math itself is unit-tested on TempoPlan in
    tests/controllers/streams/smart_fades/test_models.py.
    """

    def _stretched_fade(self) -> SmartCrossFade:
        duration = 240.0
        # 4% BPM difference with >4 bars available -> stretch is applied
        fade = SmartCrossFade(
            logger=LOGGER,
            fade_out_analysis=_analysis(bpm=120.0, duration=duration),
            fade_in_analysis=_analysis(bpm=124.8, duration=duration),
        )
        fade.build(_seconds(45), _seconds(45), PCM)
        return fade

    def test_pre_plus_cf_equals_rendered_tail(self) -> None:
        """PRE + CF equals the rendered tail duration (buffer minus stretch savings)."""
        fade = self._stretched_fade()
        assert fade.tempo_steps, "test requires the stretch to be active"
        total_savings = _savings_until(fade, fade.effective_end)
        assert total_savings > 0.0
        timing = fade.timing_info
        assert timing.pre_crossfade_duration + timing.crossfade_duration == pytest.approx(
            fade.effective_end - total_savings, abs=0.05
        )

    def test_pre_plus_cf_equals_rendered_tail_when_slowing_down(self) -> None:
        """A slower incoming track lengthens the rendered tail — PRE + CF exceeds effective_end."""
        duration = 240.0
        # ~3.8% BPM difference downwards -> stretch slows the outgoing track
        fade = SmartCrossFade(
            logger=LOGGER,
            fade_out_analysis=_analysis(bpm=120.0, duration=duration),
            fade_in_analysis=_analysis(bpm=115.4, duration=duration),
        )
        fade.build(_seconds(45), _seconds(45), PCM)
        assert fade.tempo_steps, "test requires the stretch to be active"
        total_savings = _savings_until(fade, fade.effective_end)
        assert total_savings < 0.0
        timing = fade.timing_info
        assert timing.pre_crossfade_duration + timing.crossfade_duration == pytest.approx(
            fade.effective_end - total_savings, abs=0.05
        )

    def test_bass_kill_completes_at_the_anchor(self) -> None:
        """
        The outgoing low-shelf kill reaches full depth at or before the audible end.

        A-side shelves render BEFORE the rubberband stretch, so their schedules
        live in musical input time — no rendered-time remap needed (unlike the
        old post-stretch frequency sweeps).
        """
        fade = self._stretched_fade()
        assert fade.plan is not None
        low_out = fade.plan.eq_plan.low_out
        assert low_out is not None
        assert low_out.steps[-1][1] == pytest.approx(-26.0)
        assert low_out.steps[-1][0] <= fade.effective_end + 0.05

    def test_trim_and_stretch_combined(self) -> None:
        """A trimmed silent tail and an active stretch compose: both anchor on the rendered end."""
        duration = 240.0
        fade = SmartCrossFade(
            logger=LOGGER,
            fade_out_analysis=_analysis(
                bpm=120.0,
                duration=duration,
                rms_energy=_rms_with_silent_tail(duration, 10.0),
            ),
            fade_in_analysis=_analysis(bpm=124.8, duration=duration),
        )
        fade.build(_seconds(45), _seconds(45), PCM)
        # tail trim must come first so every later schedule sees the trimmed stream
        assert isinstance(fade.filters[0], FadeOutTrimFilter)
        assert fade.tempo_steps, "test requires the stretch to be active"
        rendered_end = fade.effective_end - _savings_until(fade, fade.effective_end)
        timing = fade.timing_info
        assert timing.pre_crossfade_duration + timing.crossfade_duration == pytest.approx(
            rendered_end, abs=0.05
        )

    def test_unstretched_fade_has_zero_savings(self) -> None:
        """Without a stretch, savings are zero and PRE + CF equals effective_end exactly."""
        fade = SmartCrossFade(
            logger=LOGGER,
            fade_out_analysis=_analysis(bpm=120.0, duration=240.0),
            fade_in_analysis=_analysis(bpm=120.0, duration=240.0),
        )
        fade.build(_seconds(45), _seconds(45), PCM)
        assert fade.tempo_steps == []
        assert _savings_until(fade, fade.effective_end) == 0.0
        timing = fade.timing_info
        assert timing.pre_crossfade_duration + timing.crossfade_duration == pytest.approx(
            fade.effective_end, abs=0.05
        )
