"""Tests for crossfade transition timing math.

Covers the ``CrossfadeTimingInfo`` contract that drives lyrics-sync correctness:

  pre_crossfade_duration  + crossfade_duration         = portion attributed to A
  fadein_trimmed_duration + crossfade_duration         = where B's listener actually is
                                                          when CF ends (the value the
                                                          flow loop writes into
                                                          streamdetails.seek_position)

Pure math tests use a fake ``SmartFade`` subclass that just assigns the test-provided
timing in its ``_build``. Smart/standard end-to-end behavior is exercised through
``SmartFadesMixer.build``.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator
from unittest.mock import AsyncMock, MagicMock

import numpy as np
import pytest
from music_assistant_models.enums import ContentType, MediaType, StreamType
from music_assistant_models.media_items import AudioFormat
from music_assistant_models.streamdetails import StreamDetails

import music_assistant.controllers.streams.smart_fades.mixer as mixer_module
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
    FrequencySweepFilter,
    GradualTimeStretchFilter,
)
from music_assistant.controllers.streams.smart_fades.helpers import SMART_CROSSFADE_DURATION
from music_assistant.controllers.streams.smart_fades.mixer import SmartFadesMixer
from music_assistant.models.audio_analysis import AudioAnalysisData
from music_assistant.models.smart_fades import SmartFadesMode

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
    """Synthetic analysis data with enough beats for SmartCrossFade._build() to succeed."""
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
        beats=beats,
        downbeats=downbeats,
        rms_energy=rms_energy,
    )


class _FixedTimingFade(SmartFade):
    """Test-only SmartFade whose _build just assigns a caller-provided timing."""

    def __init__(self, timing: CrossfadeTimingInfo) -> None:
        super().__init__(logging.getLogger("test_fixed_timing_fade"))
        self._fixed_timing = timing

    def _build(
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
        """All fields default to 0.0 so _build can populate them incrementally."""
        timing = CrossfadeTimingInfo()
        assert timing.pre_crossfade_duration == 0.0
        assert timing.crossfade_duration == 0.0
        assert timing.fadein_trimmed_duration == 0.0
        assert timing.post_crossfade_duration == 0.0


# ---------------------------------------------------------------------------
# StandardCrossFade._build — timing math via the real subclass
# ---------------------------------------------------------------------------


class TestStandardCrossFadeBuild:
    """StandardCrossFade._build must produce the expected timing for given inputs."""

    def _build(
        self,
        crossfade_duration: float,
        fade_out_seconds: float,
        fade_in_seconds: float,
    ) -> CrossfadeTimingInfo:
        fade = StandardCrossFade(logger=logging.getLogger(), crossfade_duration=crossfade_duration)
        fade._build(_seconds(fade_out_seconds), _seconds(fade_in_seconds), PCM)
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
        fade._build(_seconds(6), _seconds(45), PCM)
        assert fade.timing_info.crossfade_duration == pytest.approx(6.0)
        crossfade_filter = fade.filters[0]
        assert isinstance(crossfade_filter, CrossfadeFilter)
        assert crossfade_filter.crossfade_duration == pytest.approx(6.0)


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
        fade._build(_seconds(6), _seconds(45), PCM)
        chunks = [
            chunk async for chunk in fade.apply(b"\x00" * _seconds(6), b"\x00" * _seconds(45), PCM)
        ]
        assert len(captured["fade_out"]) == _seconds(6)
        # nothing precedes the crossfade — the 6s buffer is consumed entirely by the overlap
        assert chunks[0] == crossfade_marker

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
        fade._build(0, _seconds(45), PCM)
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
        fade._build(0, 0, PCM)
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
            mode=SmartFadesMode.STANDARD_CROSSFADE,
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
            mode=SmartFadesMode.SMART_CROSSFADE,
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
        # rubberband compresses the tail; savings are computed via _stretch_savings_until.
        rendered_fade_out_seconds = fade.effective_end - fade._stretch_savings_until(
            fade.effective_end
        )
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
            mode=SmartFadesMode.SMART_CROSSFADE,
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
            mode=SmartFadesMode.SMART_CROSSFADE,
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
            mode=SmartFadesMode.STANDARD_CROSSFADE,
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
            mode=SmartFadesMode.STANDARD_CROSSFADE,
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
            mode=SmartFadesMode.SMART_CROSSFADE,
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
            mode=SmartFadesMode.SMART_CROSSFADE,
            fade_out_data=b"\x00" * _seconds(45),
            fade_in_bytes_len=_seconds(45),
        )
        assert isinstance(smart_fade, StandardCrossFade)
        assert smart_fade.trailing_silence_bytes == _seconds(5)

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
            mode=SmartFadesMode.STANDARD_CROSSFADE,
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
            mode=SmartFadesMode.STANDARD_CROSSFADE,
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
            mode=SmartFadesMode.STANDARD_CROSSFADE,
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
        fade._build(_seconds(45), _seconds(45), PCM)
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
        fade._build(partial_bytes, _seconds(45), PCM)
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
        fade._build(_seconds(45), _seconds(45), PCM)
        assert fade.timing_info.crossfade_duration <= fade.effective_end + 1e-6
        # the capped crossfade consumes the whole audible tail, so the stretch is skipped
        assert not any(isinstance(f, GradualTimeStretchFilter) for f in fade.filters)


# ---------------------------------------------------------------------------
# SmartCrossFade — rubberband stretch savings compensation
# ---------------------------------------------------------------------------


class TestStretchSavings:
    """Rendered-time savings from the stretch must reach timing and the sweep."""

    def _stretched_fade(self) -> SmartCrossFade:
        duration = 240.0
        # 4% BPM difference with >4 bars available -> stretch is applied
        fade = SmartCrossFade(
            logger=LOGGER,
            fade_out_analysis=_analysis(bpm=120.0, duration=duration),
            fade_in_analysis=_analysis(bpm=124.8, duration=duration),
        )
        fade._build(_seconds(45), _seconds(45), PCM)
        return fade

    def _fadeout_sweep(self, fade: SmartCrossFade) -> FrequencySweepFilter:
        return next(
            f
            for f in fade.filters
            if isinstance(f, FrequencySweepFilter) and f.stream_type == "fadeout"
        )

    def test_savings_until_integrates_piecewise(self) -> None:
        """_stretch_savings_until integrates savings piecewise over the step list."""
        fade = SmartCrossFade(
            logger=LOGGER,
            fade_out_analysis=_analysis(bpm=120.0),
            fade_in_analysis=_analysis(bpm=120.0),
        )
        fade.tempo_steps = [(35.0, 1.0), (40.0, 1.05)]
        assert fade._stretch_savings_until(35.0) == 0.0
        assert fade._stretch_savings_until(40.0) == 0.0  # ratio 1.0 segment saves nothing
        expected = 5.0 * (1.0 - 1.0 / 1.05)
        assert fade._stretch_savings_until(45.0) == pytest.approx(expected)

    def test_savings_until_handles_slowdown(self) -> None:
        """A ratio below 1.0 lengthens the rendered stream, so savings go negative."""
        fade = SmartCrossFade(
            logger=LOGGER,
            fade_out_analysis=_analysis(bpm=120.0),
            fade_in_analysis=_analysis(bpm=120.0),
        )
        fade.tempo_steps = [(35.0, 1.0), (40.0, 0.95)]
        expected = 5.0 * (1.0 - 1.0 / 0.95)
        assert expected < 0.0
        assert fade._stretch_savings_until(45.0) == pytest.approx(expected)

    def test_savings_counts_pre_step_span_at_first_ratio(self) -> None:
        """The span before the first step runs at the first step's ratio, not 1.0."""
        # rubberband is initialized at tempo_steps[0][1] from t=0, so a single
        # step at ts>0 with ratio != 1 stretches the whole stream from the start
        fade = SmartCrossFade(
            logger=LOGGER,
            fade_out_analysis=_analysis(bpm=120.0),
            fade_in_analysis=_analysis(bpm=120.0),
        )
        fade.tempo_steps = [(20.0, 1.004)]
        assert fade._stretch_savings_until(45.0) == pytest.approx(
            20.0 * (1.0 - 1.0 / 1.004) + 25.0 * (1.0 - 1.0 / 1.004)
        )
        assert fade._stretch_savings_until(10.0) == pytest.approx(10.0 * (1.0 - 1.0 / 1.004))

    def test_pre_plus_cf_equals_rendered_tail(self) -> None:
        """PRE + CF equals the rendered tail duration (buffer minus stretch savings)."""
        fade = self._stretched_fade()
        assert fade.tempo_steps, "test requires the stretch to be active"
        total_savings = fade._stretch_savings_until(fade.effective_end)
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
        fade._build(_seconds(45), _seconds(45), PCM)
        assert fade.tempo_steps, "test requires the stretch to be active"
        total_savings = fade._stretch_savings_until(fade.effective_end)
        assert total_savings < 0.0
        timing = fade.timing_info
        assert timing.pre_crossfade_duration + timing.crossfade_duration == pytest.approx(
            fade.effective_end - total_savings, abs=0.05
        )

    def test_sweep_schedule_ends_at_rendered_end(self) -> None:
        """Fade-out sweep schedule ends exactly at the rendered (output-time) tail end."""
        fade = self._stretched_fade()
        sweep = self._fadeout_sweep(fade)
        rendered_end = fade.effective_end - fade._stretch_savings_until(fade.effective_end)
        assert sweep.start_time + sweep.duration == pytest.approx(rendered_end, abs=0.05)

    def test_sweep_start_is_remapped_to_output_time(self) -> None:
        """The sweep start itself shifts left by the savings at its input-time position."""
        # The standard fixture cannot pin the start remap: its EQ window begins
        # before the stretch window, so the shift there is zero. A short (6-bar)
        # crossfade — high BPM plus late fade-in downbeats — pushes the EQ start
        # inside the stretch window where the remap actually binds.
        fade = SmartCrossFade(
            logger=LOGGER,
            fade_out_analysis=_analysis(bpm=230.0, duration=240.0),
            fade_in_analysis=_analysis(bpm=239.2, beats_start=38.0, duration=240.0),
        )
        fade._build(_seconds(45), _seconds(45), PCM)
        assert fade.tempo_steps, "test requires the stretch to be active"
        sweep = self._fadeout_sweep(fade)
        timing = fade.timing_info
        input_duration = min(max(timing.crossfade_duration * 2.5, 8.0), fade.effective_end)
        input_start = max(0.0, fade.effective_end - input_duration)
        start_shift = fade._stretch_savings_until(input_start)
        assert start_shift > 0.0, "fixture must place the EQ start inside the stretch window"
        assert sweep.start_time == pytest.approx(input_start - start_shift, abs=1e-9)
        rendered_end = fade.effective_end - fade._stretch_savings_until(fade.effective_end)
        assert sweep.start_time + sweep.duration == pytest.approx(rendered_end, abs=0.05)

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
        fade._build(_seconds(45), _seconds(45), PCM)
        # tail trim must come first so every later schedule sees the trimmed stream
        assert isinstance(fade.filters[0], FadeOutTrimFilter)
        assert fade.tempo_steps, "test requires the stretch to be active"
        rendered_end = fade.effective_end - fade._stretch_savings_until(fade.effective_end)
        timing = fade.timing_info
        assert timing.pre_crossfade_duration + timing.crossfade_duration == pytest.approx(
            rendered_end, abs=0.05
        )
        sweep = self._fadeout_sweep(fade)
        assert sweep.start_time + sweep.duration == pytest.approx(rendered_end, abs=0.05)

    def test_unstretched_fade_has_zero_savings(self) -> None:
        """Without a stretch, savings are zero and PRE + CF equals effective_end exactly."""
        fade = SmartCrossFade(
            logger=LOGGER,
            fade_out_analysis=_analysis(bpm=120.0, duration=240.0),
            fade_in_analysis=_analysis(bpm=120.0, duration=240.0),
        )
        fade._build(_seconds(45), _seconds(45), PCM)
        assert fade.tempo_steps == []
        assert fade._stretch_savings_until(fade.effective_end) == 0.0
        timing = fade.timing_info
        assert timing.pre_crossfade_duration + timing.crossfade_duration == pytest.approx(
            fade.effective_end, abs=0.05
        )
