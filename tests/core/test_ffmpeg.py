"""Tests for the ffmpeg helper module."""

from __future__ import annotations

from music_assistant_models.enums import ContentType

from music_assistant.helpers.ffmpeg import (
    FFMpegStreamInfo,
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
