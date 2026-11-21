"""
FLAC encoder for the stream of a slimproto player that follows a server timeline.

The stream is one endless FLAC file: the marker and the stream info once, with a length
that is not known, then one frame for every block of PCM. A frame is on its way as soon
as its block is complete.

A player runs its decoder only while it holds a fair amount of the stream beyond the
frame at hand, so the end of what it got stays undecoded until more follows. The encoder
tells how many frames that is, and keeps it short: a frame that compresses to next to
nothing, as silence does, goes out with its samples in full.

This module does no I/O and knows nothing about the rest of the server.
"""

from __future__ import annotations

from collections import deque
from functools import cache

import av
import numpy as np

# Frames of audio in a FLAC frame: one of the block sizes every decoder takes at every
# sample rate. What does not fill a block yet waits for the next PCM, 26 ms at most at
# 44.1 kHz.
BLOCK_FRAMES = 1152
# Bytes at the end of the stream a player may leave undecoded: the 25000 a player waits
# for in its stream buffer before it runs its decoder (16384 on other players), and the
# 8192 a decoder reads ahead of the frame it decodes.
HELD_BACK_BYTES = 25_000 + 8_192
# A frame is at least this part of the samples it holds: one that compresses further goes
# out with its samples in full. That bounds the audio in the bytes above, which is half a
# minute of compressed silence: a second and a half at most, and 0.2 seconds of silence.
MIN_FRAME_PART = 1 / 8

_MARKER = b"fLaC"
# the stream info is the only metadata block, which makes it the last one
_STREAMINFO_HEADER = b"\x80\x00\x00\x22"
# the header of a subframe that holds its samples as they are
_VERBATIM = b"\x02"


