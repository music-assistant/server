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
from unittest.mock import AsyncMock, MagicMock

import numpy as np
import pytest
from music_assistant_models.enums import ContentType, MediaType, StreamType
from music_assistant_models.media_items import AudioFormat
from music_assistant_models.streamdetails import StreamDetails

from music_assistant.controllers.streams.smart_fades.fades import (
    CrossfadeTimingInfo,
    SmartCrossFade,
    SmartFade,
    StandardCrossFade,
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


def _analysis(bpm: float, beats_start: float = 0.0, beats_count: int = 200) -> AudioAnalysisData:
    """Synthetic analysis data with enough beats for SmartCrossFade._build() to succeed."""
    interval = 60.0 / bpm  # seconds per beat
    beats = _beats(beats_start, beats_count, interval)
    downbeats = beats[::4]  # 4/4 time signature
    return AudioAnalysisData(
        duration=beats[-1] + interval,
        bpm=bpm,
        beats=beats,
        downbeats=downbeats,
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
    async def test_standard_mode_returns_standard_crossfade(self) -> None:
        """Standard mode always builds a StandardCrossFade with the configured duration."""
        mixer = _make_mixer()
        fade = await mixer.build(
            fade_in_streamdetails=_streamdetails("in"),
            fade_out_streamdetails=_streamdetails("out"),
            pcm_format=PCM,
            standard_crossfade_duration=8,
            mode=SmartFadesMode.STANDARD_CROSSFADE,
            fade_out_bytes_len=_seconds(20),
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
        fade = await mixer.build(
            fade_in_streamdetails=_streamdetails("in"),
            fade_out_streamdetails=_streamdetails("out"),
            pcm_format=PCM,
            standard_crossfade_duration=10,
            mode=SmartFadesMode.SMART_CROSSFADE,
            fade_out_bytes_len=_seconds(SMART_CROSSFADE_DURATION),
            fade_in_bytes_len=_seconds(SMART_CROSSFADE_DURATION),
        )
        assert isinstance(fade, SmartCrossFade)
        timing = fade.timing_info
        # SmartCrossFade applies a beat-aligned trim, so TRIM is non-zero.
        assert timing.fadein_trimmed_duration > 0
        assert timing.crossfade_duration > 0
        # Invariants the flow loop depends on:
        #   PRE + CF == fade_out_seconds  (A's audio fully accounted for)
        #   TRIM + CF + POST == fade_in_seconds  (B's audio fully accounted for)
        fade_out_seconds = float(SMART_CROSSFADE_DURATION)
        fade_in_seconds = float(SMART_CROSSFADE_DURATION)
        assert timing.pre_crossfade_duration + timing.crossfade_duration == pytest.approx(
            fade_out_seconds, abs=0.01
        )
        assert (
            timing.fadein_trimmed_duration
            + timing.crossfade_duration
            + timing.post_crossfade_duration
            == pytest.approx(fade_in_seconds, abs=0.01)
        )

    @pytest.mark.asyncio
    async def test_smart_mode_falls_back_when_no_analysis(self) -> None:
        """No audio analysis -> build() falls back to StandardCrossFade."""
        mixer = _make_mixer()
        fade = await mixer.build(
            fade_in_streamdetails=_streamdetails("in"),
            fade_out_streamdetails=_streamdetails("out"),
            pcm_format=PCM,
            standard_crossfade_duration=10,
            mode=SmartFadesMode.SMART_CROSSFADE,
            fade_out_bytes_len=_seconds(45),
            fade_in_bytes_len=_seconds(45),
        )
        assert isinstance(fade, StandardCrossFade)
        timing = fade.timing_info
        assert timing.crossfade_duration == pytest.approx(10.0)
        assert timing.fadein_trimmed_duration == 0.0
        assert timing.pre_crossfade_duration == pytest.approx(35.0)
        assert timing.post_crossfade_duration == pytest.approx(35.0)

    @pytest.mark.asyncio
    async def test_smart_mode_falls_back_when_analysis_missing_bpm(self) -> None:
        """Analysis present but missing bpm/beats -> falls back to standard."""
        incomplete = AudioAnalysisData(duration=180.0, bpm=None, beats=None)
        mixer = _make_mixer({"out": incomplete, "in": incomplete})
        fade = await mixer.build(
            fade_in_streamdetails=_streamdetails("in"),
            fade_out_streamdetails=_streamdetails("out"),
            pcm_format=PCM,
            standard_crossfade_duration=10,
            mode=SmartFadesMode.SMART_CROSSFADE,
            fade_out_bytes_len=_seconds(30),
            fade_in_bytes_len=_seconds(30),
        )
        assert isinstance(fade, StandardCrossFade)
        assert fade.timing_info.fadein_trimmed_duration == 0.0

    @pytest.mark.asyncio
    async def test_build_returns_fade_with_timing_info_readable(self) -> None:
        """timing_info is queryable immediately after build() — no apply() needed."""
        mixer = _make_mixer()
        fade = await mixer.build(
            fade_in_streamdetails=_streamdetails("in"),
            fade_out_streamdetails=_streamdetails("out"),
            pcm_format=PCM,
            standard_crossfade_duration=10,
            mode=SmartFadesMode.STANDARD_CROSSFADE,
            fade_out_bytes_len=_seconds(15),
            fade_in_bytes_len=_seconds(15),
        )
        assert isinstance(fade.timing_info, CrossfadeTimingInfo)
