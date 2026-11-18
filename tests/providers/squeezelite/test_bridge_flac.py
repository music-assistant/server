"""
Tests for the FLAC encoder of the Sendspin bridge of the squeezelite provider.

What the encoder makes is read back by a FLAC decoder, as a player does, and its frames
are held against the checksum of the format.
"""

from __future__ import annotations

import io
import random
import struct
from unittest.mock import patch

import av
import pytest

from music_assistant.providers.squeezelite import bridge_flac
from music_assistant.providers.squeezelite.bridge_flac import (
    BLOCK_FRAMES,
    HELD_BACK_BYTES,
    FlacEncoder,
)

# a block of 16-bit stereo PCM, in bytes
BLOCK = BLOCK_FRAMES * 4


def _noise(frames: int, bit_depth: int, seed: int = 1, level: int = 15) -> bytes:
    """
    Return stereo PCM of random samples.

    :param frames: The number of frames.
    :param bit_depth: 16 for samples of 2 bytes, 24 for samples of 4 bytes with the lowest
        byte unused.
    :param seed: The seed of the samples.
    :param level: The number of bits the samples take: all of them leave an encoder
        nothing to gain, a few of them most of it.
    """
    rng = random.Random(seed)  # noqa: S311
    values = [rng.randrange(-(1 << level), 1 << level) for _ in range(2 * frames)]
    if bit_depth == 16:
        return struct.pack(f"<{2 * frames}h", *values)
    return struct.pack(f"<{2 * frames}i", *(value << 16 for value in values))


def _level(frames: int, value: int) -> bytes:
    """Return 16-bit stereo PCM of one and the same sample, which is no silence."""
    return struct.pack("<h", value) * (2 * frames)


def _crc16(data: bytes, crc: int = 0) -> int:
    """
    Return the checksum the format has for a frame, taken a bit at a time.

    :param data: The frame, or the next part of it.
    :param crc: The checksum of the parts before it.
    """
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = (crc << 1 ^ 0x8005 if crc & 0x8000 else crc << 1) & 0xFFFF
    return crc


def decode_flac(stream: bytes) -> tuple[bytes, int, int]:
    """
    Return what a decoder makes of a FLAC stream that is still going on.

    :param stream: The stream from its first byte.
    :return: The PCM as the encoder took it, its sample rate and its bit depth.
    """
    with av.open(io.BytesIO(stream), mode="r", format="flac") as container:
        audio = container.streams.audio[0]
        bit_depth = audio.codec_context.bits_per_raw_sample or audio.format.bits
        frame_size = audio.codec_context.channels * audio.format.bytes
        pcm = b"".join(
            bytes(frame.planes[0])[: frame.samples * frame_size]
            for frame in container.decode(audio)
        )
        return pcm, audio.codec_context.sample_rate, bit_depth


@pytest.mark.parametrize(("sample_rate", "bit_depth"), [(44100, 16), (48000, 24), (96000, 24)])
def test_a_stream_decodes_to_the_pcm_it_was_made_of(sample_rate: int, bit_depth: int) -> None:
    """Every sample comes back as it went in: music, silence and what filled no block."""
    encoder = FlacEncoder(sample_rate, bit_depth, 2)
    frame_size = 4 if bit_depth == 16 else 8
    pcm = (
        bytes(3 * BLOCK_FRAMES * frame_size)
        + _noise(4 * BLOCK_FRAMES + 300, bit_depth)
        + bytes(140 * BLOCK_FRAMES * frame_size)
        + _noise(2 * BLOCK_FRAMES, bit_depth, seed=2, level=3)
        + bytes(77 * frame_size)
    )
    rng = random.Random(2)  # noqa: S311
    stream, at = encoder.header, 0
    while at < len(pcm):
        size = rng.choice((1, 441, 882, BLOCK_FRAMES, 22050)) * frame_size
        stream += encoder.encode(pcm[at : at + size])
        at += size
    stream += encoder.flush()

    decoded, decoded_rate, decoded_depth = decode_flac(stream)

    assert (decoded_rate, decoded_depth) == (sample_rate, bit_depth)
    assert decoded == pcm


