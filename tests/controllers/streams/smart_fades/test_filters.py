"""Tests for the smart fades FFmpeg filter builders (the toolset)."""

from __future__ import annotations

import logging

from music_assistant.controllers.streams.smart_fades.filters import (
    ECHO_DECLICK_S,
    HIGHPASS_SWEEP_FRAME_SAMPLES,
    EchoOutFilter,
    FadeOutTrimFilter,
    HighPassSweepFilter,
    PeakFilter,
    ShelfFilter,
    ShelfType,
    StreamingCrossfadeFilter,
    VoiceOverMixFilter,
)

LOGGER = logging.getLogger(__name__)


def test_fadeout_trim_trims_fadeout_and_passes_fadein_through() -> None:
    """The fadeout stream is end-trimmed; the fadein stream is untouched."""
    fadeout_trim = FadeOutTrimFilter(logger=LOGGER, fadeout_end_pos=35.0, trimmed_seconds=10.0)
    filter_strings = fadeout_trim.apply("[fadein]", "[fadeout]")
    assert len(filter_strings) == 2
    assert repr(fadeout_trim) == "FadeOutTrim(end=35.00s, trimmed=10.00s)"

    trim_chain = next(f for f in filter_strings if "atrim" in f)
    assert trim_chain.startswith("[fadeout]")
    assert "atrim=end=35.000" in trim_chain
    assert "asetpts=PTS-STARTPTS" in trim_chain
    assert trim_chain.endswith(f"[{fadeout_trim.output_fadeout_label}]")

    passthrough = next(f for f in filter_strings if "anull" in f)  # codespell:ignore anull
    assert passthrough.startswith("[fadein]")
    assert passthrough.endswith(f"[{fadeout_trim.output_fadein_label}]")


def test_streaming_crossfade_blends_the_exact_overlap() -> None:
    """
    The blend fades both streams over the sample-exact overlap and sums them.

    afade+amix instead of acrossfade on purpose: acrossfade holds all output
    back until its second input hits EOF, which would stall a fade whose
    incoming side is still arriving. Equal-power qsin curves, not ffmpeg's
    default tri/tri. The final output stays unlabeled: an unconnected named
    output fails the whole graph.
    """
    crossfade = StreamingCrossfadeFilter(logger=LOGGER, crossfade_samples=441000)
    filter_strings = crossfade.apply("[fadein]", "[fadeout]")
    assert filter_strings == [
        "[fadeout]afade=t=out:start_sample=0:nb_samples=441000:curve=qsin[xfade_out]",
        "[fadein]afade=t=in:start_sample=0:nb_samples=441000:curve=qsin[xfade_in]",
        "[xfade_out][xfade_in]amix=inputs=2:normalize=0",
    ]


def test_streaming_crossfade_emits_the_given_curves() -> None:
    """Explicit fadeout/fadein curves override the default qsin:qsin pair."""
    crossfade = StreamingCrossfadeFilter(
        logger=LOGGER, crossfade_samples=441000, fadeout_curve="nofade", fadein_curve="tri"
    )
    filter_strings = crossfade.apply("[fadein]", "[fadeout]")
    assert "curve=nofade" in filter_strings[0]
    assert "curve=tri" in filter_strings[1]


def test_streaming_crossfade_positions_the_blend() -> None:
    """
    A positioned blend delays the incoming stream and hard-cuts the outgoing one.

    The pre-point places the fade on the outgoing stream, adelay (sample-exact,
    ``S`` suffix) aligns the incoming stream under it, and the trim at the
    planned end keeps any time-stretch drift out of the incoming audio.
    """
    crossfade = StreamingCrossfadeFilter(
        logger=LOGGER, crossfade_samples=441000, pre_crossfade_samples=882000
    )
    filter_strings = crossfade.apply("[fadein]", "[fadeout]")
    assert filter_strings == [
        "[fadeout]afade=t=out:start_sample=882000:nb_samples=441000:curve=qsin,"
        "atrim=end_sample=1323000[xfade_out]",
        "[fadein]afade=t=in:start_sample=0:nb_samples=441000:curve=qsin,"
        "adelay=882000S:all=1[xfade_in]",
        "[xfade_out][xfade_in]amix=inputs=2:normalize=0",
    ]


