"""Tests for the ffmpeg helper module."""

from __future__ import annotations

import asyncio
import subprocess
from array import array
from collections.abc import AsyncGenerator, Sequence
from math import sqrt
from pathlib import Path

import pytest
from music_assistant_models.enums import ContentType
from music_assistant_models.media_items import AudioFormat

from music_assistant.helpers.dsp import ComplexFilter, ComplexFilterInput
from music_assistant.helpers.ffmpeg import (
    _INPUT_READ_ARGS,
    FFMpeg,
    FFMpegStreamInfo,
    _build_filtergraph_args,
    _build_overlay_mixer,
    _get_overlay_volume_filter,
    get_ffmpeg_args,
    get_ffmpeg_overlay_stream,
    parse_ffmpeg_duration,
    parse_ffmpeg_stream_info,
)


def test_get_ffmpeg_args_does_not_mutate_filters() -> None:
    """Automatic resampling must not alter a caller-owned filter plan."""
    input_format = AudioFormat(
        content_type=ContentType.PCM_F32LE,
        sample_rate=96000,
        bit_depth=32,
        channels=2,
    )
    output_format = AudioFormat(
        content_type=ContentType.FLAC,
        sample_rate=48000,
        bit_depth=16,
        channels=2,
    )
    filter_params = ["volume=-1dB"]

    get_ffmpeg_args(input_format, output_format, filter_params)

    assert filter_params == ["volume=-1dB"]


def test_get_ffmpeg_args_downmixes_multichannel_for_single_channel_output() -> None:
    """A surround source is folded to stereo before the output is narrowed to one channel."""
    input_format = AudioFormat(
        content_type=ContentType.PCM_F32LE,
        sample_rate=48000,
        bit_depth=32,
        channels=6,
    )
    output_format = AudioFormat(
        content_type=ContentType.FLAC,
        sample_rate=48000,
        bit_depth=16,
        channels=1,
    )

    args = get_ffmpeg_args(input_format, output_format, ["pan=mono|c0=0.5*FL+0.5*FR"])

    filter_graph = args[args.index("-af") + 1]
    assert filter_graph.index("aformat=channel_layouts=stereo") < filter_graph.index(
        "pan=mono|c0=0.5*FL+0.5*FR"
    )


def _split_at_input(args: list[str]) -> tuple[list[str], list[str]]:
    """Split generated ffmpeg args into the part describing the input and the output."""
    idx = args.index("-i")
    return args[:idx], args[idx + 2 :]


@pytest.mark.parametrize(
    ("channels", "expected_layout"),
    [(1, "mono"), (2, "stereo")],
)
def test_get_ffmpeg_args_names_layout_up_to_stereo(channels: int, expected_layout: str) -> None:
    """Mono and stereo PCM are described by both their channel count and their layout."""
    fmt = AudioFormat(
        content_type=ContentType.PCM_S24LE,
        sample_rate=48000,
        bit_depth=24,
        channels=channels,
    )

    input_args, output_args = _split_at_input(get_ffmpeg_args(fmt, fmt, []))

    for part in (input_args, output_args):
        assert part[part.index("-ac") + 1] == str(channels)
        assert part[part.index("-channel_layout") + 1] == expected_layout


def test_get_ffmpeg_args_omits_layout_above_stereo() -> None:
    """Surround PCM is described by its channel count alone, never as a stereo layout."""
    fmt = AudioFormat(
        content_type=ContentType.PCM_S24LE,
        sample_rate=48000,
        bit_depth=24,
        channels=6,
    )

    input_args, output_args = _split_at_input(get_ffmpeg_args(fmt, fmt, []))

    for part in (input_args, output_args):
        assert part[part.index("-ac") + 1] == "6"
        assert "-channel_layout" not in part