def test_a_frame_goes_out_as_soon_as_its_block_is_complete() -> None:
    """No more than a block of audio waits in the encoder."""
    encoder = FlacEncoder(44100, 16, 2)
    pcm = _noise(3 * BLOCK_FRAMES, 16)

    assert encoder.encode(pcm[: BLOCK - 4]) == b""
    assert encoder.frames_held == BLOCK_FRAMES - 1
    first = encoder.encode(pcm[BLOCK - 4 : BLOCK])
    assert first
    rest = encoder.encode(pcm[BLOCK:])

    assert decode_flac(encoder.header + first)[0] == pcm[:BLOCK]
    assert decode_flac(encoder.header + first + rest)[0] == pcm


def test_the_stream_opens_with_its_marker_and_its_stream_info() -> None:
    """The header says what the stream is and not how long."""
    header = FlacEncoder(48000, 24, 2).header

    assert header[:4] == b"fLaC"
    # the stream info: 34 bytes, the last block before the audio
    assert header[4:8] == bytes([0x80, 0, 0, 34])
    assert len(header) == 42
    info = header[8:]
    assert (int.from_bytes(info[0:2], "big"), int.from_bytes(info[2:4], "big")) == (
        BLOCK_FRAMES,
        BLOCK_FRAMES,
    )
    packed = int.from_bytes(info[10:18], "big")
    assert packed >> 44 == 48000
    assert (packed >> 41 & 0x7) + 1 == 2
    assert (packed >> 36 & 0x1F) + 1 == 24
    # the number of samples of the stream is not known
    assert packed & (2**36 - 1) == 0


@pytest.mark.parametrize(
    ("block", "in_full"),
    [
        # what an encoder makes next to nothing of holds its samples as they are
        (bytes(BLOCK), True),
        (_level(BLOCK_FRAMES, 77), True),
        # what it gets less out of stays as it made it
        (_noise(BLOCK_FRAMES, 16, level=3), False),
        (_noise(BLOCK_FRAMES, 16, level=10), False),
    ],
    ids=["silence", "one level", "quiet noise", "noise"],
)
def test_a_frame_that_compresses_to_next_to_nothing_goes_out_in_full(
    block: bytes, in_full: bool
) -> None:
    """Silence is a frame of the size of its PCM: a player holds enough of it to decode it."""
    encoder = FlacEncoder(44100, 16, 2)

    frame = encoder.encode(block)

    # its samples, with the header and the checksum of the frame and a byte per channel
    assert (len(frame) == BLOCK + 10) is in_full
    assert BLOCK // 8 <= len(frame) <= BLOCK + 10
    assert decode_flac(encoder.header + frame)[0] == block


@pytest.mark.parametrize("bit_depth", [16, 24])
def test_every_frame_has_the_checksum_of_the_format(bit_depth: int) -> None:
    """A frame the encoder did not make itself is as valid as one it did, whatever its number."""
    encoder = FlacEncoder(44100, bit_depth, 2)
    frame_size = 4 if bit_depth == 16 else 8
    silence = bytes(BLOCK_FRAMES * frame_size)
    level = (b"\x00\x4d" if bit_depth == 16 else b"\x00\x00\x4d\x00") * (2 * BLOCK_FRAMES)
    blocks = [silence, _noise(BLOCK_FRAMES, bit_depth), level] * 50

    frames = [encoder.encode(block) for block in blocks]

    assert all(frame[-2:] == _crc16(frame[:-2]).to_bytes(2, "big") for frame in frames)
    # frames from number 128 on have a longer header
    assert decode_flac(encoder.header + b"".join(frames))[0] == b"".join(blocks)


def test_the_checksum_is_right_for_every_byte_at_every_place() -> None:
    """The checksum of a long frame comes from a table of what a byte adds at its place in a row of them."""
    row = bridge_flac._CRC16_ROW
    for byte in range(256):
        # the byte with ever more bytes after it: the last place of a row first
        expected = _crc16(bytes([byte]))
        for after in range(row):
            data = bytes(row - 1 - after) + bytes([byte]) + bytes(after)
            assert bridge_flac._crc16(data) == expected, (byte, after)
            expected = _crc16(b"\x00", expected)


def test_the_checksum_carries_on_from_the_bytes_before() -> None:
    """Any run of bytes after any checksum: what fills no row, rows, and both."""
    rng = random.Random(4)  # noqa: S311
    row = bridge_flac._CRC16_ROW
    for length in (0, 1, row - 1, row, row + 1, 3 * row, 5 * row + 77, 4610, 6914):
        for _ in range(8):
            data, before = rng.randbytes(length), rng.randrange(1 << 16)
            assert bridge_flac._crc16(data, before) == _crc16(data, before), (length, before)


