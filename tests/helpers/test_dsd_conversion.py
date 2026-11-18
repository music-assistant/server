"""Characterization tests for local DSD decoding."""

from __future__ import annotations

import asyncio
import json
import math
import struct
import subprocess
from array import array
from collections.abc import Callable
from pathlib import Path
from typing import cast

import pytest
from music_assistant_models.enums import ContentType
from music_assistant_models.media_items import AudioFormat
from music_assistant_models.streamdetails import StreamDetails

from music_assistant.controllers.streams.audio_buffer import AudioBuffer, _buffer_pcm_format
from music_assistant.helpers.ffmpeg import get_ffmpeg_stream


def _write_dsf(path: Path, *, channels: int = 2, block_size: int = 4096) -> int:
    """Write a minimal, deterministic DSD64 DSF file and return its sample count."""
    sample_count = block_size * 8
    channel_data = bytes((0x69 if index % 2 == 0 else 0x96) for index in range(block_size))
    payload = channel_data * channels
    file_size = 28 + 52 + 12 + len(payload)
    path.write_bytes(
        b"DSD "
        + struct.pack("<QQQ", 28, file_size, 0)
        + b"fmt "
        + struct.pack(
            "<QIIIIIIQII",
            52,
            1,
            0,
            2 if channels == 2 else 1,
            channels,
            2_822_400,
            1,
            sample_count,
            block_size,
            0,
        )
        + b"data"
        + struct.pack("<Q", 12 + len(payload))
        + payload
    )
    return sample_count


def _dff_chunk(chunk_id: bytes, payload: bytes) -> bytes:
    """Pack one even-aligned DSDIFF chunk."""
    padding = b"\0" if len(payload) % 2 else b""
    return chunk_id + struct.pack(">Q", len(payload)) + payload + padding


def _write_dff(path: Path, *, channels: int = 2, bytes_per_channel: int = 4096) -> int:
    """Write a minimal, deterministic uncompressed DSD64 DSDIFF file."""
    sample_count = bytes_per_channel * 8
    channel_ids = b"SLFTSRGT" if channels == 2 else b"C   "
    properties = b"SND " + b"".join(
        (
            _dff_chunk(b"FS  ", struct.pack(">I", 2_822_400)),
            _dff_chunk(b"CHNL", struct.pack(">H", channels) + channel_ids),
            _dff_chunk(b"CMPR", b"DSD " + bytes((14,)) + b"not compressed"),
        )
    )
    audio = bytes(
        0x69 if (byte_index + channel) % 2 == 0 else 0x96
        for byte_index in range(bytes_per_channel)
        for channel in range(channels)
    )
    body = b"DSD " + b"".join(
        (
            _dff_chunk(b"FVER", struct.pack(">I", 0x01050000)),
            _dff_chunk(b"PROP", properties),
            _dff_chunk(b"DSD ", audio),
        )
    )
    path.write_bytes(b"FRM8" + struct.pack(">Q", len(body)) + body)
    return sample_count


