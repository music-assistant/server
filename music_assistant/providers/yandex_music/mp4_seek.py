"""Minimal MP4 box parser for seek support in Yandex Music buffered streaming.

Parses the moov atom to locate the file byte offset of the sample nearest to a
requested seek position. Only the boxes required for seeking are parsed:
- moov/mvhd  (movie-level timescale)
- moov/trak/mdia/mdhd  (track-level timescale; takes priority)
- moov/trak/mdia/minf/stbl/stts  (time-to-sample table)
- moov/trak/mdia/minf/stbl/stsc  (sample-to-chunk table)
- moov/trak/mdia/minf/stbl/stco / co64  (chunk offset table)

Public API:
    find_seek_byte(moov_data, seek_seconds) -> int | None
    find_moov_end(data) -> int | None
"""

from __future__ import annotations

import struct
from collections.abc import Generator


def _read_box_header(data: bytes, offset: int) -> tuple[str, int, int] | None:
    """Read an MP4 box header at offset and return (box_type, header_size, box_size).

    Handles both compact (4-byte size) and extended (8-byte size = 1 + 8-byte largesize)
    box headers. Returns None if there is not enough data or if box_size is invalid.

    :param data: Raw bytes containing the box.
    :param offset: Byte position where the box starts.
    :return: (box_type, header_size, total_box_size) or None.
    """
    if offset + 8 > len(data):
        return None
    raw_size = struct.unpack_from(">I", data, offset)[0]
    box_type = data[offset + 4 : offset + 8].decode("latin-1")

    if raw_size == 1:
        # Extended size: next 8 bytes hold the real size
        if offset + 16 > len(data):
            return None
        box_size = struct.unpack_from(">Q", data, offset + 8)[0]
        header_size = 16
    elif raw_size == 0:
        # Box extends to end of file — use remaining data length
        box_size = len(data) - offset
        header_size = 8
    else:
        box_size = raw_size
        header_size = 8

    if box_size < header_size:
        return None
    return box_type, header_size, box_size


def _iter_boxes(data: bytes, start: int, end: int) -> Generator[tuple[str, int, int], None, None]:
    """Iterate over boxes in data[start:end], yielding (box_type, payload_start, payload_end).

    :param data: Raw bytes.
    :param start: Start offset (inclusive).
    :param end: End offset (exclusive).
    """
    pos = start
    while pos < end:
        result = _read_box_header(data, pos)
        if result is None:
            break
        box_type, header_size, box_size = result
        payload_start = pos + header_size
        payload_end = pos + box_size
        yield box_type, payload_start, min(payload_end, end)
        pos += box_size
        if box_size == 0:
            break


def _find_box(data: bytes, start: int, end: int, target: str) -> tuple[int, int] | None:
    """Find the first box of type target in data[start:end].

    :param data: Raw bytes.
    :param start: Start offset.
    :param end: End offset.
    :param target: 4-character box type to find.
    :return: (payload_start, payload_end) or None.
    """
    for box_type, p_start, p_end in _iter_boxes(data, start, end):
        if box_type == target:
            return p_start, p_end
    return None


def _parse_mvhd_timescale(data: bytes, start: int, end: int) -> int | None:
    """Parse the timescale from an mvhd box payload.

    :param data: Raw bytes.
    :param start: Payload start offset.
    :param end: Payload end offset.
    :return: Timescale value or None.
    """
    if end - start < 1:
        return None
    version = data[start]
    # version 0: 4-byte creation/modification times + 4-byte timescale at offset 12
    # version 1: 8-byte creation/modification times + 4-byte timescale at offset 20
    ts_offset = start + (20 if version == 1 else 12)
    if ts_offset + 4 > end:
        return None
    return struct.unpack_from(">I", data, ts_offset)[0] or None


def _parse_mdhd_timescale(data: bytes, start: int, end: int) -> int | None:
    """Parse the timescale from an mdhd box payload.

    :param data: Raw bytes.
    :param start: Payload start offset.
    :param end: Payload end offset.
    :return: Timescale value or None.
    """
    return _parse_mvhd_timescale(data, start, end)  # Same layout as mvhd up to timescale


def _parse_stts(data: bytes, start: int, end: int) -> list[tuple[int, int]]:
    """Parse stts (time-to-sample) box payload.

    :param data: Raw bytes.
    :param start: Payload start offset.
    :param end: Payload end offset.
    :return: List of (sample_count, sample_duration) entries.
    """
    # Skip version (1 byte) + flags (3 bytes)
    offset = start + 4
    if offset + 4 > end:
        return []
    entry_count = struct.unpack_from(">I", data, offset)[0]
    offset += 4
    entries = []
    for _ in range(entry_count):
        if offset + 8 > end:
            break
        count, duration = struct.unpack_from(">II", data, offset)
        entries.append((count, duration))
        offset += 8
    return entries


