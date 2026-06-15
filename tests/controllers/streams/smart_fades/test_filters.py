"""Tests for the smart fades FFmpeg filter builders.

The FrequencySweepFilter must produce a true frequency sweep: an ``asendcmd``
command sequence that moves the lowpass/highpass cutoff frequency over time,
rather than blending a fixed-cutoff wet path against the dry signal.
"""

from __future__ import annotations

import logging
import re

import pytest

from music_assistant.controllers.streams.smart_fades.filters import (
    CrossfadeFilter,
    FadeOutTrimFilter,
    FrequencySweepFilter,
)

LOGGER = logging.getLogger(__name__)

# matches "<timestamp> <filter>@<instance> f <frequency>" entries inside asendcmd
CMD_RE = re.compile(r"(\d+\.\d+) ((?:low|high)pass@\w+) f (\d+(?:\.\d+)?)")


def _make_filter(
    *,
    sweep_type: str = "lowpass",
    target_freq: int = 2000,
    duration: float = 10.0,
    start_time: float = 5.0,
    sweep_direction: str = "fade_in",
    poles: int = 1,
    curve_type: str = "linear",
    stream_type: str = "fadeout",
) -> FrequencySweepFilter:
    """Return a FrequencySweepFilter with sensible defaults for tests."""
    return FrequencySweepFilter(
        logger=LOGGER,
        sweep_type=sweep_type,
        target_freq=target_freq,
        duration=duration,
        start_time=start_time,
        sweep_direction=sweep_direction,
        poles=poles,
        curve_type=curve_type,
        stream_type=stream_type,
    )


def _parse_sweep(filter_strings: list[str]) -> tuple[str, list[tuple[float, float]]]:
    """Extract (sweep_chain, [(timestamp, frequency), ...]) from the filter strings."""
    sweep_chains = [f for f in filter_strings if "asendcmd" in f]
    assert len(sweep_chains) == 1, f"expected exactly one asendcmd chain in {filter_strings}"
    chain = sweep_chains[0]
    steps = [(float(ts), float(freq)) for ts, _, freq in CMD_RE.findall(chain)]
    assert steps, f"no frequency commands found in {chain}"
    return chain, steps


def test_lowpass_fade_in_sweeps_from_open_to_target() -> None:
    """Applying a lowpass sweeps the cutoff from fully open down to the target."""
    sweep = _make_filter(sweep_type="lowpass", sweep_direction="fade_in", target_freq=2000)
    chain, steps = _parse_sweep(sweep.apply("[fadein]", "[fadeout]"))

    freqs = [freq for _, freq in steps]
    assert freqs[0] >= 18000, "sweep must start with the filter effectively open"
    assert freqs[-1] == pytest.approx(2000, rel=0.01)
    assert freqs == sorted(freqs, reverse=True), "cutoff must move monotonically down"
    # the initial filter cutoff must match the first command so there is no jump
    assert f"=f={int(freqs[0])}" in chain


def test_highpass_fade_out_sweeps_from_target_to_open() -> None:
    """Removing a highpass sweeps the cutoff from the target down until open."""
    sweep = _make_filter(
        sweep_type="highpass",
        sweep_direction="fade_out",
        target_freq=2000,
        start_time=0.0,
        stream_type="fadein",
    )
    _, steps = _parse_sweep(sweep.apply("[fadein]", "[fadeout]"))

    freqs = [freq for _, freq in steps]
    assert freqs[0] == pytest.approx(2000, rel=0.01)
    assert freqs[-1] <= 30, "sweep must end with the highpass effectively open"
    assert freqs == sorted(freqs, reverse=True), "cutoff must move monotonically down"


def test_sweep_commands_cover_the_configured_window() -> None:
    """Command timestamps span [start_time, start_time + duration]."""
    sweep = _make_filter(start_time=5.0, duration=10.0)
    _, steps = _parse_sweep(sweep.apply("[fadein]", "[fadeout]"))

    timestamps = [ts for ts, _ in steps]
    assert timestamps[0] == pytest.approx(5.0, abs=0.01)
    assert timestamps[-1] == pytest.approx(15.0, abs=0.01)
    assert timestamps == sorted(timestamps)
    # fine-grained enough to be perceived as continuous
    assert len(timestamps) >= 50