def _probe(path: Path) -> dict[str, object]:
    """Return FFprobe's first audio-stream description for a fixture."""
    result = subprocess.run(  # noqa: S603
        [  # noqa: S607
            "ffprobe",
            "-v",
            "error",
            "-show_streams",
            "-of",
            "json",
            str(path),
        ],
        capture_output=True,
        check=False,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    return cast("dict[str, object]", json.loads(result.stdout)["streams"][0])


def test_ffmpeg_dsf_probe_contract(tmp_path: Path) -> None:
    """FFmpeg identifies DSF as DSD64 and exposes its one-byte decimation rate."""
    source = tmp_path / "synthetic.dsf"
    sample_count = _write_dsf(source)

    stream = _probe(source)

    assert stream["codec_name"] == "dsd_lsbf_planar"
    assert stream["sample_fmt"] == "fltp"
    assert stream["sample_rate"] == "352800"
    assert stream["channels"] == 2
    assert stream["duration_ts"] == sample_count // 8


def test_ffmpeg_dff_probe_contract(tmp_path: Path) -> None:
    """FFmpeg identifies uncompressed DSDIFF as interleaved MSB-first DSD64."""
    source = tmp_path / "synthetic.dff"
    sample_count = _write_dff(source)

    stream = _probe(source)

    assert stream["codec_name"] == "dsd_msbf"
    assert stream["sample_fmt"] == "fltp"
    assert stream["sample_rate"] == "352800"
    assert stream["channels"] == 2
    assert stream["duration_ts"] == sample_count // 8


async def test_dsf_buffer_duration_and_seek_match_decoded_frames(tmp_path: Path) -> None:
    """DSF probe depth must not turn three seconds of S32/F32 output into twelve."""
    source = tmp_path / "synthetic.dsf"
    _write_dsf(source, block_size=4096 * 260)
    source_format = AudioFormat(
        content_type=ContentType.DSF,
        codec_type=ContentType.DSD_LSBF_PLANAR,
        sample_rate=352800,
        bit_depth=8,
        channels=2,
    )
    details = StreamDetails(
        provider="test", item_id="dsf", audio_format=source_format, path=str(source)
    )
    pcm_format = _buffer_pcm_format(details)
    buffer = AudioBuffer(pcm_format)
    buffer.fill(
        get_ffmpeg_stream(
            audio_input=str(source),
            input_format=source_format,
            output_format=pcm_format,
            chunk_size=pcm_format.pcm_sample_size,
            extra_output_args=["-t", "3"],
        )
    )
    try:
        assert buffer._producer_task is not None
        await asyncio.wait_for(buffer._producer_task, timeout=10)
        decoded = b"".join([chunk async for chunk in buffer.get_raw_stream()])
        sought = b"".join([chunk async for chunk in buffer.get_raw_stream(seek_position_ms=1500)])
        assert len(decoded) == 352800 * 2 * 4 * 3
        assert buffer.duration_available == 3
        assert buffer.size_seconds == 3
        assert sought == decoded[352800 * 2 * 4 * 3 // 2 :]
    finally:
        await buffer.clear()


@pytest.mark.parametrize(
    ("suffix", "writer", "content_type", "codec_type"),
    [
        ("dsf", _write_dsf, ContentType.DSF, ContentType.DSD_LSBF_PLANAR),
        ("dff", _write_dff, ContentType.UNKNOWN, ContentType.DSD_MSBF),
    ],
)
async def test_ma_decodes_dsd_to_frame_exact_float_pcm(
    tmp_path: Path,
    suffix: str,
    writer: Callable[[Path], int],
    content_type: ContentType,
    codec_type: ContentType,
) -> None:
    """The MA FFmpeg wrapper emits one stereo float frame for every eight DSD samples."""
    source = tmp_path / f"synthetic.{suffix}"
    sample_count = writer(source)
    source_format = AudioFormat(
        content_type=content_type,
        codec_type=codec_type,
        sample_rate=352800,
        bit_depth=8,
        channels=2,
    )
    pcm_format = AudioFormat(
        content_type=ContentType.PCM_F32LE,
        codec_type=ContentType.PCM_F32LE,
        sample_rate=352800,
        bit_depth=32,
        channels=2,
    )

    async def _decode() -> bytes:
        return b"".join(
            [
                chunk
                async for chunk in get_ffmpeg_stream(
                    audio_input=str(source),
                    input_format=source_format,
                    output_format=pcm_format,
                )
            ]
        )

    first_decode = await _decode()
    second_decode = await _decode()
    decoded_samples = array("f")
    decoded_samples.frombytes(first_decode)

    assert len(first_decode) == (sample_count // 8) * pcm_format.channels * 4
    assert first_decode == second_decode
    assert decoded_samples
    assert all(math.isfinite(sample) for sample in decoded_samples)
    assert any(sample != 0 for sample in decoded_samples)
