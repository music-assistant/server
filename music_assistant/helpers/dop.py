"""
DSD over PCM (DoP) packing for local DSD files.

DoP (DoP open standard v1.1) carries 1-bit DSD inside 24-bit PCM frames so it can
travel over any bit-perfect PCM path to a DAC that recognises it: each 24-bit word
holds a marker byte (alternating 0x05 / 0xFA per frame) followed by 16 DSD bits
of one channel, oldest bit first. The PCM rate is the DSD rate / 16, so DSD64
(2.8224 MHz) travels as 176.4 kHz PCM, DSD128 as 352.8 kHz and DSD256 as 705.6 kHz.

Only an untouched stream is DoP: any resampling, mixing, volume or other processing
on the way turns it into noise, which is why callers must bypass all of that.
"""

from __future__ import annotations

import asyncio
import os
import struct
from dataclasses import dataclass
from functools import cache
from typing import TYPE_CHECKING

from music_assistant_models.enums import ContentType, StreamType

from music_assistant.helpers.audio import pcm_container_bit_depth

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    import numpy as np
    from music_assistant_models.media_items.audio_format import AudioFormat
    from music_assistant_models.streamdetails import StreamDetails

DOP_MARKERS = (0x05, 0xFA)
# DSD idle pattern, used to pad a trailing odd byte so the last frame stays valid DoP
DSD_SILENCE_BYTE = 0x69
# ffprobe (and therefore the library) reports a DSD stream at DSD rate / 8
_PROBED_DSD_RATE_DIVISOR = 8
_DOP_BITS_PER_FRAME = 16
# number of DSF blocks read per worker-thread call (~0.75 s of DSD64 audio)
_BLOCKS_PER_READ = 64

# numpy is imported on first use so it does not weigh on the server start-up


class DoPError(Exception):
    """Raised when a file cannot be streamed as DoP."""


@dataclass
class DopMarkerState:
    """
    Marker phase carried from one DoP stream into the next.

    The markers have to keep alternating across a gapless track boundary: a file with an
    odd number of DoP frames ends on the marker the next file would start with, and a DAC
    that sees the same marker twice drops out of DSD mode for a moment.
    """

    phase: int = 0


@dataclass(frozen=True)
class DsfInfo:
    """Layout details of a DSF file needed to stream it."""

    channels: int
    dsd_rate: int
    lsb_first: bool
    sample_count: int
    block_size: int
    data_offset: int

    @property
    def dop_sample_rate(self) -> int:
        """Return the PCM sample rate that carries this file as DoP."""
        return self.dsd_rate // _DOP_BITS_PER_FRAME

    @property
    def bytes_per_channel(self) -> int:
        """Return the number of valid DSD bytes per channel."""
        return self.sample_count // 8


def get_dop_sample_rate(audio_format: AudioFormat) -> int | None:
    """
    Return the PCM rate a DSD source travels at as DoP, or None if it is not DSD.

    :param audio_format: The (probed) audio format of the source.
    """
    if audio_format.content_type != ContentType.DSF or not audio_format.sample_rate:
        return None
    dsd_rate = audio_format.sample_rate * _PROBED_DSD_RATE_DIVISOR
    return dsd_rate // _DOP_BITS_PER_FRAME


def is_dop_source(streamdetails: StreamDetails) -> bool:
    """
    Return whether a stream can be sent as DoP.

    Only local DSF files qualify: DoP needs the raw DSD bits, which only the file holds.

    :param streamdetails: The stream details of the source.
    """
    return (
        streamdetails.stream_type == StreamType.LOCAL_FILE
        and isinstance(streamdetails.path, str)
        and streamdetails.audio_format.channels == 2
        and get_dop_sample_rate(streamdetails.audio_format) is not None
    )


def read_dsf_info(path: str) -> DsfInfo:
    """
    Parse the header of a DSF file.

    :param path: Path to the DSF file.
    """
    fd = os.open(path, os.O_RDONLY)
    try:
        return read_dsf_info_fd(fd, path)
    finally:
        os.close(fd)


