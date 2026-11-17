"""
FLAC encoder for the stream of a slimproto player that follows a server timeline.

The stream is one endless FLAC file: the marker and the stream info once, with a length
that is not known, then one frame for every block of PCM. A frame is on its way as soon
as its block is complete, so no more than one block of audio is ever held back.

This module does no I/O and knows nothing about the rest of the server.
"""

from __future__ import annotations

import av

# Frames of audio in a FLAC frame: one of the block sizes every decoder takes at every
# sample rate. What does not fill a block yet waits for the next PCM, 26 ms at most at
# 44.1 kHz.
BLOCK_FRAMES = 1152

_MARKER = b"fLaC"
# metadata block types, and the flag of the last block before the audio
_STREAMINFO = 0
_PADDING = 1
_LAST_BLOCK = 0x80


class FlacEncoder:
    """Turns the PCM of one stream into a FLAC stream, block by block."""

    def __init__(self, sample_rate: int, bit_depth: int, channels: int, padding: int = 0) -> None:
        """
        Initialize the encoder of a new stream.

        :param sample_rate: Sample rate of the PCM in Hz.
        :param bit_depth: Bit depth of the audio: 16 for samples of 2 bytes, 24 for samples
            of 4 bytes with the lowest byte unused.
        :param channels: Number of channels of the PCM.
        :param padding: Bytes of padding the stream opens with, after its stream info: a
            player skips them, and holds that much of the stream at once.
        """
        self._format = "s16" if bit_depth == 16 else "s32"
        self._layout = "stereo" if channels == 2 else "mono"
        self._sample_rate = sample_rate
        self._frame_size = channels * (2 if bit_depth == 16 else 4)
        self._context = av.CodecContext.create("flac", "w")
        self._context.sample_rate = sample_rate
        self._context.format = self._format
        self._context.layout = self._layout
        self._context.options = {"frame_size": str(BLOCK_FRAMES)}
        self._context.open()
        self._pending = bytearray()
        self._frames_encoded = 0
        stream_info = self._context.extradata
        assert stream_info is not None  # the encoder is open
        self.header = (
            _MARKER
            + _block_header(_STREAMINFO, len(stream_info))
            + stream_info
            + _block_header(_PADDING | _LAST_BLOCK, padding)
            + bytes(padding)
        )

    @property
    def frames_held(self) -> int:
        """Return the number of frames of PCM that wait for the rest of their block."""
        return len(self._pending) // self._frame_size

    def encode(self, pcm: bytes) -> bytes:
        """
        Add PCM to the stream and return the FLAC frames that are complete with it.

        :param pcm: The next PCM of the stream, a whole number of frames.
        :return: The frames, nothing while the PCM does not complete a block.
        """
        pending = self._pending
        pending += pcm
        block_size = BLOCK_FRAMES * self._frame_size
        parts: list[bytes] = []
        for start in range(0, len(pending) - block_size + 1, block_size):
            frame = av.AudioFrame(format=self._format, layout=self._layout, samples=BLOCK_FRAMES)
            frame.planes[0].update(bytes(pending[start : start + block_size]))
            frame.sample_rate = self._sample_rate
            frame.pts = self._frames_encoded
            self._frames_encoded += BLOCK_FRAMES
            parts.extend(bytes(packet) for packet in self._context.encode(frame))
        del pending[: len(parts) * block_size]
        return b"".join(parts)


def _block_header(block_type: int, length: int) -> bytes:
    """
    Return the header of a FLAC metadata block.

    :param block_type: The type of the block, with the flag of the last block when it is.
    :param length: The length of the block in bytes.
    """
    return bytes([block_type]) + length.to_bytes(3, "big")