def test_logarithmic_curve_sweeps_faster_initially_than_linear() -> None:
    """The logarithmic curve must reach a lower cutoff at mid-sweep than linear."""

    def freq_at_midpoint(curve_type: str) -> float:
        sweep = _make_filter(curve_type=curve_type, start_time=0.0, duration=10.0)
        _, steps = _parse_sweep(sweep.apply("[fadein]", "[fadeout]"))
        return min(freq for ts, freq in steps if ts <= 5.0)

    assert freq_at_midpoint("logarithmic") < freq_at_midpoint("linear")


def test_exponential_curve_sweeps_slower_initially_than_linear() -> None:
    """The exponential curve must keep a higher cutoff at mid-sweep than linear."""

    def freq_at_midpoint(curve_type: str) -> float:
        sweep = _make_filter(curve_type=curve_type, start_time=0.0, duration=10.0)
        _, steps = _parse_sweep(sweep.apply("[fadein]", "[fadeout]"))
        return min(freq for ts, freq in steps if ts <= 5.0)

    assert freq_at_midpoint("exponential") > freq_at_midpoint("linear")


def test_poles_are_passed_through() -> None:
    """The poles option must end up in the generated filter chain."""
    sweep = _make_filter(poles=2)
    chain, _ = _parse_sweep(sweep.apply("[fadein]", "[fadeout]"))
    assert "poles=2" in chain


def test_fadeout_stream_labels_and_passthrough() -> None:
    """Processing the fadeout stream passes the fadein stream through untouched."""
    sweep = _make_filter(stream_type="fadeout")
    filter_strings = sweep.apply("[fadein]", "[fadeout]")

    assert sweep.output_fadeout_label == "fadeout_lowpass"
    assert sweep.output_fadein_label == "fadein_passthrough"
    passthrough = next(f for f in filter_strings if "anull" in f)  # codespell:ignore anull
    assert passthrough.startswith("[fadein]")
    assert passthrough.endswith("[fadein_passthrough]")
    sweep_chain, _ = _parse_sweep(filter_strings)
    assert sweep_chain.startswith("[fadeout]")
    assert sweep_chain.endswith("[fadeout_lowpass]")


def test_fadein_stream_labels_and_passthrough() -> None:
    """Processing the fadein stream passes the fadeout stream through untouched."""
    sweep = _make_filter(
        sweep_type="highpass", sweep_direction="fade_out", stream_type="fadein", start_time=0.0
    )
    filter_strings = sweep.apply("[fadein]", "[fadeout]")

    assert sweep.output_fadeout_label == "fadeout_passthrough"
    assert sweep.output_fadein_label == "fadein_highpass"
    passthrough = next(f for f in filter_strings if "anull" in f)  # codespell:ignore anull
    assert passthrough.startswith("[fadeout]")
    assert passthrough.endswith("[fadeout_passthrough]")
    sweep_chain, _ = _parse_sweep(filter_strings)
    assert sweep_chain.startswith("[fadein]")
    assert sweep_chain.endswith("[fadein_highpass]")


def test_sweep_instances_are_unique_per_stream_type() -> None:
    """Both stream types must use distinct filter instance names for asendcmd."""
    fadeout_sweep = _make_filter(stream_type="fadeout")
    fadein_sweep = _make_filter(
        sweep_type="highpass", sweep_direction="fade_out", stream_type="fadein"
    )
    out_chain, _ = _parse_sweep(fadeout_sweep.apply("[fadein]", "[fadeout]"))
    in_chain, _ = _parse_sweep(fadein_sweep.apply("[fadein]", "[fadeout]"))
    out_match = CMD_RE.search(out_chain)
    in_match = CMD_RE.search(in_chain)
    assert out_match is not None
    assert in_match is not None
    assert out_match.group(2) != in_match.group(2)


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


def test_crossfade_uses_equal_power_curves() -> None:
    """The level crossfade must use equal-power curves, not ffmpeg's default tri/tri."""
    crossfade = CrossfadeFilter(logger=LOGGER, crossfade_duration=12.5)
    filter_strings = crossfade.apply("[fadein]", "[fadeout]")
    assert filter_strings == ["[fadeout][fadein]acrossfade=d=12.5:c1=qsin:c2=qsin"]
