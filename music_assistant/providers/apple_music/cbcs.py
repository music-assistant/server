"""
Prepare the cbcs encrypted segments of a live station for ffmpeg.

Some stations describe each sample as one subsample that is encrypted as a whole. ffmpeg refuses
subsamples in cbcs without an encryption pattern, although the same sample decrypts fine as a
whole sample. For such segments the boxes with the subsamples are turned into free boxes, so
ffmpeg decrypts the whole sample with the constant IV of the init segment. Nothing moves, so all
offsets in the segment stay valid.
"""

from __future__ import annotations

import struct
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator

# the bytes in front of the child boxes of a container that is searched for the track encryption
_CONTAINERS = {
    b"moov": 0,
    b"trak": 0,
    b"mdia": 0,
    b"minf": 0,
    b"stbl": 0,
    b"stsd": 8,
    b"enca": 28,
    b"sinf": 0,
    b"schi": 0,
}
_SUBSAMPLE_BOXES = (b"senc", b"saiz", b"saio")
_SENC_USE_SUBSAMPLES = 0x2
_TFHD_BASE_DATA_OFFSET = 0x1
_TFHD_SAMPLE_DESCRIPTION_INDEX = 0x2
_TFHD_DEFAULT_SAMPLE_DURATION = 0x8
_TFHD_DEFAULT_SAMPLE_SIZE = 0x10
_TRUN_DATA_OFFSET = 0x1
_TRUN_FIRST_SAMPLE_FLAGS = 0x4
_TRUN_SAMPLE_DURATION = 0x100
_TRUN_SAMPLE_SIZE = 0x200
_TRUN_SAMPLE_FLAGS = 0x400
_TRUN_SAMPLE_TIME_OFFSET = 0x800


def encrypts_whole_samples(init: bytes) -> bool:
    """
    Return if an init segment declares cbcs with a constant IV and no encryption pattern.

    Only such a track can have its subsamples dropped, as the whole sample is then encrypted
    with the same IV. The init segment of a live station holds a single audio track, so the first
    scheme and track encryption boxes are the ones of that track.

    :param init: The init segment of a live station.
    """
    try:
        schm = _find_box(init, 0, len(init), b"schm")
        tenc = _find_box(init, 0, len(init), b"tenc")
        if schm is None or tenc is None:
            return False
        if init[schm[0] + 4 : schm[0] + 8] != b"cbcs":
            return False
        body = tenc[0]
        version = init[body]
        pattern = init[body + 5] if version > 0 else 0
        is_protected, iv_size = init[body + 6], init[body + 7]
    except IndexError, struct.error:
        return False
    return pattern == 0 and is_protected == 1 and iv_size == 0


def drop_whole_sample_subsamples(segment: bytes) -> bytes:
    """
    Return a media segment without subsamples that each cover a whole sample.

    A track fragment keeps its subsamples when any of them leaves bytes clear or does not cover
    its whole sample. A segment that cannot be read is returned as it is. Only call this for a
    track for which encrypts_whole_samples is true.

    :param segment: A media segment of the track.
    """
    data = bytearray(segment)
    try:
        for name, _, body, end in _boxes(data, 0, len(data)):
            if name != b"moof":
                continue
            for child, _, traf_body, traf_end in _boxes(data, body, end):
                if child == b"traf":
                    _drop_traf_subsamples(data, traf_body, traf_end)
    except IndexError, struct.error:
        return segment
    return bytes(data)


def _drop_traf_subsamples(data: bytearray, start: int, end: int) -> None:
    """Turn the subsample boxes of one track fragment into free boxes when they are all whole."""
    boxes = list(_boxes(data, start, end))
    senc = next(((body, box_end) for name, _, body, box_end in boxes if name == b"senc"), None)
    if senc is None or _has_sample_group(data, boxes, b"seig"):
        return
    samples = _senc_sample_count(data, *senc)
    if samples is None:
        return
    default_size: int | None = None
    sizes: list[int] = []
    for name, _, body, box_end in boxes:
        if name == b"tfhd":
            default_size = _tfhd_default_size(data, body)
        elif name == b"trun":
            run_sizes = _trun_sizes(data, body, box_end, default_size, samples - len(sizes))
            if run_sizes is None:
                return
            sizes += run_sizes
    if not _whole_sample_subsamples(data, *senc, sizes):
        return
    for name, box_start, _, _ in boxes:
        if name in _SUBSAMPLE_BOXES:
            data[box_start + 4 : box_start + 8] = b"free"