class TestShelfFilter:
    """asendcmd-driven shelving EQ on one stream, passthrough on the other."""

    def test_fadeout_lowshelf_strings(self) -> None:
        """A fadeout lowshelf emits an asendcmd gain schedule and a fadein passthrough."""
        f = ShelfFilter(
            LOGGER,
            ShelfType.LOW,
            100,
            [(0.0, 0.0), (30.0, -13.0), (31.0, -26.0)],
            "fadeout",
        )
        strings = f.apply("[1]", "[0]")
        assert len(strings) == 2
        # passthrough for the untouched stream
        assert any("anull" in s and "[1]" in s for s in strings)  # codespell:ignore anull
        chain = next(s for s in strings if "[0]" in s)
        assert "asendcmd=" in chain
        assert "lowshelf@fadeout_low" in chain
        assert "g=0.00" in chain  # initial gain from the first step
        assert "f=100" in chain
        assert "30.000 lowshelf@fadeout_low g -13.00" in chain

    def test_fadein_highshelf_processes_other_stream(self) -> None:
        """A fadein highshelf processes the incoming stream and passes the outgoing through."""
        f = ShelfFilter(LOGGER, ShelfType.HIGH, 13000, [(0.0, -20.0), (5.0, 0.0)], "fadein")
        strings = f.apply("[1]", "[0]")
        chain = next(s for s in strings if "[1]" in s)
        assert "highshelf@fadein_high" in chain
        assert "g=-20.00" in chain
        passthrough = next(s for s in strings if "[0]" in s)
        assert "anull" in passthrough  # codespell:ignore anull

    def test_labels_unique_per_band_and_stream(self) -> None:
        """Output labels differ per band and stream so four instances can coexist."""
        a = ShelfFilter(LOGGER, ShelfType.LOW, 100, [(0.0, -26.0)], "fadein")
        b = ShelfFilter(LOGGER, ShelfType.HIGH, 13000, [(0.0, -20.0)], "fadein")
        assert a.output_fadein_label != b.output_fadein_label
        assert a.output_fadeout_label != b.output_fadeout_label
        c = ShelfFilter(LOGGER, ShelfType.LOW, 100, [(0.0, -26.0)], "fadeout")
        assert a.output_fadein_label != c.output_fadein_label


class TestPeakFilter:
    """asendcmd-driven parametric peak EQ (mid swap) on one stream, passthrough on the other."""

    def test_fadeout_peak_strings(self) -> None:
        """A fadeout peak emits an asendcmd gain schedule and a fadein passthrough."""
        f = PeakFilter(
            LOGGER,
            1200,
            2.5,
            [(0.0, 0.0), (30.0, -4.0), (31.0, -8.0)],
            "fadeout",
        )
        strings = f.apply("[1]", "[0]")
        assert len(strings) == 2
        assert any("anull" in s and "[1]" in s for s in strings)  # codespell:ignore anull
        chain = next(s for s in strings if "[0]" in s)
        assert "asendcmd=" in chain
        assert "equalizer@fadeout_mid" in chain
        assert "g=0.00" in chain
        assert "f=1200:width_type=o:width=2.5" in chain
        assert "30.000 equalizer@fadeout_mid g -4.00" in chain

    def test_fadein_peak_processes_other_stream(self) -> None:
        """A fadein peak processes the incoming stream and passes the outgoing through."""
        f = PeakFilter(LOGGER, 1200, 2.5, [(0.0, -8.0), (5.0, 0.0)], "fadein")
        strings = f.apply("[1]", "[0]")
        chain = next(s for s in strings if "[1]" in s)
        assert "equalizer@fadein_mid" in chain
        assert "g=-8.00" in chain
        passthrough = next(s for s in strings if "[0]" in s)
        assert "anull" in passthrough  # codespell:ignore anull

    def test_labels_unique_and_no_collision_with_shelf(self) -> None:
        """Peak labels differ per stream and don't collide with ShelfFilter's labels."""
        a = PeakFilter(LOGGER, 1200, 2.5, [(0.0, -8.0)], "fadein")
        b = PeakFilter(LOGGER, 1200, 2.5, [(0.0, -8.0)], "fadeout")
        assert a.output_fadein_label != b.output_fadein_label
        low = ShelfFilter(LOGGER, ShelfType.LOW, 100, [(0.0, -26.0)], "fadein")
        high = ShelfFilter(LOGGER, ShelfType.HIGH, 13000, [(0.0, -20.0)], "fadein")
        assert a.output_fadein_label not in {low.output_fadein_label, high.output_fadein_label}
        assert a.output_fadeout_label not in {low.output_fadeout_label, high.output_fadeout_label}