def read_dsf_info_fd(fd: int, name: str = "") -> DsfInfo:
    """
    Parse the header of an open DSF file.

    :param fd: Descriptor of the DSF file, read with positional reads only.
    :param name: Name of the file, for error messages.
    """
    header = os.pread(fd, 92, 0)
    if len(header) < 92 or header[0:4] != b"DSD " or header[28:32] != b"fmt ":
        raise DoPError(f"Not a DSF file: {name}")
    fmt_chunk_size = struct.unpack_from("<Q", header, 32)[0]
    channels, dsd_rate, bits_per_sample, sample_count, block_size = struct.unpack_from(
        "<IIIQI", header, 52
    )
    data_chunk_start = 28 + fmt_chunk_size
    data_header = os.pread(fd, 12, data_chunk_start)
    if len(data_header) < 12 or data_header[0:4] != b"data":
        raise DoPError(f"DSF file has no data chunk: {name}")
    if bits_per_sample not in (1, 8):
        raise DoPError(f"Unsupported DSF bit order {bits_per_sample}: {name}")
    if block_size <= 0 or channels <= 0:
        raise DoPError(f"Invalid DSF layout: {name}")
    return DsfInfo(
        channels=channels,
        dsd_rate=dsd_rate,
        lsb_first=bits_per_sample == 1,
        sample_count=sample_count,
        block_size=block_size,
        data_offset=data_chunk_start + 12,
    )


def pack_dop(dsd: np.ndarray, marker_phase: int) -> tuple[np.ndarray, int]:
    """
    Pack MSB-first DSD bytes into interleaved 24-bit DoP words.

    :param dsd: uint8 array of shape (channels, bytes), MSB-first, even byte count.
    :param marker_phase: Index (0/1) of the marker the first frame must carry.
    :return: The uint32 words (24-bit value in the low bits) interleaved per frame,
        and the marker phase the next frame must carry.
    """
    import numpy as np  # noqa: PLC0415

    channels = dsd.shape[0]
    pairs = dsd.reshape(channels, -1, 2).astype(np.uint32)
    frames = pairs.shape[1]
    markers = np.where(
        (np.arange(frames) + marker_phase) % 2 == 0, DOP_MARKERS[0], DOP_MARKERS[1]
    ).astype(np.uint32)
    words = (markers[None, :] << 16) | (pairs[:, :, 0] << 8) | pairs[:, :, 1]
    return words.T.reshape(-1), (marker_phase + frames) % 2


def dop_words_to_pcm(words: np.ndarray, pcm_format: AudioFormat) -> bytes:
    """
    Encode 24-bit DoP words as raw PCM in the given format, bit-exactly.

    :param words: uint32 array holding the 24-bit DoP words.
    :param pcm_format: The PCM format the stream consumer expects.
    """
    import numpy as np  # noqa: PLC0415

    content_type = pcm_format.content_type
    if content_type == ContentType.PCM_F32LE:
        # every 24-bit integer is exactly representable in float32
        signed = words.astype(np.int32)
        signed[signed >= 1 << 23] -= 1 << 24
        return (signed.astype(np.float32) / float(1 << 23)).astype("<f4").tobytes()
    if content_type == ContentType.PCM_S32LE:
        return (words << 8).astype("<u4").tobytes()
    if content_type == ContentType.PCM_S24LE:
        return words.astype("<u4").view(np.uint8).reshape(-1, 4)[:, :3].tobytes()
    raise DoPError(f"DoP needs a 24-bit or wider PCM format, not {content_type}")


