"""
Tests for the FLAC encoder of the Sendspin bridge of the squeezelite provider.

What the encoder makes is read back by a FLAC decoder, as a player does.
"""

from __future__ import annotations

import io
import random
import struct

import av
import pytest

from music_assistant.providers.squeezelite.bridge_flac import BLOCK_FRAMES, FlacEncoder

PADDING = 16 * 1024


def _noise(frames: int, bit_depth: int, seed: int = 1) -> bytes:
    """
    Return stereo PCM of random samples, which no encoder can predict.

    :param frames: The number of frames.
    :param bit_depth: 16 for samples of 2 bytes, 24 for samples of 4 bytes with the lowest
        byte unused.
    :param seed: The seed of the samples.
    """
    rng = random.Random(seed)  # noqa: S311
    if bit_depth == 16:
        return struct.pack(
            f"<{2 * frames}h", *(rng.randrange(-32768, 32768) for _ in range(2 * frames))
        )
    samples = (rng.randrange(-(2**23), 2**23) << 8 for _ in range(2 * frames))
    return struct.pack(f"<{2 * frames}i", *samples)


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
    """Every sample comes back as it went in, in whatever pieces the PCM came."""
    encoder = FlacEncoder(sample_rate, bit_depth, 2, PADDING)
    frame_size = 4 if bit_depth == 16 else 8
    pcm = _noise(7 * BLOCK_FRAMES + 300, bit_depth)
    rng = random.Random(2)  # noqa: S311
    stream, at = encoder.header, 0
    while at < len(pcm):
        size = rng.choice((1, 441, 882, BLOCK_FRAMES, 22050)) * frame_size
        stream += encoder.encode(pcm[at : at + size])
        at += size

    decoded, decoded_rate, decoded_depth = decode_flac(stream)

    assert (decoded_rate, decoded_depth) == (sample_rate, bit_depth)
    assert decoded == pcm[: 7 * BLOCK_FRAMES * frame_size]
    assert encoder.frames_held == 300


def test_a_frame_goes_out_as_soon_as_its_block_is_complete() -> None:
    """No more than a block of audio waits in the encoder, and what waits is known."""
    encoder = FlacEncoder(44100, 16, 2)
    pcm = _noise(3 * BLOCK_FRAMES, 16)

    assert encoder.encode(pcm[: (BLOCK_FRAMES - 1) * 4]) == b""
    assert encoder.frames_held == BLOCK_FRAMES - 1
    first = encoder.encode(pcm[(BLOCK_FRAMES - 1) * 4 : BLOCK_FRAMES * 4])
    assert first
    assert encoder.frames_held == 0
    rest = encoder.encode(pcm[BLOCK_FRAMES * 4 :])
    assert encoder.frames_held == 0

    assert decode_flac(encoder.header + first)[0] == pcm[: BLOCK_FRAMES * 4]
    assert decode_flac(encoder.header + first + rest)[0] == pcm


def test_the_stream_opens_with_its_marker_its_stream_info_and_padding() -> None:
    """The header says what the stream is and not how long, and holds the padding asked for."""
    header = FlacEncoder(48000, 24, 2, PADDING).header

    assert header[:4] == b"fLaC"
    # the stream info: 34 bytes, not the last block
    assert header[4:8] == bytes([0, 0, 0, 34])
    info = header[8:42]
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
    # the padding: the last block before the audio
    assert header[42:46] == bytes([0x81]) + PADDING.to_bytes(3, "big")
    assert header[46:] == bytes(PADDING)


def test_silence_takes_next_to_nothing() -> None:
    """A quarter of a second of silence is a few hundred bytes: no player buffers on that."""
    encoder = FlacEncoder(44100, 16, 2)

    silence = encoder.encode(bytes(11025 * 4))

    assert 0 < len(silence) < 400
    assert encoder.frames_held == 11025 % BLOCK_FRAMES