class TestHighPassSweepFilter:
    """asendcmd-driven high-pass sweep on the outgoing stream, passthrough on the incoming."""

    def test_sweep_strings(self) -> None:
        """The cutoff starts at start_hz and steps log-spaced to end_hz over the window."""
        f = HighPassSweepFilter(LOGGER, 2.0, 5.0, start_hz=20.0, end_hz=600.0)
        strings = f.apply("[1]", "[0]")
        assert len(strings) == 2
        assert strings[0] == "[1]anull[fadein_pt_highpass]"  # codespell:ignore anull
        chain = strings[1]
        assert chain.startswith(f"[0]asetnsamples=n={HIGHPASS_SWEEP_FRAME_SAMPLES}:p=0,")
        # the filter only acts from the sweep start on
        assert chain.endswith(",highpass@fadeout_hp=f=20.0:enable='gte(t,2.000)'[fadeout_highpass]")
        steps = chain.split("c='")[1].split("'")[0].split("; ")
        # 3 s at 25 ms per step; nothing before the window, the end lands on end_hz
        assert len(steps) == 120
        assert steps[0] == "2.025 highpass@fadeout_hp f 20.6"
        assert steps[-1] == "5.000 highpass@fadeout_hp f 600.0"
        # halfway through time is halfway in octaves: the geometric mean of 20 and 600
        assert steps[59] == "3.500 highpass@fadeout_hp f 109.5"

    def test_zero_length_sweep_switches_at_start(self) -> None:
        """A sweep without duration is one switch to end_hz at its start."""
        f = HighPassSweepFilter(LOGGER, 1.5, 1.5, start_hz=30.0, end_hz=400.0)
        chain = f.apply("[1]", "[0]")[1]
        assert "asendcmd=c='1.500 highpass@fadeout_hp f 400.0'" in chain
        assert "highpass@fadeout_hp=f=30.0:enable='gte(t,1.500)'[" in chain

    def test_repr(self) -> None:
        """The repr names the frequency range and the window."""
        f = HighPassSweepFilter(LOGGER, 2.0, 5.0, start_hz=20.0, end_hz=600.0)
        assert repr(f) == "HighPassSweep(20->600Hz, 2.00s->5.00s)"


class TestEchoOutFilter:
    """Dry cut plus aecho taps of the last beat on the outgoing stream."""

    def test_echo_strings(self) -> None:
        """The dry side stops at the cut; the beat before it feeds one tap per decay."""
        f = EchoOutFilter(LOGGER, 4.0, 0.5)
        assert f.apply("[1]", "[0]") == [
            "[1]anull[fadein_pt_echo]",  # codespell:ignore anull
            "[0]asplit=2[echo_dry][echo_src]",
            f"[echo_dry]afade=t=out:st=4.000:d={ECHO_DECLICK_S}[echo_cut]",
            f"[echo_src]afade=t=in:st=3.500:d={ECHO_DECLICK_S},"
            f"afade=t=out:st=3.985:d={ECHO_DECLICK_S},"
            "aecho=in_gain=0:out_gain=1:delays=500.000|1000.000|1500.000|2000.000"
            ":decays=0.5|0.25|0.12|0.06[echo_wet]",
            "[echo_cut][echo_wet]amix=inputs=2:duration=first:normalize=0[fadeout_echo]",
        ]

    def test_cut_at_zero_clamps_the_gates(self) -> None:
        """A cut at the stream start keeps every gate at t=0 instead of before it."""
        strings = EchoOutFilter(LOGGER, 0.0, 0.5).apply("[1]", "[0]")
        assert "afade=t=out:st=0.000:" in strings[2]
        assert "afade=t=in:st=0.000:" in strings[3]
        assert "afade=t=out:st=0.000:" in strings[3]

    def test_repr(self) -> None:
        """The repr names the cut, the beat and the tap count."""
        assert repr(EchoOutFilter(LOGGER, 4.0, 0.5)) == "EchoOut(cut=4.00s, beat=0.500s, taps=4)"


def test_voice_over_mix_ducks_the_incoming_track_under_the_voice() -> None:
    """The voice plays untouched; the track is ducked, ramps back and the sum is limited."""
    voice_over = VoiceOverMixFilter(logger=LOGGER, overlap_seconds=3.0, ramp_seconds=0.4)
    assert voice_over.apply("[fadein]", "[fadeout]") == [
        "[fadein]volume=eval=frame:volume="
        "'1-0.6*max(0\\,min(1\\,(3.000+0.400-t)/0.400))'[voice_over_in]",
        "[fadeout][voice_over_in]amix=inputs=2:normalize=0,"
        "alimiter=limit=-0.5dB:level=false:latency=true",
    ]