def _senc_sample_count(data: bytearray, body: int, end: int) -> int | None:
    """Return the sample count of a senc box with subsamples, None when it has none or is cut."""
    flags = int.from_bytes(data[body + 1 : body + 4])
    if not flags & _SENC_USE_SUBSAMPLES:
        return None
    (count,) = struct.unpack_from(">I", data, body + 4)
    # each sample needs at least its subsample count
    return None if count * 2 > end - body - 8 else int(count)


def _whole_sample_subsamples(data: bytearray, body: int, end: int, sizes: list[int]) -> bool:
    """Return if a senc box holds one fully encrypted subsample per sample of the given sizes."""
    (count,) = struct.unpack_from(">I", data, body + 4)
    if count != len(sizes) or body + 8 + count * 8 > end:
        return False
    position = body + 8
    for size in sizes:
        subsamples, clear, protected = struct.unpack_from(">HHI", data, position)
        if subsamples != 1 or clear != 0 or protected != size:
            return False
        position += 8
    return True


def _tfhd_default_size(data: bytearray, body: int) -> int | None:
    """Return the default sample size of a track fragment header, None when it has none."""
    flags = int.from_bytes(data[body + 1 : body + 4])
    position = body + 8
    if flags & _TFHD_BASE_DATA_OFFSET:
        position += 8
    if flags & _TFHD_SAMPLE_DESCRIPTION_INDEX:
        position += 4
    if flags & _TFHD_DEFAULT_SAMPLE_DURATION:
        position += 4
    if flags & _TFHD_DEFAULT_SAMPLE_SIZE:
        return int(struct.unpack_from(">I", data, position)[0])
    return None


def _trun_sizes(
    data: bytearray, body: int, end: int, default_size: int | None, limit: int
) -> list[int] | None:
    """Return the sample sizes of a track run, None when they are unknown or more than limit."""
    flags = int.from_bytes(data[body + 1 : body + 4])
    (count,) = struct.unpack_from(">I", data, body + 4)
    if count > limit:
        return None
    position = body + 8
    if flags & _TRUN_DATA_OFFSET:
        position += 4
    if flags & _TRUN_FIRST_SAMPLE_FLAGS:
        position += 4
    if not flags & _TRUN_SAMPLE_SIZE:
        return None if default_size is None else [default_size] * count
    fields = [
        flag
        for flag in (
            _TRUN_SAMPLE_DURATION,
            _TRUN_SAMPLE_SIZE,
            _TRUN_SAMPLE_FLAGS,
            _TRUN_SAMPLE_TIME_OFFSET,
        )
        if flags & flag
    ]
    if position + count * len(fields) * 4 > end:
        return None
    size_index = fields.index(_TRUN_SAMPLE_SIZE)
    return [
        int(struct.unpack_from(">I", data, position + (sample * len(fields) + size_index) * 4)[0])
        for sample in range(count)
    ]


def _has_sample_group(
    data: bytearray, boxes: list[tuple[bytes, int, int, int]], grouping: bytes
) -> bool:
    """Return if a track fragment describes a sample group of the given type."""
    return any(
        name in (b"sgpd", b"sbgp") and data[body + 4 : body + 8] == grouping
        for name, _, body, _ in boxes
    )


def _find_box(data: bytes, start: int, end: int, wanted: bytes) -> tuple[int, int] | None:
    """Return the body start and the end of the first box of a type in the sample description."""
    for name, _, body, box_end in _boxes(data, start, end):
        if name == wanted:
            return body, box_end
        if (skip := _CONTAINERS.get(name)) is not None and (
            found := _find_box(data, body + skip, box_end, wanted)
        ):
            return found
    return None


def _boxes(data: bytes | bytearray, start: int, end: int) -> Iterator[tuple[bytes, int, int, int]]:
    """Yield the type, the start, the body start and the end of each box between start and end."""
    position = start
    while position + 8 <= end:
        size, name = struct.unpack_from(">I4s", data, position)
        body = position + 8
        if size == 1:
            (size,) = struct.unpack_from(">Q", data, body)
            body += 8
        elif size == 0:
            size = end - position
        if size < body - position or position + size > end:
            return
        yield bytes(name), position, body, position + size
        position += size
