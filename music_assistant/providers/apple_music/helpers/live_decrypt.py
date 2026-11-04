"""In-memory decryption of Apple Music live radio segments, for playback only."""

from __future__ import annotations

import struct
from dataclasses import dataclass

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

_CONTAINER_BOXES = frozenset(
    {b"moov", b"trak", b"mdia", b"minf", b"stbl", b"sinf", b"schi", b"mvex", b"moof", b"traf"}
)
_BLOCK_SIZE = 16


class UnsupportedEncryptionError(ValueError):
    """The stream uses an encryption layout the decryption does not handle."""


@dataclass(slots=True, frozen=True)
class TrackEncryption:
    """The encryption settings an init segment declares."""

    key_id: str
    constant_iv: bytes
    default_sample_size: int
    audio_config: bytes


def parse_init_segment(init: bytes) -> TrackEncryption:
    """
    Return the encryption settings of an init segment.

    :param init: The bytes of the init segment.
    :raises UnsupportedEncryptionError: If it is not the expected cbcs layout.
    """
    tenc: bytes | None = None
    scheme: bytes | None = None
    audio_config = b""
    default_sample_size = 0
    for name, start, end in _walk(init, 0, len(init)):
        if name == b"tenc":
            tenc = init[start:end]
        elif name == b"schm":
            scheme = init[start + 4 : start + 8]
        elif name == b"trex":
            default_sample_size = struct.unpack(">I", init[start + 16 : start + 20])[0]
        elif name == b"stsd":
            # the sample entry holds the codec setup, before its protection scheme box
            audio_config = _audio_config(init[start:end])
    if tenc is None or scheme != b"cbcs":
        raise UnsupportedEncryptionError(f"Expected a cbcs stream, found {scheme!r}")
    # version and flags (4), reserved (1), pattern (1), protected (1), iv size (1), key id (16)
    crypt_skip = tenc[5]
    per_sample_iv_size = tenc[7]
    if crypt_skip != 0 or per_sample_iv_size != 0:
        raise UnsupportedEncryptionError(
            f"Unsupported pattern {crypt_skip} or per sample iv size {per_sample_iv_size}"
        )
    iv_size = tenc[24]
    return TrackEncryption(
        key_id=tenc[8:24].hex(),
        constant_iv=tenc[25 : 25 + iv_size].ljust(_BLOCK_SIZE, b"\0"),
        default_sample_size=default_sample_size,
        audio_config=audio_config,
    )


def decrypt_segment(segment: bytes, key: bytes, encryption: TrackEncryption) -> bytes:
    """
    Return a media segment with its audio samples decrypted in place of the encrypted ones.

    :param segment: The bytes of the media segment.
    :param key: The content key from the license of this account.
    :param encryption: The settings of the segment's init segment.
    """
    output = bytearray(segment)
    position = 0
    while position + 8 <= len(segment):
        size, box = struct.unpack(">I4s", segment[position : position + 8])
        if size < 8:
            break
        if box == b"moof":
            _decrypt_fragment(segment, output, position, position + size, key, encryption)
        position += size
    return bytes(output)


def _decrypt_fragment(
    segment: bytes,
    output: bytearray,
    moof_start: int,
    moof_end: int,
    key: bytes,
    encryption: TrackEncryption,
) -> None:
    """Decrypt the samples of the fragment that one moof box describes."""
    base_offset = moof_start
    default_size = encryption.default_sample_size
    subsamples: list[list[tuple[int, int]]] = []
    sample_ranges: list[tuple[int, int]] = []
    next_offset: int | None = None
    for name, start, end in _walk(segment, moof_start + 8, moof_end):
        if name == b"tfhd":
            base_offset, default_size = _parse_tfhd(segment, start, moof_start, default_size)
        elif name == b"senc":
            subsamples = _parse_senc(segment, start)
        elif name == b"trun":  # codespell:ignore trun
            next_offset = _parse_trun(
                segment, start, end, base_offset, default_size, next_offset, sample_ranges
            )
    for index, (offset, size) in enumerate(sample_ranges):
        layout = subsamples[index] if index < len(subsamples) else []
        for part_start, part_size in _encrypted_parts(offset, size, layout):
            whole = part_size - part_size % _BLOCK_SIZE
            if not whole:
                continue
            decryptor = Cipher(algorithms.AES(key), modes.CBC(encryption.constant_iv)).decryptor()
            output[part_start : part_start + whole] = decryptor.update(
                segment[part_start : part_start + whole]
            )