def _parse_stsc(data: bytes, start: int, end: int) -> list[tuple[int, int, int]]:
    """Parse stsc (sample-to-chunk) box payload.

    :param data: Raw bytes.
    :param start: Payload start offset.
    :param end: Payload end offset.
    :return: List of (first_chunk, samples_per_chunk, sample_description_index).
    """
    offset = start + 4  # skip version+flags
    if offset + 4 > end:
        return []
    entry_count = struct.unpack_from(">I", data, offset)[0]
    offset += 4
    entries = []
    for _ in range(entry_count):
        if offset + 12 > end:
            break
        first_chunk, spc, sdi = struct.unpack_from(">III", data, offset)
        entries.append((first_chunk, spc, sdi))
        offset += 12
    return entries


def _parse_stco(data: bytes, start: int, end: int) -> list[int]:
    """Parse stco (chunk offset) box payload.

    :param data: Raw bytes.
    :param start: Payload start offset.
    :param end: Payload end offset.
    :return: List of file byte offsets for each chunk (1-indexed in MP4, returned 0-indexed).
    """
    offset = start + 4  # skip version+flags
    if offset + 4 > end:
        return []
    entry_count = struct.unpack_from(">I", data, offset)[0]
    offset += 4
    offsets = []
    for _ in range(entry_count):
        if offset + 4 > end:
            break
        offsets.append(struct.unpack_from(">I", data, offset)[0])
        offset += 4
    return offsets


def _parse_co64(data: bytes, start: int, end: int) -> list[int]:
    """Parse co64 (64-bit chunk offset) box payload.

    :param data: Raw bytes.
    :param start: Payload start offset.
    :param end: Payload end offset.
    :return: List of file byte offsets for each chunk.
    """
    offset = start + 4  # skip version+flags
    if offset + 4 > end:
        return []
    entry_count = struct.unpack_from(">I", data, offset)[0]
    offset += 4
    offsets = []
    for _ in range(entry_count):
        if offset + 8 > end:
            break
        offsets.append(struct.unpack_from(">Q", data, offset)[0])
        offset += 8
    return offsets


def _sample_index_for_time(stts: list[tuple[int, int]], target_ticks: int) -> int:
    """Return the 0-based sample index closest to target_ticks using stts entries.

    Iterates the run-length encoded stts table to find the sample whose cumulative
    time is at or just before target_ticks.

    :param stts: List of (sample_count, sample_duration) pairs from stts box.
    :param target_ticks: Target time in track timescale units.
    :return: 0-based sample index.
    """
    elapsed = 0
    sample_index = 0
    for count, duration in stts:
        for _ in range(count):
            if elapsed + duration > target_ticks:
                return sample_index
            elapsed += duration
            sample_index += 1
    return max(0, sample_index - 1)


def _chunk_for_sample(stsc: list[tuple[int, int, int]], sample_index: int) -> int:
    """Return the 1-based chunk index containing the given 0-based sample index.

    Uses the stsc (sample-to-chunk) run-length table.

    :param stsc: List of (first_chunk, samples_per_chunk, sdi) entries from stsc.
    :param sample_index: 0-based sample index.
    :return: 1-based chunk number.
    """
    if not stsc:
        return 1
    cumulative_samples = 0
    for i, (first_chunk, spc, _) in enumerate(stsc):
        if i + 1 < len(stsc):
            next_first_chunk = stsc[i + 1][0]
            chunks_in_run = next_first_chunk - first_chunk
        else:
            chunks_in_run = None  # Last run extends to end

        if chunks_in_run is not None:
            samples_in_run = chunks_in_run * spc
            if cumulative_samples + samples_in_run > sample_index:
                # Sample is within this run
                chunk_offset_in_run = (sample_index - cumulative_samples) // spc
                return first_chunk + chunk_offset_in_run
            cumulative_samples += samples_in_run
        else:
            # Last stsc entry: sample is in this run
            chunk_offset_in_run = (sample_index - cumulative_samples) // spc
            return first_chunk + chunk_offset_in_run

    return 1


def find_moov_end(data: bytes) -> int | None:
    """Find the end byte offset of the moov box in data.

    Scans top-level boxes to locate the moov atom. Returns the exclusive end
    offset (i.e., the first byte after moov), or None if moov was not found.

    :param data: Bytes to scan (typically the first 1-4 MB of the file).
    :return: Exclusive end offset of the moov box, or None.
    """
    pos = 0
    while pos < len(data):
        result = _read_box_header(data, pos)
        if result is None:
            break
        box_type, _, box_size = result
        if box_type == "moov":
            return pos + box_size
        pos += box_size
        if box_size == 0:
            break
    return None


