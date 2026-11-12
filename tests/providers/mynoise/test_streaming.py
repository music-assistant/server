"""Tests for the myNoise ffmpeg mix helpers."""

import pytest
from music_assistant_models.enums import ContentType
from music_assistant_models.errors import AudioError

from music_assistant.providers.mynoise.catalog import Machine, MachineLayer
from music_assistant.providers.mynoise.constants import OUTPUT_SAMPLE_RATE
from music_assistant.providers.mynoise.streaming import (
    build_ffmpeg_args,
    pcm_output_format,
    resolve_layer_gains,
)


def _machine() -> Machine:
    return Machine(
        machine_id="M",
        name="Mix Machine",
        layers=[
            MachineLayer("a1", "b1", 0.5),
            MachineLayer("a2", "b2", 0.5),
        ],
    )


def _filter(args: list[str]) -> str:
    """Return the filter_complex argument from built ffmpeg args."""
    return args[args.index("-filter_complex") + 1]


def test_build_ffmpeg_args_mixes_active_layers() -> None:
    """All active layers become looped inputs joined by a non-normalizing amix."""
    args = build_ffmpeg_args(_machine(), [1.0, 0.5])
    assert args.count("-i") == 2
    assert args.count("-stream_loop") == 2
    flt = _filter(args)
    assert "amix=inputs=2:normalize=0:duration=longest" in flt
    assert "volume=1.000[a0]" in flt
    assert "volume=0.500[a1]" in flt


def test_build_ffmpeg_args_skips_muted_layers() -> None:
    """A muted layer gets no input and the single survivor bypasses the mixer."""
    args = build_ffmpeg_args(_machine(), [1.0, 0.0])
    assert args.count("-i") == 1
    flt = _filter(args)
    assert "anull[mix]" in flt
    assert "amix" not in flt


def test_build_ffmpeg_args_selects_variant_b() -> None:
    """The alternate takes are used when variant B is requested."""
    args = build_ffmpeg_args(_machine(), [1.0, 1.0], use_variant_b=True)
    assert "b1" in args
    assert "b2" in args
    assert "a1" not in args


def test_build_ffmpeg_args_paces_only_remote_sources() -> None:
    """Only remote sources are paced with -re; local files rely on backpressure."""
    machine = Machine(
        machine_id="M",
        name="Mixed Sources",
        layers=[
            MachineLayer("https://cdn/x.mp3", "https://cdn/xb.mp3", 0.5),
            MachineLayer("/cache/y.mp3", "/cache/yb.mp3", 0.5),
        ],
    )
    args = build_ffmpeg_args(machine, [1.0, 1.0])
    assert args.count("-re") == 1
    paced = args.index("-re")
    assert args[paced + 1] == "-i"
    assert args[paced + 2] == "https://cdn/x.mp3"


def test_build_ffmpeg_args_master_gain_and_output_format() -> None:
    """The master gain and the s16le stereo output format land in the filter."""
    flt = _filter(build_ffmpeg_args(_machine(), [1.0, 1.0], master_gain_db=-6.0))
    assert "volume=-6.0dB" in flt
    assert f"sample_rates={OUTPUT_SAMPLE_RATE}" in flt
    assert "channel_layouts=stereo" in flt


def test_build_ffmpeg_args_all_muted_raises() -> None:
    """A machine with no active layers cannot be mixed."""
    with pytest.raises(AudioError):
        build_ffmpeg_args(_machine(), [0.0, 0.0])


def test_resolve_layer_gains() -> None:
    """The 0..1 default curve maps to linear gains (0.5 is unity)."""
    assert resolve_layer_gains(_machine()) == [1.0, 1.0]
    quiet = Machine(
        machine_id="Q",
        name="Quiet",
        layers=[MachineLayer("a1", "b1", 0.25), MachineLayer("a2", "b2", 0.0)],
    )
    assert resolve_layer_gains(quiet) == [0.5, 0.0]


def test_resolve_layer_gains_offset_and_overrides() -> None:
    """A uniform dB offset scales all gains; overrides win outright."""
    machine = _machine()
    offset = resolve_layer_gains(machine, band_offset_db=6.0)
    assert offset[0] == pytest.approx(10 ** (6.0 / 20))
    overridden = resolve_layer_gains(machine, overrides={0: 0.1})
    assert overridden == [0.1, 1.0]


def test_pcm_output_format() -> None:
    """The mixed stream is delivered as signed 16-bit stereo PCM."""
    audio_format = pcm_output_format()
    assert audio_format.content_type == ContentType.PCM_S16LE
    assert (audio_format.sample_rate, audio_format.bit_depth, audio_format.channels) == (
        44100,
        16,
        2,
    )