async def get_dop_stream(
    source: str | int,
    pcm_format: AudioFormat,
    seek_position: float = 0,
    marker_state: DopMarkerState | None = None,
) -> AsyncGenerator[bytes]:
    """
    Stream a DSF file as DoP, encoded in the given PCM format.

    :param source: Path to the DSF file, or an open descriptor of it. A descriptor is only
        read with positional reads and is left open for the caller to close.
    :param pcm_format: PCM format of the consumer; its rate must be the DoP rate.
    :param seek_position: Start position in seconds.
    :param marker_state: Marker phase to start from and to leave for the next stream, so
        the markers keep alternating across gapless tracks. A fresh stream starts at 0.
    """
    if isinstance(source, int):
        async for chunk in _get_dop_stream_fd(
            source, str(source), pcm_format, seek_position, marker_state
        ):
            yield chunk
        return
    fd = await asyncio.to_thread(os.open, source, os.O_RDONLY)
    try:
        async for chunk in _get_dop_stream_fd(fd, source, pcm_format, seek_position, marker_state):
            yield chunk
    finally:
        os.close(fd)


async def _get_dop_stream_fd(
    fd: int,
    name: str,
    pcm_format: AudioFormat,
    seek_position: float,
    marker_state: DopMarkerState | None,
) -> AsyncGenerator[bytes]:
    """
    Stream an open DSF file as DoP, see `get_dop_stream`.

    :param fd: Descriptor of the DSF file.
    :param name: Name of the file, for error messages.
    :param pcm_format: PCM format of the consumer; its rate must be the DoP rate.
    :param seek_position: Start position in seconds.
    :param marker_state: Marker phase carried across streams, if any.
    """
    import numpy as np  # noqa: PLC0415

    info = await asyncio.to_thread(read_dsf_info_fd, fd, name)
    if info.channels != 2 or pcm_format.channels != 2:
        raise DoPError("DoP streaming supports stereo DSD only")
    if pcm_format.sample_rate != info.dop_sample_rate:
        raise DoPError(
            f"PCM rate {pcm_format.sample_rate} cannot carry DSD rate {info.dsd_rate} as DoP"
        )
    if pcm_container_bit_depth(pcm_format.bit_depth) < 24:
        raise DoPError(f"DoP needs a 24-bit or wider PCM format, not {pcm_format.bit_depth}")
    channels = info.channels
    block_size = info.block_size
    total_blocks = -(-info.bytes_per_channel // block_size)
    start_block = min(int(seek_position * info.dsd_rate / 8) // block_size, total_blocks)
    if marker_state is None:
        marker_state = DopMarkerState()

    def _read_blocks(first_block: int, count: int) -> bytes:
        return os.pread(
            fd,
            count * block_size * channels,
            info.data_offset + first_block * block_size * channels,
        )

    block = start_block
    while block < total_blocks:
        count = min(_BLOCKS_PER_READ, total_blocks - block)
        raw = await asyncio.to_thread(_read_blocks, block, count)
        whole_blocks = len(raw) // (block_size * channels)
        if whole_blocks == 0:
            break
        dsd = (
            np.frombuffer(raw[: whole_blocks * block_size * channels], dtype=np.uint8)
            .reshape(whole_blocks, channels, block_size)
            .transpose(1, 0, 2)
            .reshape(channels, -1)
        )
        # drop the padding of the last block of the file
        valid = info.bytes_per_channel - block * block_size
        if valid < dsd.shape[1]:
            dsd = dsd[:, :valid]
        if info.lsb_first:
            dsd = _bit_reverse_table()[dsd]
        if dsd.shape[1] % 2:
            dsd = np.concatenate(
                [dsd, np.full((channels, 1), DSD_SILENCE_BYTE, dtype=np.uint8)], axis=1
            )
        if dsd.shape[1]:
            words, marker_state.phase = pack_dop(dsd, marker_state.phase)
            yield dop_words_to_pcm(words, pcm_format)
        block += whole_blocks


@cache
def _bit_reverse_table() -> np.ndarray:
    """Return the table that reverses the bit order of a byte."""
    import numpy as np  # noqa: PLC0415

    return np.array([int(f"{i:08b}"[::-1], 2) for i in range(256)], dtype=np.uint8)
