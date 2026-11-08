"""Tests for DSD over PCM (DoP) packing."""

from __future__ import annotations

import os
import struct
from pathlib import Path

import numpy as np
import pytest
from music_assistant_models.enums import ContentType, StreamType
from music_assistant_models.media_items.audio_format import AudioFormat
from music_assistant_models.streamdetails import StreamDetails

from music_assistant.helpers.dop import (
    DoPError,
    DopMarkerState,
    get_dop_sample_rate,
    get_dop_stream,
    is_dop_source,
    read_dsf_info,
)

DSD64_RATE = 2822400
BLOCK_SIZE = 4096


def _write_dsf(path: Path, left: bytes, right: bytes, *, lsb_first: bool = True) -> None:
    """Write a minimal stereo DSF file whose channels hold the given DSD bytes."""
    assert len(left) == len(right)
    valid = len(left)
    blocks = -(-valid // BLOCK_SIZE)
    data = bytearray()
    for index in range(blocks):
        for channel in (left, right):
            chunk = channel[index * BLOCK_SIZE : (index + 1) * BLOCK_SIZE]
            data += chunk + bytes(BLOCK_SIZE - len(chunk))
    fmt_chunk = b"fmt " + struct.pack(
        "<QIIIIIIQI4x", 52, 1, 0, 2, 2, DSD64_RATE, 1 if lsb_first else 8, valid * 8, BLOCK_SIZE
    )
    data_chunk = b"data" + struct.pack("<Q", 12 + len(data)) + bytes(data)
    total = 28 + len(fmt_chunk) + len(data_chunk)
    dsd_chunk = b"DSD " + struct.pack("<QQQ", 28, total, 0)
    path.write_bytes(dsd_chunk + fmt_chunk + data_chunk)


def _reverse_bits(value: int) -> int:
    return int(f"{value:08b}"[::-1], 2)


def _pcm_format(content_type: ContentType, bit_depth: int, rate: int = 176400) -> AudioFormat:
    return AudioFormat(content_type=content_type, sample_rate=rate, bit_depth=bit_depth, channels=2)


async def _collect(path: Path, pcm_format: AudioFormat, seek: float = 0) -> bytes:
    return b"".join([chunk async for chunk in get_dop_stream(str(path), pcm_format, seek)])


def _words_from_s32(raw: bytes) -> np.ndarray:
    return np.frombuffer(raw, dtype="<u4") >> 8


def test_get_dop_sample_rate() -> None:
    """The library reports DSD at DSD rate / 8; DoP runs at DSD rate / 16."""
    dsd64 = AudioFormat(content_type=ContentType.DSF, sample_rate=352800, bit_depth=8)
    dsd128 = AudioFormat(content_type=ContentType.DSF, sample_rate=705600, bit_depth=8)
    flac = AudioFormat(content_type=ContentType.FLAC, sample_rate=176400, bit_depth=24)
    assert get_dop_sample_rate(dsd64) == 176400
    assert get_dop_sample_rate(dsd128) == 352800
    assert get_dop_sample_rate(flac) is None


def test_is_dop_source_needs_local_stereo_dsf() -> None:
    """Only a local stereo DSF file can be streamed as DoP."""
    dsd = AudioFormat(content_type=ContentType.DSF, sample_rate=352800, bit_depth=8, channels=2)
    local = StreamDetails(
        provider="test",
        item_id="1",
        audio_format=dsd,
        stream_type=StreamType.LOCAL_FILE,
        path="/music/a.dsf",
    )
    remote = StreamDetails(
        provider="test",
        item_id="2",
        audio_format=dsd,
        stream_type=StreamType.HTTP,
        path="http://host/a.dsf",
    )
    assert is_dop_source(local)
    assert not is_dop_source(remote)


async def test_dop_stream_markers_and_bit_order(tmp_path: Path) -> None:
    """Frames alternate 0x05/0xFA on both channels and carry MSB-first DSD bytes in order."""
    rng = np.random.default_rng(1)
    left = rng.integers(0, 256, BLOCK_SIZE * 3, dtype=np.uint8).tobytes()
    right = rng.integers(0, 256, BLOCK_SIZE * 3, dtype=np.uint8).tobytes()
    dsf = tmp_path / "test.dsf"
    _write_dsf(dsf, left, right)

    words = _words_from_s32(await _collect(dsf, _pcm_format(ContentType.PCM_S32LE, 32)))

    frames = words.reshape(-1, 2)
    assert len(frames) == len(left) // 2
    markers = frames >> 16
    assert (markers[0::2] == 0x05).all()
    assert (markers[1::2] == 0xFA).all()
    for channel, source in ((0, left), (1, right)):
        expected = [_reverse_bits(b) for b in source]
        got = np.stack([(frames[:, channel] >> 8) & 0xFF, frames[:, channel] & 0xFF], axis=1)
        assert got.reshape(-1).tolist() == expected


async def test_dop_stream_trims_padding_and_pads_odd_byte(tmp_path: Path) -> None:
    """The padding of the last DSF block is dropped; an odd last byte gets DSD silence."""
    left = bytes(range(256)) * 4 + b"\x01"
    right = bytes(reversed(range(256))) * 4 + b"\x02"
    dsf = tmp_path / "odd.dsf"
    _write_dsf(dsf, left, right, lsb_first=False)

    words = _words_from_s32(await _collect(dsf, _pcm_format(ContentType.PCM_S32LE, 32)))

    frames = words.reshape(-1, 2)
    assert len(frames) == (len(left) + 1) // 2
    assert frames[-1, 0] & 0xFFFF == 0x0169
    assert frames[-1, 1] & 0xFFFF == 0x0269


async def test_dop_stream_formats_are_bit_exact(tmp_path: Path) -> None:
    """F32, S32 and S24 output all carry the identical 24-bit DoP words."""
    rng = np.random.default_rng(2)
    left = rng.integers(0, 256, BLOCK_SIZE, dtype=np.uint8).tobytes()
    right = rng.integers(0, 256, BLOCK_SIZE, dtype=np.uint8).tobytes()
    dsf = tmp_path / "fmt.dsf"
    _write_dsf(dsf, left, right)

    s32 = _words_from_s32(await _collect(dsf, _pcm_format(ContentType.PCM_S32LE, 32)))
    f32 = np.frombuffer(await _collect(dsf, _pcm_format(ContentType.PCM_F32LE, 32)), dtype="<f4")
    s24 = np.frombuffer(await _collect(dsf, _pcm_format(ContentType.PCM_S24LE, 24)), np.uint8)

    from_f32 = np.round(f32.astype(np.float64) * (1 << 23)).astype(np.int64) & 0xFFFFFF
    s24_bytes = s24.reshape(-1, 3).astype(np.uint32)
    from_s24 = s24_bytes[:, 0] | (s24_bytes[:, 1] << 8) | (s24_bytes[:, 2] << 16)
    assert (from_f32 == s32).all()
    assert (from_s24 == s32).all()


async def test_dop_stream_seek_starts_at_block(tmp_path: Path) -> None:
    """Seeking starts at the DSF block holding the position, keeping markers valid."""
    left = bytes([0x11]) * BLOCK_SIZE + bytes([0x22]) * BLOCK_SIZE
    right = bytes([0x33]) * BLOCK_SIZE + bytes([0x44]) * BLOCK_SIZE
    dsf = tmp_path / "seek.dsf"
    _write_dsf(dsf, left, right, lsb_first=False)
    one_block_seconds = BLOCK_SIZE * 8 / DSD64_RATE

    words = _words_from_s32(
        await _collect(dsf, _pcm_format(ContentType.PCM_S32LE, 32), seek=one_block_seconds * 1.5)
    )

    frames = words.reshape(-1, 2)
    assert len(frames) == BLOCK_SIZE // 2
    assert (frames[:, 0] & 0xFFFF == 0x2222).all()
    assert frames[0, 0] >> 16 == 0x05


async def test_dop_stream_rejects_wrong_rate(tmp_path: Path) -> None:
    """A PCM rate that is not the DoP rate of the file cannot carry it."""
    dsf = tmp_path / "rate.dsf"
    _write_dsf(dsf, bytes(BLOCK_SIZE), bytes(BLOCK_SIZE))
    with pytest.raises(DoPError):
        await _collect(dsf, _pcm_format(ContentType.PCM_S32LE, 32, rate=192000))


def test_read_dsf_info(tmp_path: Path) -> None:
    """The DSF header is parsed into the layout needed for streaming."""
    dsf = tmp_path / "info.dsf"
    _write_dsf(dsf, bytes(10), bytes(10))
    info = read_dsf_info(str(dsf))
    assert info.channels == 2
    assert info.dsd_rate == DSD64_RATE
    assert info.dop_sample_rate == 176400
    assert info.bytes_per_channel == 10
    assert info.data_offset == 92


async def test_dop_markers_alternate_across_gapless_tracks(tmp_path: Path) -> None:
    """A track with an odd number of DoP frames hands the next track the other marker."""
    # 1026 bytes per channel = 513 DoP frames: the first track ends on marker 0x05
    first = tmp_path / "first.dsf"
    second = tmp_path / "second.dsf"
    _write_dsf(first, bytes(1026), bytes(1026))
    _write_dsf(second, bytes(1024), bytes(1024))
    pcm_format = _pcm_format(ContentType.PCM_S32LE, 32)
    state = DopMarkerState()

    raw_first = b"".join(
        [chunk async for chunk in get_dop_stream(str(first), pcm_format, marker_state=state)]
    )
    raw_second = b"".join(
        [chunk async for chunk in get_dop_stream(str(second), pcm_format, marker_state=state)]
    )
    markers = np.concatenate([_words_from_s32(raw_first), _words_from_s32(raw_second)])[::2] >> 16

    assert markers[512] == 0x05
    assert markers[513] == 0xFA
    assert np.all(markers[1:] != markers[:-1])


async def test_dop_stream_reads_an_open_descriptor(tmp_path: Path) -> None:
    """A descriptor is read in place and left open for its owner to close."""
    path = tmp_path / "track.dsf"
    _write_dsf(path, bytes(range(256)) * 4, bytes(range(256)) * 4)
    pcm_format = _pcm_format(ContentType.PCM_S32LE, 32)
    fd = os.open(path, os.O_RDONLY)
    try:
        from_fd = b"".join([chunk async for chunk in get_dop_stream(fd, pcm_format)])
        os.fstat(fd)  # still open
    finally:
        os.close(fd)
    assert from_fd == await _collect(path, pcm_format)