@pytest.mark.parametrize("bit_depth", [16, 24])
def test_a_frame_in_full_is_not_checked_a_byte_at_a_time(bit_depth: int) -> None:
    """Quiet audio that is no silence keeps the event loop no longer than audio that compresses less."""
    encoder = FlacEncoder(44100, bit_depth, 2)
    level = (b"\x00\x4d" if bit_depth == 16 else b"\x00\x00\x4d\x00") * (2 * BLOCK_FRAMES)
    table = bridge_flac._CRC16_TABLE
    looked_up = 0

    class _Table:
        """Counts the bytes that are looked up one by one."""

        def __getitem__(self, index: int) -> int:
            nonlocal looked_up
            looked_up += 1
            return table[index]

    with patch.object(bridge_flac, "_CRC16_TABLE", _Table()):
        frame = encoder.encode(level)

    assert len(frame) == BLOCK_FRAMES * 2 * bit_depth // 8 + 10
    assert frame[-2:] == _crc16(frame[:-2]).to_bytes(2, "big")
    # the header of the frame, and the two bytes of its samples that fill no row
    assert looked_up == 8


def test_a_24_bit_frame_is_held_against_the_size_of_its_samples() -> None:
    """A sample of 24 bits comes in 4 bytes: a frame goes out in full under an eighth of the 3 that count."""
    encoder = FlacEncoder(44100, 24, 2)
    block = _noise(BLOCK_FRAMES, 24, level=1)

    frame = encoder.encode(block)

    # over an eighth of its samples, and under an eighth of the bytes they came in
    assert BLOCK_FRAMES * 6 // 8 <= len(frame) < BLOCK_FRAMES * 8 // 8
    assert decode_flac(encoder.header + frame)[0] == block


def test_what_a_player_can_not_decode_yet_is_known() -> None:
    """A player decodes a frame once more follows from it on than its decoder waits for."""
    encoder = FlacEncoder(44100, 16, 2)
    rng = random.Random(3)  # noqa: S311
    kinds = [bytes(BLOCK), _noise(BLOCK_FRAMES, 16, level=10), _noise(BLOCK_FRAMES, 16, level=3)]
    sizes: list[int] = []
    held: list[int] = []
    expected: list[int] = []

    for _ in range(200):
        sizes.append(len(encoder.encode(rng.choice(kinds))))
        held.append(encoder.frames_held)
        # the frames at the end that are no more than that together
        count = 0
        while count < len(sizes) and sum(sizes[len(sizes) - count - 1 :]) <= HELD_BACK_BYTES:
            count += 1
        expected.append(count * BLOCK_FRAMES)

    assert held == expected
    # between 7 frames of silence and 57 of the smallest ones there are
    assert min(held[60:]) >= 7 * BLOCK_FRAMES
    assert max(held) <= 57 * BLOCK_FRAMES
    # with what waits for the rest of its block
    encoder.encode(bytes(400))
    assert encoder.frames_held == held[-1] + 100


@pytest.mark.parametrize(
    ("sample_rate", "bit_depth", "held_ms"), [(44100, 16, 208), (48000, 24, 120), (96000, 24, 60)]
)
def test_silence_leaves_a_player_with_a_fifth_of_a_second_at_most(
    sample_rate: int, bit_depth: int, held_ms: int
) -> None:
    """What a player can not decode of silence is known before any of it is made."""
    encoder = FlacEncoder(sample_rate, bit_depth, 2)
    frame_size = 4 if bit_depth == 16 else 8
    assert encoder.silence_held * 1000 // sample_rate == held_ms

    held = []
    for _ in range(300):
        encoder.encode(bytes(441 * frame_size))
        held.append(encoder.frames_held)

    assert max(held) <= encoder.silence_held
    assert max(held) > encoder.silence_held - 441


def test_the_last_frame_holds_what_filled_no_block() -> None:
    """A stream that ends leaves nothing behind in its encoder."""
    encoder = FlacEncoder(44100, 16, 2)
    pcm = _noise(BLOCK_FRAMES + 300, 16)
    stream = encoder.header + encoder.encode(pcm)
    assert decode_flac(stream)[0] == pcm[:BLOCK]

    stream += encoder.flush()

    assert decode_flac(stream)[0] == pcm
    assert encoder.flush() == b""