def find_seek_byte(moov_data: bytes, seek_seconds: float) -> int | None:
    """Parse moov atom and return the file byte offset for seek_seconds.

    Locates the moov box, parses stts/stsc/stco (or co64), and returns the file
    byte offset of the chunk containing the sample nearest to seek_seconds.
    Returns None if moov is incomplete, absent, or parsing fails.

    :param moov_data: Bytes containing the start of the MP4 file (ideally >= moov end).
    :param seek_seconds: Target seek position in seconds.
    :return: File byte offset of the nearest chunk, or None on failure.
    """
    try:
        # Locate top-level moov box
        moov = _find_box(moov_data, 0, len(moov_data), "moov")
        if moov is None:
            return None
        moov_start, moov_end = moov

        # Parse movie timescale from mvhd
        mvhd = _find_box(moov_data, moov_start, moov_end, "mvhd")
        movie_timescale = _parse_mvhd_timescale(moov_data, *mvhd) if mvhd else None

        # Walk trak boxes to find the audio/video track with stbl
        for box_type, t_start, t_end in _iter_boxes(moov_data, moov_start, moov_end):
            if box_type != "trak":
                continue

            # mdia
            mdia = _find_box(moov_data, t_start, t_end, "mdia")
            if mdia is None:
                continue

            # mdhd gives track-level timescale (takes priority over mvhd)
            mdhd = _find_box(moov_data, mdia[0], mdia[1], "mdhd")
            timescale = (
                _parse_mdhd_timescale(moov_data, *mdhd) if mdhd else movie_timescale
            ) or movie_timescale
            if not timescale:
                continue

            # minf → stbl
            minf = _find_box(moov_data, mdia[0], mdia[1], "minf")
            if minf is None:
                continue
            stbl = _find_box(moov_data, minf[0], minf[1], "stbl")
            if stbl is None:
                continue

            # Parse stts, stsc, chunk offsets
            stts_box = _find_box(moov_data, stbl[0], stbl[1], "stts")
            stsc_box = _find_box(moov_data, stbl[0], stbl[1], "stsc")
            stco_box = _find_box(moov_data, stbl[0], stbl[1], "stco")
            co64_box = _find_box(moov_data, stbl[0], stbl[1], "co64")

            if stts_box is None or (stco_box is None and co64_box is None):
                continue

            stts = _parse_stts(moov_data, *stts_box)
            stsc = _parse_stsc(moov_data, *stsc_box) if stsc_box else []
            if stco_box:
                chunk_offsets = _parse_stco(moov_data, *stco_box)
            elif co64_box:
                chunk_offsets = _parse_co64(moov_data, *co64_box)
            else:
                continue

            if not stts or not chunk_offsets:
                continue

            target_ticks = int(seek_seconds * timescale)
            sample_index = _sample_index_for_time(stts, target_ticks)
            chunk_index_1based = _chunk_for_sample(stsc, sample_index)
            # chunk_offsets is 0-indexed; MP4 chunk numbers are 1-indexed
            array_index = chunk_index_1based - 1
            if array_index < 0 or array_index >= len(chunk_offsets):
                array_index = max(0, min(array_index, len(chunk_offsets) - 1))

            return chunk_offsets[array_index]

    except Exception:
        return None

    return None


def patch_stco(moov_data: bytes, delta: int) -> bytes:
    """Subtract delta from all stco/co64 chunk offsets in the moov atom.

    Used to adjust absolute file offsets after seeking: when we skip seek_byte
    bytes into the mdat, all chunk offsets must be decremented by the number of
    bytes we skipped (seek_byte), then re-incremented by the size of the moov
    prefix we serve to ffmpeg.

    :param moov_data: Raw bytes of the moov box (and any preceding boxes like ftyp).
    :param delta: Value to subtract from every chunk offset entry.
    :return: Modified bytes with adjusted offsets.
    """
    result = bytearray(moov_data)

    def _patch_box(box_name: str, entry_size: int, struct_fmt: str) -> None:
        """Patch all entries in stco or co64 boxes found recursively."""
        pos = 0
        while pos < len(result):
            hdr = _read_box_header(bytes(result), pos)
            if hdr is None:
                break
            btype, hdr_size, bsize = hdr
            payload_start = pos + hdr_size
            payload_end = pos + bsize
            if btype == box_name:
                # Skip version (1) + flags (3) + entry_count (4) = 8 bytes
                offset = payload_start + 4
                if offset + 4 > payload_end:
                    pos += bsize
                    continue
                entry_count = struct.unpack_from(">I", result, offset)[0]
                offset += 4
                for _ in range(entry_count):
                    if offset + entry_size > payload_end:
                        break
                    val = struct.unpack_from(struct_fmt, result, offset)[0]
                    new_val = max(0, val - delta)
                    struct.pack_into(struct_fmt, result, offset, new_val)
                    offset += entry_size
            pos += bsize
            if bsize == 0:
                break

    _patch_box("stco", 4, ">I")
    _patch_box("co64", 8, ">Q")
    return bytes(result)