def test_multichannel_pcm_folds_down_without_stretching(tmp_path: Path) -> None:
    """Raw surround PCM keeps its real width and length, and its rear channels survive."""
    source = tmp_path / "surround.pcm"
    out = tmp_path / "out.flac"
    # only the rear channels carry a tone: a downmix that drops them yields silence
    subprocess.run(  # noqa: S603
        [  # noqa: S607
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=1000:duration=1:sample_rate=48000",
            "-af",
            "pan=5.1|BL=c0|BR=c0",
            "-f",
            "s16le",
            str(source),
        ],
        check=True,
        capture_output=True,
    )
    pcm_format = AudioFormat(
        content_type=ContentType.PCM_S16LE,
        sample_rate=48000,
        bit_depth=16,
        channels=6,
    )
    output_format = AudioFormat(
        content_type=ContentType.FLAC,
        sample_rate=48000,
        bit_depth=16,
        channels=2,
    )
    args = get_ffmpeg_args(
        pcm_format, output_format, [], input_path=str(source), output_path=str(out)
    )

    result = subprocess.run([*args, "-y"], capture_output=True, text=True, check=False)  # noqa: S603
    assert result.returncode == 0, result.stderr

    duration = subprocess.run(  # noqa: S603
        [  # noqa: S607
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "csv=p=0",
            str(out),
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert float(duration) == pytest.approx(1.0, abs=0.05)
    assert _rms_db(out) > -40


def _output_args(args: list[str]) -> list[str]:
    """Return the output section of an ffmpeg command line (everything past the input path)."""
    return args[args.index("-i") + 2 :]


@pytest.mark.parametrize(
    ("content_type", "encoder_args"),
    [
        (ContentType.AAC, ["-f", "adts", "-c:a", "aac", "-b:a", "256k"]),
        (ContentType.MP3, ["-f", "mp3", "-b:a", "320k"]),
        (ContentType.WAV, ["-ar", "44100", "-acodec", "pcm_s16le", "-f", "wav"]),
        (
            ContentType.FLAC,
            ["-sample_fmt", "s16", "-ar", "44100", "-f", "flac", "-compression_level", "0"],
        ),
    ],
)
def test_get_ffmpeg_args_encoded_output_declares_channels(
    content_type: ContentType, encoder_args: list[str]
) -> None:
    """Every encoded output format is handed the requested channel count."""
    input_format = AudioFormat(
        content_type=ContentType.PCM_S16LE, sample_rate=44100, bit_depth=16, channels=2
    )
    output_format = AudioFormat(content_type=content_type, sample_rate=44100, bit_depth=16)

    args = get_ffmpeg_args(input_format, output_format, [])

    assert _output_args(args) == ["-ac", "2", "-channel_layout", "stereo", *encoder_args, "-"]


def test_get_ffmpeg_args_single_channel_output_declares_mono() -> None:
    """A one channel target is declared as mono rather than stereo."""
    fmt = AudioFormat(
        content_type=ContentType.PCM_S16LE, sample_rate=44100, bit_depth=16, channels=1
    )
    output_format = AudioFormat(
        content_type=ContentType.WAV, sample_rate=44100, bit_depth=16, channels=1
    )

    args = get_ffmpeg_args(fmt, output_format, [])

    assert _output_args(args) == [
        "-ac",
        "1",
        "-channel_layout",
        "mono",
        "-ar",
        "44100",
        "-acodec",
        "pcm_s16le",
        "-f",
        "wav",
        "-",
    ]


@pytest.mark.parametrize(
    ("output_path", "output_content_type", "expected"),
    [
        ("NULL", ContentType.FLAC, ["-f", "null", "-"]),
        ("-", ContentType.NUT, ["-vn", "-dn", "-sn", "-acodec", "copy", "-f", "nut", "-"]),
    ],
)
def test_get_ffmpeg_args_passthrough_sinks_omit_channels(
    output_path: str, output_content_type: ContentType, expected: list[str]
) -> None:
    """The analysis sink and the cache passthrough declare no channel count of their own."""
    input_format = AudioFormat(
        content_type=ContentType.PCM_S16LE, sample_rate=44100, bit_depth=16, channels=1
    )
    output_format = AudioFormat(
        content_type=output_content_type, sample_rate=44100, bit_depth=16, channels=1
    )

    args = get_ffmpeg_args(input_format, output_format, [], output_path=output_path)

    assert _output_args(args) == expected


def test_get_ffmpeg_args_duplicates_mono_source_for_stereo_output() -> None:
    """A mono source is widened by duplication, ahead of the caller's own filters."""
    input_format = AudioFormat(
        content_type=ContentType.PCM_S16LE, sample_rate=44100, bit_depth=16, channels=1
    )
    output_format = AudioFormat(
        content_type=ContentType.FLAC, sample_rate=44100, bit_depth=16, channels=2
    )

    args = get_ffmpeg_args(input_format, output_format, ["volume=-1dB"])

    assert args[args.index("-af") + 1] == "pan=stereo|c0=c0|c1=c0,volume=-1dB"


def test_get_ffmpeg_args_mono_source_to_mono_output_is_not_widened() -> None:
    """A mono source kept at one channel needs no channel filter at all."""
    fmt = AudioFormat(
        content_type=ContentType.PCM_S16LE, sample_rate=44100, bit_depth=16, channels=1
    )
    output_format = AudioFormat(
        content_type=ContentType.FLAC, sample_rate=44100, bit_depth=16, channels=1
    )

    args = get_ffmpeg_args(fmt, output_format, [])

    assert "-af" not in args


# -- parse_ffmpeg_stream_info --


def test_parse_stream_info_mp3() -> None:
    """Lossy MP3 line yields codec/sample rate/bit rate, but no bit depth."""
    line = "Stream #0:0: Audio: mp3, 44100 Hz, stereo, fltp, 320 kb/s"
    info = parse_ffmpeg_stream_info(line)
    assert info == FFMpegStreamInfo(
        codec=ContentType.MP3,
        sample_rate=44100,
        bit_depth=None,
        bit_rate=320,
    )


def test_parse_stream_info_aac_with_profile_and_language() -> None:
    """AAC line with profile annotation and language tag is parsed correctly."""
    line = "Stream #0:0(eng): Audio: aac (LC) (mp4a / 0x6134706D), 44100 Hz, stereo, fltp, 254 kb/s"
    info = parse_ffmpeg_stream_info(line)
    assert info is not None
    assert info.codec == ContentType.AAC
    assert info.sample_rate == 44100
    assert info.bit_rate == 254
    assert info.bit_depth is None


def test_parse_stream_info_flac_16bit() -> None:
    """16-bit FLAC: bit depth is inferred from the s16 sample format token."""
    line = "Stream #0:0: Audio: flac, 44100 Hz, stereo, s16, 1024 kb/s"
    info = parse_ffmpeg_stream_info(line)
    assert info == FFMpegStreamInfo(
        codec=ContentType.FLAC,
        sample_rate=44100,
        bit_depth=16,
        bit_rate=1024,
    )


def test_parse_stream_info_flac_24bit_in_s32() -> None:
    """24-bit FLAC is stored in s32; the explicit "(24 bit)" annotation wins."""
    line = "Stream #0:0: Audio: flac, 96000 Hz, stereo, s32 (24 bit)"
    info = parse_ffmpeg_stream_info(line)
    assert info == FFMpegStreamInfo(
        codec=ContentType.FLAC,
        sample_rate=96000,
        bit_depth=24,
        bit_rate=None,
    )


def test_parse_stream_info_flac_24bit_hires_with_bitrate() -> None:
    """High-resolution 24-bit FLAC at 192k with reported bit rate."""
    line = "Stream #0:0: Audio: flac, 192000 Hz, stereo, s32 (24 bit), 5644 kb/s"
    info = parse_ffmpeg_stream_info(line)
    assert info == FFMpegStreamInfo(
        codec=ContentType.FLAC,
        sample_rate=192000,
        bit_depth=24,
        bit_rate=5644,
    )


def test_parse_stream_info_pcm_s16le() -> None:
    """PCM stream reports codec via try_parse, sample format gives bit depth."""
    line = "Stream #0:0: Audio: pcm_s16le, 44100 Hz, stereo, s16, 1411 kb/s"
    info = parse_ffmpeg_stream_info(line)
    assert info == FFMpegStreamInfo(
        codec=ContentType.PCM_S16LE,
        sample_rate=44100,
        bit_depth=16,
        bit_rate=1411,
    )


def test_parse_stream_info_opus_without_bitrate() -> None:
    """Opus often omits bit rate; we still get codec and sample rate."""
    line = "Stream #0:0: Audio: opus, 48000 Hz, stereo, fltp"
    info = parse_ffmpeg_stream_info(line)
    assert info == FFMpegStreamInfo(
        codec=ContentType.OPUS,
        sample_rate=48000,
        bit_depth=None,
        bit_rate=None,
    )


def test_parse_stream_info_alac_planar() -> None:
    """ALAC reported with s16p (planar) sample format still yields 16-bit depth."""
    line = "Stream #0:0: Audio: alac (alac / 0x63616C61), 44100 Hz, stereo, s16p"
    info = parse_ffmpeg_stream_info(line)
    assert info is not None
    assert info.codec == ContentType.ALAC
    assert info.sample_rate == 44100
    assert info.bit_depth == 16


def test_parse_stream_info_returns_none_for_non_stream_line() -> None:
    """Non-stream log lines must return None."""
    assert parse_ffmpeg_stream_info("Duration: 00:03:25.78, start: 0.000000") is None
    assert parse_ffmpeg_stream_info("[error] Invalid data found") is None
    assert parse_ffmpeg_stream_info("") is None


def test_parse_stream_info_ignores_video_stream() -> None:
    """Video stream lines must not be misparsed as audio."""
    line = "Stream #0:0: Video: h264 (High), yuv420p, 1920x1080, 5000 kb/s, 25 fps"
    assert parse_ffmpeg_stream_info(line) is None


def test_parse_stream_info_unknown_codec_still_yields_other_fields() -> None:
    """Unrecognised codec token returns UNKNOWN but sample rate / bit rate are still parsed."""
    line = "Stream #0:0: Audio: somenewcodec, 48000 Hz, stereo, 192 kb/s"
    info = parse_ffmpeg_stream_info(line)
    assert info is not None
    assert info.codec == ContentType.UNKNOWN
    assert info.sample_rate == 48000
    assert info.bit_rate == 192
    assert info.bit_depth is None


# -- parse_ffmpeg_duration --


def test_parse_duration_typical() -> None:
    """Typical 'Duration: HH:MM:SS.ms' line yields total seconds (floor)."""
    line = "Duration: 00:03:25.78, start: 0.000000, bitrate: 320 kb/s"
    assert parse_ffmpeg_duration(line) == 3 * 60 + 25


def test_parse_duration_one_hour() -> None:
    """Hours component is honoured."""
    assert parse_ffmpeg_duration("Duration: 01:00:00.00, bitrate: 128 kb/s") == 3600


def test_parse_duration_under_one_second() -> None:
    """Sub-second durations round down to 0."""
    assert parse_ffmpeg_duration("Duration: 00:00:00.50, bitrate: 128 kb/s") == 0


def test_parse_duration_na_returns_none() -> None:
    """Live streams report 'Duration: N/A' — must not match."""
    assert parse_ffmpeg_duration("Duration: N/A, start: 0.000000, bitrate: N/A") is None


def test_parse_duration_unrelated_line_returns_none() -> None:
    """Random log lines must return None."""
    assert parse_ffmpeg_duration("Stream #0:0: Audio: mp3, 44100 Hz") is None
    assert parse_ffmpeg_duration("") is None


# -- get_ffmpeg_overlay_stream (end-to-end with a real ffmpeg process) --

_PCM_FORMAT = AudioFormat(
    content_type=ContentType.PCM_S16LE, sample_rate=44100, bit_depth=16, channels=2
)
_BYTES_PER_SECOND = _PCM_FORMAT.pcm_sample_size  # 1 second of PCM audio


@pytest.fixture
def overlay_file(tmp_path: Path) -> Path:
    """Generate a 1 second mono sine-tone wav file to use as overlay source."""
    overlay_path = tmp_path / "overlay.wav"
    subprocess.run(  # noqa: S603
        ["ffmpeg", "-f", "lavfi", "-i", "sine=frequency=440:duration=1", str(overlay_path)],  # noqa: S607
        check=True,
        capture_output=True,
    )
    return overlay_path


@pytest.fixture
def overlay_file_stereo(tmp_path: Path) -> Path:
    """Generate a stereo overlay wav carrying the same tone as ``overlay_file`` on both channels."""
    overlay_path = tmp_path / "overlay_stereo.wav"
    subprocess.run(  # noqa: S603
        [  # noqa: S607
            "ffmpeg",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=1",
            "-af",
            "pan=stereo|c0=c0|c1=c0",
            str(overlay_path),
        ],
        check=True,
        capture_output=True,
    )
    return overlay_path


@pytest.fixture
def overlay_file_wide_stereo(tmp_path: Path) -> Path:
    """Generate a 1 second stereo overlay wav with the tone on the left channel only."""
    overlay_path = tmp_path / "overlay_wide.wav"
    subprocess.run(  # noqa: S603
        [  # noqa: S607
            "ffmpeg",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=1",
            "-af",
            "pan=stereo|c0=c0",
            str(overlay_path),
        ],
        check=True,
        capture_output=True,
    )
    return overlay_path


@pytest.fixture
def overlay_file_with_silent_intro(tmp_path: Path) -> Path:
    """Generate a 2 second overlay wav that starts with 1s of silence then a 1s tone."""
    overlay_path = tmp_path / "overlay_silent_intro.wav"
    subprocess.run(  # noqa: S603
        [  # noqa: S607
            "ffmpeg",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=1",
            "-af",
            "adelay=1000:all=1",
            str(overlay_path),
        ],
        check=True,
        capture_output=True,
    )
    return overlay_path


async def _silence(seconds: int) -> AsyncGenerator[bytes]:
    """Yield the given amount of seconds of PCM silence in 1-second chunks."""
    for _ in range(seconds):
        yield b"\x00" * _BYTES_PER_SECOND


async def _collect_chunks(stream: AsyncGenerator[bytes]) -> list[bytes]:
    return [chunk async for chunk in stream]


def _samples(pcm: bytes, channel: int | None = None) -> Sequence[int]:
    """Return the samples of the given PCM audio, optionally for one channel only."""
    samples = array("h")
    samples.frombytes(pcm)
    return samples if channel is None else samples[channel :: _PCM_FORMAT.channels]


def _rms(samples: Sequence[int]) -> float:
    """Return the RMS level of the given PCM samples."""
    return sqrt(sum(sample * sample for sample in samples) / len(samples))


async def _mix_overlay(overlay_input: Path) -> bytes:
    """Mix the given overlay source into 1 second of silence and return the result."""
    return b"".join(
        await _collect_chunks(
            get_ffmpeg_overlay_stream(
                audio_input=_silence(1),
                overlay_input=str(overlay_input),
                pcm_format=_PCM_FORMAT,
            )
        )
    )


async def test_overlay_stream_mixes_loops_and_preserves_length(overlay_file: Path) -> None:
    """The overlay is looped and mixed in while length, format and chunking stay intact."""
    chunks = await _collect_chunks(
        get_ffmpeg_overlay_stream(
            audio_input=_silence(3),
            overlay_input=str(overlay_file),
            pcm_format=_PCM_FORMAT,
            chunk_size=_BYTES_PER_SECOND,
        )
    )
    output = b"".join(chunks)
    # duration=first: output length exactly matches the 3s main input
    assert len(output) == 3 * _BYTES_PER_SECOND
    # all chunks except the last are exactly chunk_size
    assert all(len(chunk) == _BYTES_PER_SECOND for chunk in chunks[:-1])
    # the main input was pure silence, so any signal proves the overlay was mixed in;
    # signal in the third second proves the 1s overlay file was looped
    assert any(output[:_BYTES_PER_SECOND])
    assert any(output[2 * _BYTES_PER_SECOND :])


async def test_overlay_stream_does_not_mutate_pcm_format(overlay_file: Path) -> None:
    """Mixing an overlay leaves the caller's PCM format unchanged."""
    pcm_format = AudioFormat(
        content_type=ContentType.PCM_F32LE,
        sample_rate=48000,
        bit_depth=32,
        channels=2,
    )
    original_format = pcm_format.to_dict()

    async def silence() -> AsyncGenerator[bytes]:
        yield b"\x00" * pcm_format.pcm_sample_size

    await _collect_chunks(
        get_ffmpeg_overlay_stream(
            audio_input=silence(),
            overlay_input=str(overlay_file),
            pcm_format=pcm_format,
        )
    )

    assert pcm_format.to_dict() == original_format


async def test_overlay_stream_applies_volume(overlay_file: Path) -> None:
    """Overlay volume 0% silences the overlay entirely (gain is applied)."""
    output = b"".join(
        await _collect_chunks(
            get_ffmpeg_overlay_stream(
                audio_input=_silence(1),
                overlay_input=str(overlay_file),
                pcm_format=_PCM_FORMAT,
                overlay_volume=0,
            )
        )
    )
    assert len(output) == _BYTES_PER_SECOND
    assert not any(output)


async def test_overlay_stream_trims_leading_silence(
    overlay_file_with_silent_intro: Path,
) -> None:
    """A near-silent intro on the overlay source is trimmed so it plays immediately."""
    output = b"".join(
        await _collect_chunks(
            get_ffmpeg_overlay_stream(
                audio_input=_silence(1),
                overlay_input=str(overlay_file_with_silent_intro),
                pcm_format=_PCM_FORMAT,
            )
        )
    )
    assert len(output) == _BYTES_PER_SECOND
    # without trimming, the first second would be the overlay's silent intro;
    # the trim makes the tone play from the start, so the first second has signal
    assert any(output)


async def test_overlay_stream_level_is_independent_of_source_channel_count(
    overlay_file: Path, overlay_file_stereo: Path
) -> None:
    """A mono overlay source mixes in at the same level as an identical stereo one."""
    mono_level = _rms(_samples(await _mix_overlay(overlay_file)))
    stereo_level = _rms(_samples(await _mix_overlay(overlay_file_stereo)))
    assert mono_level > 0
    # left to FFmpeg, the mono source would be spread at 1/sqrt(2) per channel
    # and land a factor sqrt(2) (3 dB) below the stereo one
    assert mono_level == pytest.approx(stereo_level, rel=0.02)


async def test_overlay_stream_preserves_stereo_image(overlay_file_wide_stereo: Path) -> None:
    """A stereo overlay keeps its channels apart instead of being folded to dual mono."""
    output = await _mix_overlay(overlay_file_wide_stereo)
    # the source carries the tone on the left only, so a fold would leak it into the right
    assert _rms(_samples(output, channel=0)) > 0
    assert not any(_samples(output, channel=1))


# -- overlay volume filter --


def test_overlay_volume_filter_compensates_only_for_a_stereo_output() -> None:
    """Only the widening to stereo costs a mono source level, so only there is it scaled up."""
    stereo_output = _get_overlay_volume_filter(100, 2)
    assert "nb_channels" in stereo_output
    # a comma would read as the end of this filter in the graph
    assert "," not in stereo_output
    # a mono source keeps its level when it is widened further, or not at all
    assert _get_overlay_volume_filter(100, 1) == "volume=1.0"
    assert _get_overlay_volume_filter(60, 6) == "volume=0.6"


def test_overlay_mixer_loops_its_source() -> None:
    """The overlay source is looped for as long as the main stream runs."""
    (overlay,) = _build_overlay_mixer("/sound.wav", _PCM_FORMAT, 100).inputs
    # a local file has nothing to reconnect to
    assert overlay.input_args == ["-stream_loop", "-1"]


def test_overlay_mixer_reconnects_for_http_sources() -> None:
    """An http overlay source additionally gets the reconnect options."""
    (overlay,) = _build_overlay_mixer("http://host/sound.mp3", _PCM_FORMAT, 100).inputs
    assert "-reconnect" in overlay.input_args
    assert overlay.input_args[-2:] == ["-stream_loop", "-1"]


def test_overlay_args_probe_the_main_input_and_add_no_filters() -> None:
    """Both inputs are read under our own limits and no filter is injected on top."""
    args = get_ffmpeg_args(
        _PCM_FORMAT, _PCM_FORMAT, [_build_overlay_mixer("/sound.wav", _PCM_FORMAT, 100)]
    )
    main_input = args.index("-i")
    # the limits must reach the main input, which is the first one, and the overlay
    assert args.index("-probesize") < main_input
    assert args.count("-probesize") == 2
    assert args.index("-stream_loop") > main_input
    # the overlay format matches the output, so nothing gets resampled or reconformed
    assert "-af" not in args
    assert args.count("-filter_complex") == 1
    assert not any(arg.startswith(("pan=", "aresample=resampler=")) for arg in args)


# -- _log_reader_task (decode-error flood guard) --


class _FakeStream:
    """Stand-in for a StreamReader/StreamWriter: already closed/at EOF, nothing to drain."""

    def is_closing(self) -> bool:
        return True

    def at_eof(self) -> bool:
        return True


class _FakeProc:
    """Minimal stand-in for asyncio.subprocess.Process — just enough for close() to run."""

    def __init__(self) -> None:
        self.pid = 12345
        self.returncode: int | None = None
        self.stdin: _FakeStream | None = _FakeStream()
        self.stdout = _FakeStream()

    async def communicate(self) -> tuple[bytes, bytes]:
        self.returncode = 0
        return b"", b""

    def send_signal(self, _sig: int) -> None:
        pass


class _FakeProcRacingExit(_FakeProc):
    """A no-stdin process that has already exited, so send_signal raises ProcessLookupError."""

    def __init__(self) -> None:
        super().__init__()
        # no stdin routes close() down the send_signal(SIGINT) branch
        self.stdin = None

    def send_signal(self, _sig: int) -> None:
        raise ProcessLookupError("no such process")


async def test_log_reader_reports_decode_errors_once_and_aborts() -> None:
    """
    Crossing the decode-error threshold logs a single line and aborts the stream.

    Regression test: previously every stderr line was re-promoted to ERROR for the
    rest of the process once 50 "Invalid data" lines were seen, flooding the log
    with thousands of lines for a single corrupted file.
    """
    ffmpeg = FFMpeg(audio_input="-", input_format=_PCM_FORMAT, output_format=_PCM_FORMAT)

    async def fake_stderr() -> AsyncGenerator[str]:
        for _ in range(60):
            yield "Invalid data found when processing input"
        # noise that a genuinely corrupted stream keeps emitting after the threshold;
        # none of this should reach ERROR level under the fix
        for _ in range(20):
            yield "Reserved bit set."

    ffmpeg.iter_stderr = fake_stderr  # type: ignore[method-assign]

    error_lines: list[str] = []
    ffmpeg.logger.error = lambda msg, *args: error_lines.append(msg % args if args else msg)  # type: ignore[method-assign]

    await ffmpeg._log_reader_task()
    assert ffmpeg._abort_task is not None
    await ffmpeg._abort_task

    assert error_lines == ["Excessive decode errors (50+) for this stream; aborting"]
    assert ffmpeg.closed


async def test_log_reader_below_threshold_does_not_abort() -> None:
    """A handful of decode errors, well under the threshold, triggers no report or abort."""
    ffmpeg = FFMpeg(audio_input="-", input_format=_PCM_FORMAT, output_format=_PCM_FORMAT)

    async def fake_stderr() -> AsyncGenerator[str]:
        for _ in range(10):
            yield "Invalid data found when processing input"
        yield "Reserved bit set."

    ffmpeg.iter_stderr = fake_stderr  # type: ignore[method-assign]

    error_lines: list[str] = []
    ffmpeg.logger.error = lambda msg, *args: error_lines.append(msg % args if args else msg)  # type: ignore[method-assign]

    await ffmpeg._log_reader_task()

    assert error_lines == []
    assert ffmpeg._abort_task is None
    assert not ffmpeg.closed


async def test_log_reader_abort_does_not_self_deadlock() -> None:
    """
    The detached abort task can close() the reader's own process without a self-await.

    Regression test for the deadlock this PR reintroduces abort-on-close around:
    close() does ``await asyncio.wait_for(self._stderr_reader_task, 5)``, and
    ``_stderr_reader_task`` here is wired to the very task running
    ``_log_reader_task`` — the same setup ``start()`` uses in production. If the
    abort were awaited inline from within ``_log_reader_task`` instead of via a
    detached task, that task would be awaiting itself, which asyncio turns into
    ``RuntimeError: Task cannot await on itself`` rather than a hang.
    """
    ffmpeg = FFMpeg(audio_input="-", input_format=_PCM_FORMAT, output_format=_PCM_FORMAT)
    ffmpeg.proc = _FakeProc()  # type: ignore[assignment]

    async def fake_stderr() -> AsyncGenerator[str]:
        for _ in range(50):
            yield "Invalid data found when processing input"

    ffmpeg.iter_stderr = fake_stderr  # type: ignore[method-assign]

    reader_task = asyncio.create_task(ffmpeg._log_reader_task())
    ffmpeg._stderr_reader_task = reader_task

    await asyncio.wait_for(reader_task, timeout=2)
    assert ffmpeg._abort_task is not None
    await asyncio.wait_for(ffmpeg._abort_task, timeout=2)

    assert ffmpeg.closed


async def test_abort_survives_send_signal_racing_process_exit() -> None:
    """
    The fire-and-forget abort must not crash if the process exits before it is signalled.

    close() sends SIGINT to no-stdin processes, which raises ProcessLookupError when the
    process already exited between the returncode check and the signal. The abort task is
    never awaited by a caller, so that race must be swallowed inside close() rather than
    escaping as an untracked "Task exception was never retrieved".
    """
    ffmpeg = FFMpeg(audio_input="-", input_format=_PCM_FORMAT, output_format=_PCM_FORMAT)
    ffmpeg.proc = _FakeProcRacingExit()  # type: ignore[assignment]

    async def fake_stderr() -> AsyncGenerator[str]:
        for _ in range(50):
            yield "Invalid data found when processing input"

    ffmpeg.iter_stderr = fake_stderr  # type: ignore[method-assign]

    reader_task = asyncio.create_task(ffmpeg._log_reader_task())
    ffmpeg._stderr_reader_task = reader_task

    await asyncio.wait_for(reader_task, timeout=2)
    assert ffmpeg._abort_task is not None
    # must complete without propagating ProcessLookupError
    await asyncio.wait_for(ffmpeg._abort_task, timeout=2)

    assert ffmpeg.closed


# -- _build_filtergraph_args (DSP chain assembly) --


def test_build_filtergraph_all_simple_uses_af() -> None:
    """A chain of plain filters renders to a single -af comma chain."""
    assert _build_filtergraph_args(["equalizer=x", "volume=3dB"]) == (
        [],
        ["-af", "equalizer=x,volume=3dB"],
    )


def test_build_filtergraph_empty_returns_no_args() -> None:
    """An empty chain produces no ffmpeg arguments."""
    assert _build_filtergraph_args([]) == ([], [])


def test_build_filtergraph_single_complex_fragment() -> None:
    """A complex fragment renders a labelled -filter_complex graph with -map."""
    result = _build_filtergraph_args(
        [ComplexFilter("afir=irnorm=1", [ComplexFilterInput("/ir.wav", "aresample=48000")])]
    )
    assert result == (
        [*_INPUT_READ_ARGS, "-i", "/ir.wav"],
        [
            "-filter_complex",
            "[1:a]aresample=48000[dsp1];[0:a][dsp1]afir=irnorm=1[dsp2]",
            "-map",
            "[dsp2]",
        ],
    )


def test_build_filtergraph_complex_between_simple_runs() -> None:
    """Simple runs on either side of a complex fragment weave into labelled pads."""
    result = _build_filtergraph_args(
        [
            "equalizer=x",
            ComplexFilter("afir=irnorm=1", [ComplexFilterInput("/ir.wav", "aresample=48000")]),
            "volume=2dB",
        ]
    )
    assert result == (
        [*_INPUT_READ_ARGS, "-i", "/ir.wav"],
        [
            "-filter_complex",
            "[0:a]equalizer=x[dsp1];[1:a]aresample=48000[dsp2];"
            "[dsp1][dsp2]afir=irnorm=1[dsp3];[dsp3]volume=2dB[dsp4]",
            "-map",
            "[dsp4]",
        ],
    )


def test_build_filtergraph_multiple_inputs() -> None:
    """A fragment with several inputs numbers them in order and feeds them to the body."""
    result = _build_filtergraph_args(
        [ComplexFilter("amerge", [ComplexFilterInput("/a.wav"), ComplexFilterInput("/b.wav")])]
    )
    assert result == (
        [*_INPUT_READ_ARGS, "-i", "/a.wav", *_INPUT_READ_ARGS, "-i", "/b.wav"],
        ["-filter_complex", "[0:a][1:a][2:a]amerge[dsp1]", "-map", "[dsp1]"],
    )


def test_build_filtergraph_input_args_precede_the_input() -> None:
    """An input's own ffmpeg options are emitted directly before its -i."""
    result = _build_filtergraph_args(
        [
            ComplexFilter(
                "amix=inputs=2",
                [ComplexFilterInput("/loop.wav", input_args=["-stream_loop", "-1"])],
            )
        ]
    )
    assert result == (
        [*_INPUT_READ_ARGS, "-stream_loop", "-1", "-i", "/loop.wav"],
        ["-filter_complex", "[0:a][1:a]amix=inputs=2[dsp1]", "-map", "[dsp1]"],
    )


def test_get_ffmpeg_args_uses_af_without_complex_filter() -> None:
    """Plain filter chains keep the -af path (no -filter_complex/-map)."""
    fmt = AudioFormat(
        content_type=ContentType.PCM_S16LE, sample_rate=48000, bit_depth=16, channels=2
    )
    args = get_ffmpeg_args(fmt, fmt, ["volume=-1dB"])
    assert "-af" in args
    assert "-filter_complex" not in args


def test_get_ffmpeg_args_uses_filter_complex_with_complex_filter() -> None:
    """A complex fragment switches the whole chain to -filter_complex with -map."""
    fmt = AudioFormat(
        content_type=ContentType.PCM_S16LE, sample_rate=48000, bit_depth=16, channels=2
    )
    args = get_ffmpeg_args(
        fmt, fmt, [ComplexFilter("afir=irnorm=1", [ComplexFilterInput("/ir.wav")])]
    )
    assert "-filter_complex" in args
    assert "-map" in args
    assert "-af" not in args
    # the impulse response is a real input, so it never passes through graph quoting
    assert args.count("-i") == 2
    assert args.index("/ir.wav") > args.index("-i")


def _rms_db(path: Path) -> float:
    """Return the overall RMS level of an audio file in dB via ffmpeg astats."""
    output = subprocess.run(  # noqa: S603
        [  # noqa: S607
            "ffmpeg",
            "-hide_banner",
            "-nostats",
            "-i",
            str(path),
            "-af",
            "astats=measure_perchannel=none",
            "-f",
            "null",
            "-",
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stderr
    for line in output.splitlines():
        if "RMS level dB" in line:
            return float(line.split("RMS level dB:")[-1])
    raise AssertionError("no RMS level in astats output")


def test_filtergraph_complex_runs_in_ffmpeg(tmp_path: Path) -> None:
    """The generated -filter_complex graph is valid and an identity IR passes audio through."""
    main = tmp_path / "main.wav"
    ir = tmp_path / "ir.wav"
    out = tmp_path / "out.wav"
    subprocess.run(  # noqa: S603
        [  # noqa: S607
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=1000:duration=1:sample_rate=48000",
            "-ac",
            "2",
            str(main),
        ],
        check=True,
        capture_output=True,
    )
    # a single-sample impulse is the identity IR: convolving with it returns the input
    subprocess.run(  # noqa: S603
        [  # noqa: S607
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "aevalsrc=eq(n\\,0):d=0.01:s=48000:c=stereo",
            str(ir),
        ],
        check=True,
        capture_output=True,
    )
    input_args, filter_args = _build_filtergraph_args(
        [ComplexFilter("afir=irnorm=1", [ComplexFilterInput(str(ir), "aresample=48000")])]
    )
    result = subprocess.run(  # noqa: S603
        [  # noqa: S607
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(main),
            *input_args,
            *filter_args,
            str(out),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert out.exists()
    # identity IR => output level matches input level
    assert abs(_rms_db(out) - _rms_db(main)) < 0.5


def test_mono_source_keeps_its_level_when_widened_to_stereo(tmp_path: Path) -> None:
    """Widening a mono source to stereo duplicates it, where a rematrix would cost 3 dB."""
    main = tmp_path / "mono.wav"
    out = tmp_path / "stereo.wav"
    subprocess.run(  # noqa: S603
        [  # noqa: S607
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=1000:duration=1:sample_rate=44100",
            "-ac",
            "1",
            str(main),
        ],
        check=True,
        capture_output=True,
    )
    args = get_ffmpeg_args(
        AudioFormat(content_type=ContentType.WAV, sample_rate=44100, bit_depth=16, channels=1),
        AudioFormat(content_type=ContentType.WAV, sample_rate=44100, bit_depth=16, channels=2),
        [],
        input_path=str(main),
        output_path=str(out),
    )
    result = subprocess.run(args, capture_output=True, text=True, check=False)  # noqa: S603

    assert result.returncode == 0, result.stderr
    assert abs(_rms_db(out) - _rms_db(main)) < 0.5