class FlacEncoder:
    """Turns the PCM of one stream into a FLAC stream, block by block."""

    def __init__(self, sample_rate: int, bit_depth: int, channels: int) -> None:
        """
        Initialize the encoder of a new stream.

        :param sample_rate: Sample rate of the PCM in Hz.
        :param bit_depth: Bit depth of the audio: 16 for samples of 2 bytes, 24 for samples
            of 4 bytes with the lowest byte unused.
        :param channels: Number of channels of the PCM.
        """
        self._format = "s16" if bit_depth == 16 else "s32"
        self._layout = "stereo" if channels == 2 else "mono"
        self._sample_rate = sample_rate
        self._channels = channels
        # bytes of a sample in the PCM, and the ones of them that hold its bits
        self._sample_size = 2 if bit_depth == 16 else 4
        self._sample_bytes = bit_depth // 8
        self._block_size = BLOCK_FRAMES * channels * self._sample_size
        # the smallest frame that goes out as the encoder made it
        self._min_frame_size = BLOCK_FRAMES * channels * self._sample_bytes * MIN_FRAME_PART
        self._context = av.CodecContext.create("flac", "w")
        self._context.sample_rate = sample_rate
        self._context.format = self._format
        self._context.layout = self._layout
        self._context.options = {"frame_size": str(BLOCK_FRAMES)}
        if bit_depth == 24:
            self._context.bits_per_raw_sample = 24
        self._context.open()
        stream_info = self._context.extradata
        assert stream_info is not None  # the encoder is open
        self.header = _MARKER + _STREAMINFO_HEADER + stream_info
        self._silence = bytes(self._block_size)
        self._silent_subframes = self._subframes(self._silence)
        self._pending = bytearray()
        self._blocks = 0
        # sizes of the frames at the end of the stream that a player can not decode yet
        self._held: deque[int] = deque()
        self._held_bytes = 0

    @property
    def silence_held(self) -> int:
        """Return the most frames of silence a player leaves undecoded at the end of the stream."""
        # the smallest a frame of silence is: its header, its subframes and its checksum
        frame_size = 6 + len(self._silent_subframes) + 2
        # the frames that many bytes hold, and what waits for the rest of its block
        return BLOCK_FRAMES * (HELD_BACK_BYTES // frame_size + 1)

    @property
    def frames_held(self) -> int:
        """
        Return the number of frames of the PCM it took that a player can not decode yet.

        Those are the frames that wait for the rest of their block, and the ones a player
        leaves undecoded until more of the stream follows them.
        """
        pending = len(self._pending) // (self._channels * self._sample_size)
        return pending + len(self._held) * BLOCK_FRAMES

    def encode(self, pcm: bytes) -> bytes:
        """
        Add PCM to the stream and return the FLAC frames that are complete with it.

        :param pcm: The next PCM of the stream, a whole number of frames.
        :return: The frames, nothing while the PCM does not complete a block.
        """
        pending = self._pending
        pending += pcm
        block_size = self._block_size
        parts = [
            self._encode_block(bytes(pending[start : start + block_size]))
            for start in range(0, len(pending) - block_size + 1, block_size)
        ]
        del pending[: len(parts) * block_size]
        return b"".join(parts)

    def flush(self) -> bytes:
        """Return the last frame of the stream, which ends with it: the PCM that filled no block."""
        if not self._pending:
            return b""
        frame = self._frame(bytes(self._pending))
        self._pending.clear()
        # a frame of less than a block only comes out as the last one
        packets = (*self._context.encode(frame), *self._context.encode(None))
        return b"".join(bytes(packet) for packet in packets)

    def _frame(self, pcm: bytes) -> av.AudioFrame:
        """
        Return PCM as a frame for the encoder.

        :param pcm: The PCM of a block, or of less for the last one of a stream.
        """
        frames = len(pcm) // (self._channels * self._sample_size)
        frame = av.AudioFrame(format=self._format, layout=self._layout, samples=frames)
        frame.planes[0].update(pcm)
        frame.sample_rate = self._sample_rate
        frame.pts = self._blocks * BLOCK_FRAMES
        return frame

    def _encode_block(self, block: bytes) -> bytes:
        """
        Return the FLAC frame of a block of PCM.

        :param block: The PCM of the block.
        """
        (packet,) = self._context.encode(self._frame(block))
        self._blocks += 1
        data = bytes(packet)
        if len(data) < self._min_frame_size:
            data = self._plain_frame(data, block)
        held = self._held
        held.append(len(data))
        self._held_bytes += len(data)
        # a player decodes a frame once it holds more than this from that frame on
        while self._held_bytes > HELD_BACK_BYTES:
            self._held_bytes -= held.popleft()
        return data

    def _plain_frame(self, frame: bytes, block: bytes) -> bytes:
        """
        Return the frame of a block with its samples in full.

        :param frame: The frame the encoder made of the block.
        :param block: The PCM of the block.
        """
        # its frame header as the encoder made it: what the frame holds and its number
        size = 4 + _coded_number_size(frame[4])
        size += {6: 1, 7: 2}.get(frame[2] >> 4, 0) + {12: 1, 13: 2, 14: 2}.get(frame[2] & 0x0F, 0)
        # with every channel on its own, where the encoder may code one against another
        header = frame[:3] + bytes([(self._channels - 1) << 4 | frame[3] & 0x0F]) + frame[4:size]
        header += bytes([_crc8(header)])
        if block != self._silence:
            subframes = self._subframes(block)
            return header + subframes + _crc16(subframes, _crc16(header)).to_bytes(2, "big")
        # silence is most of what comes here: its checksum is that of the header, carried
        # over subframes that are the same every time
        subframes = self._silent_subframes
        carry, crc = _crc16_over(subframes)
        crc ^= _xor_of(carry, _crc16(header))
        return header + subframes + crc.to_bytes(2, "big")

    def _subframes(self, block: bytes) -> bytes:
        """
        Return the subframes of a frame that holds the samples of a block as they are.

        :param block: The PCM of the block.
        """
        step = self._channels * self._sample_size
        parts: list[bytes] = []
        for channel in range(self._channels):
            samples = bytearray(BLOCK_FRAMES * self._sample_bytes)
            last = channel * self._sample_size + self._sample_size - 1
            # a sample goes out with its highest byte first
            for byte in range(self._sample_bytes):
                samples[byte :: self._sample_bytes] = block[last - byte :: step]
            parts += (_VERBATIM, bytes(samples))
        return b"".join(parts)


def _coded_number_size(first: int) -> int:
    """
    Return the number of bytes of the number of a frame, which is coded as UTF-8 is.

    :param first: The first byte of the number.
    """
    return max(1, 8 - (first ^ 0xFF).bit_length())


def _crc8(data: bytes) -> int:
    """
    Return the checksum of a frame header.

    :param data: The header, without its checksum.
    """
    crc = 0
    for byte in data:
        crc = _CRC8_TABLE[crc ^ byte]
    return crc


def _crc16(data: bytes, crc: int = 0) -> int:
    """
    Return the checksum of a frame.

    :param data: The frame, or the next part of it.
    :param crc: The checksum of the parts before it.
    """
    # what fills no row goes first, a byte at a time: all of a frame header
    head = len(data) % _CRC16_ROW
    for byte in data[:head]:
        crc = (crc << 8) & 0xFFFF ^ _CRC16_TABLE[crc >> 8 ^ byte]
    if head == len(data):
        return crc
    # The checksum of a row is the exclusive or of what each of its bytes adds at its
    # place, and the checksum before a row counts as two bytes more ahead of it.
    rows = np.frombuffer(data, np.uint8, offset=head).reshape(-1, _CRC16_ROW)
    added = np.bitwise_xor.reduce(_CRC16_AT.take(rows + _CRC16_PLACES), axis=1)
    for row in added.tolist():
        crc = _CRC16_HIGH[crc >> 8] ^ _CRC16_LOW[crc & 0xFF] ^ row
    return crc


@cache
def _crc16_over(data: bytes) -> tuple[tuple[int, ...], int]:
    """
    Return what it takes to carry a checksum over data that follows what it is of.

    :param data: The data that follows.
    :return: What every bit of the checksum before the data becomes after it, and the
        checksum of the data itself: the checksum of both is those of the bits that are
        set and that of the data, combined with exclusive or.
    """
    zeros = bytes(len(data))
    return tuple(_crc16(zeros, 1 << bit) for bit in range(16)), _crc16(data)


def _xor_of(values: tuple[int, ...], bits: int) -> int:
    """
    Return the exclusive or of the values of the bits that are set.

    :param values: A value for every bit, the lowest first.
    :param bits: The bits.
    """
    result = 0
    for value in values:
        if bits & 1:
            result ^= value
        bits >>= 1
    return result


def _crc_table(polynomial: int, bits: int) -> tuple[int, ...]:
    """
    Return the table of a checksum that takes a byte at a time, the highest bit first.

    :param polynomial: The polynomial of the checksum.
    :param bits: The size of the checksum in bits.
    """
    top = 1 << (bits - 1)
    mask = (1 << bits) - 1
    table = []
    for byte in range(256):
        crc = byte << (bits - 8)
        for _ in range(8):
            crc = (crc << 1 ^ polynomial if crc & top else crc << 1) & mask
        table.append(crc)
    return tuple(table)


def _crc16_places(row: int) -> np.ndarray:
    """
    Return what a byte adds to the checksum of a row of bytes, for every place in the row.

    :param row: The number of bytes of a row.
    :return: A row of the table per place, the first place first, with a value per byte.
    """
    table = np.array(_CRC16_TABLE, np.uint16)
    # from the last place back: a byte with one byte more after it
    places = [table]
    for _ in range(row - 1):
        last = places[-1]
        places.append(last << 8 ^ table[last >> 8])
    return np.stack(places[::-1])


_CRC8_TABLE = _crc_table(0x07, 8)
_CRC16_TABLE = _crc_table(0x8005, 16)
# The checksum of a frame with its samples in full is taken a row of bytes at a time: a
# byte at a time takes ten times as long as the encoder takes over the block.
_CRC16_ROW = 128
_CRC16_AT = _crc16_places(_CRC16_ROW)
# where the values of each place of a row start in that table
_CRC16_PLACES = np.arange(_CRC16_ROW, dtype=np.uint16) * 256
# the high and the low byte of a checksum, as the two bytes ahead of the row after it
_CRC16_HIGH: list[int] = _CRC16_AT[0].tolist()
_CRC16_LOW: list[int] = _CRC16_AT[1].tolist()
