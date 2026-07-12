"""Tests for the ffmpeg helper module."""

from __future__ import annotations

import subprocess
from collections.abc import AsyncGenerator
from pathlib import Path

import pytest
from music_assistant_models.enums import ContentType
from music_assistant_models.media_items import AudioFormat

from music_assistant.helpers.ffmpeg import (
    FFMpegStreamInfo,
    get_ffmpeg_overlay_stream,
    parse_ffmpeg_duration,
    parse_ffmpeg_stream_info,
)

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
    """Generate a 1 second sine-tone wav file to use as overlay source."""
    overlay_path = tmp_path / "overlay.wav"
    subprocess.run(  # noqa: S603
        ["ffmpeg", "-f", "lavfi", "-i", "sine=frequency=440:duration=1", str(overlay_path)],  # noqa: S607
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