def _parse_tfhd(segment: bytes, start: int, moof_start: int, default_size: int) -> tuple[int, int]:
    """Return the base data offset and default sample size of a track fragment."""
    flags = int.from_bytes(segment[start + 1 : start + 4])
    position = start + 8
    base_offset = moof_start
    if flags & 0x1:
        base_offset = struct.unpack(">Q", segment[position : position + 8])[0]
        position += 8
    if flags & 0x2:
        position += 4
    if flags & 0x8:
        position += 4
    if flags & 0x10:
        default_size = struct.unpack(">I", segment[position : position + 4])[0]
    return base_offset, default_size


def _parse_trun(
    segment: bytes,
    start: int,
    end: int,
    base_offset: int,
    default_size: int,
    previous_end: int | None,
    sample_ranges: list[tuple[int, int]],
) -> int:
    """Add the sample positions of a track run, returning where the run ends."""
    flags = int.from_bytes(segment[start + 1 : start + 4])
    count = struct.unpack(">I", segment[start + 4 : start + 8])[0]
    position = start + 8
    offset = previous_end if previous_end is not None else base_offset
    if flags & 0x1:
        offset = base_offset + struct.unpack(">i", segment[position : position + 4])[0]
        position += 4
    if flags & 0x4:
        position += 4
    for _ in range(count):
        if position > end:
            raise UnsupportedEncryptionError("Track run is shorter than its sample count")
        if flags & 0x100:
            position += 4
        size = default_size
        if flags & 0x200:
            size = struct.unpack(">I", segment[position : position + 4])[0]
            position += 4
        if flags & 0x400:
            position += 4
        if flags & 0x800:
            position += 4
        sample_ranges.append((offset, size))
        offset += size
    return offset


def _parse_senc(segment: bytes, start: int) -> list[list[tuple[int, int]]]:
    """Return the clear and encrypted byte counts of each sample, if the box lists them."""
    flags = int.from_bytes(segment[start + 1 : start + 4])
    if not flags & 0x2:
        return []
    count = struct.unpack(">I", segment[start + 4 : start + 8])[0]
    position = start + 8
    samples: list[list[tuple[int, int]]] = []
    for _ in range(count):
        parts = struct.unpack(">H", segment[position : position + 2])[0]
        position += 2
        layout: list[tuple[int, int]] = []
        for _ in range(parts):
            clear, encrypted = struct.unpack(">HI", segment[position : position + 6])
            layout.append((clear, encrypted))
            position += 6
        samples.append(layout)
    return samples


def _encrypted_parts(
    offset: int, size: int, layout: list[tuple[int, int]]
) -> list[tuple[int, int]]:
    """Return the encrypted byte ranges of a sample, each starting over from the iv."""
    if not layout:
        return [(offset, size)]
    parts: list[tuple[int, int]] = []
    position = offset
    for clear, encrypted in layout:
        position += clear
        parts.append((position, encrypted))
        position += encrypted
    return parts


def _audio_config(stsd: bytes) -> bytes:
    """Return the codec setup of a sample description, without the protection scheme."""
    marker = stsd.find(b"esds")
    if marker < 4:
        return b""
    size = struct.unpack(">I", stsd[marker - 4 : marker])[0]
    return stsd[marker - 4 : marker - 4 + size]


def _walk(data: bytes, start: int, end: int) -> list[tuple[bytes, int, int]]:
    """Return the boxes in a range and inside its containers, as (name, payload start, end)."""
    found: list[tuple[bytes, int, int]] = []
    position = start
    while position + 8 <= end:
        size, name = struct.unpack(">I4s", data[position : position + 8])
        if size < 8 or position + size > end:
            break
        payload = position + 8
        found.append((name, payload, position + size))
        if name in _CONTAINER_BOXES:
            found.extend(_walk(data, payload, position + size))
        elif name == b"stsd":
            # version and flags (4), entry count (4), then the sample entries
            found.extend(_walk(data, payload + 8, position + size))
        elif name == b"enca":
            # the fields of an audio sample entry (28) come before its boxes
            found.extend(_walk(data, payload + 28, position + size))
        position += size
    return found
