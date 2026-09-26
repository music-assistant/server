"""Map native lyric records to MA text and line-synchronized lyrics."""

from __future__ import annotations

import re
from typing import Any

from music_assistant_models.errors import InvalidDataError

from music_assistant.helpers.lyrics import extract_lrc_lyrics, normalize_lrc_lyrics

_OFFSET = re.compile(r"^\[offset:\s*([+-]?\d+)\s*\]$", re.IGNORECASE)
_TIMESTAMP = re.compile(r"\[(\d{1,3}):(\d{1,2})(?:[.:](\d{1,3}))?\]")
_PREFIX = re.compile(r"^((?:\[\d{1,3}:\d{1,2}(?:[.:]\d{1,3})?\]\s*)+)(.*)$")


def parse_lyrics(data: dict[str, Any]) -> tuple[str | None, str | None]:
    """Use the selected native lyric, falling back to the first nonempty record."""
    rows = data.get("list")
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise InvalidDataError("FeiNiu returned an invalid lyric list")
    candidates = [
        row for row in rows if isinstance(row.get("content"), str) and row["content"].strip()
    ]
    if not candidates:
        return None, None
    selected = next(
        (row for row in candidates if row.get("guid") == data.get("preferred")), candidates[0]
    )
    content = selected["content"].lstrip("\ufeff").strip()
    if len(content) > 256_000:
        raise InvalidDataError("FeiNiu lyric exceeds size limit")
    if not extract_lrc_lyrics(content):
        return content, None
    offset = selected.get("offset")
    if offset is not None and type(offset) is not int:
        raise InvalidDataError("FeiNiu returned an invalid lyric offset")
    normalized = normalize_lrc_lyrics(content) or ""
    plain = "\n".join(_PREFIX.sub(r"\2", line).strip() for line in normalized.splitlines())
    return plain.strip() or None, normalize_lrc_lyrics(_shift_timestamps(content, offset))


def _shift_timestamps(content: str, api_offset: int | None) -> str:
    """Account for the native millisecond alignment without mutating remote lyrics."""
    lines = content.splitlines()
    embedded_offsets = [
        int(match[1]) for line in lines if (match := _OFFSET.fullmatch(line.strip()))
    ]
    # The web player compares playback time + selected alignment with parsed LRC times.
    # Its LRC parser adds inline offsets; absent API alignment falls back to the LRC tag.
    alignment = (
        api_offset if api_offset is not None else (embedded_offsets[-1] if embedded_offsets else 0)
    )
    embedded_offset = 0
    result = []
    for raw_line in lines:
        line = raw_line.strip()
        if offset_tag := _OFFSET.fullmatch(line):
            embedded_offset = int(offset_tag[1])
            continue
        block = _PREFIX.match(line)
        if not block:
            result.append(line)
            continue
        for timestamp in _TIMESTAMP.finditer(block[1]):
            milliseconds = (
                int(timestamp[1]) * 60_000
                + int(timestamp[2]) * 1000
                + int((timestamp[3] or "0").ljust(3, "0"))
                + embedded_offset
                - alignment
            )
            if milliseconds < 0:
                continue
            minutes, remainder = divmod(milliseconds, 60_000)
            seconds, fraction = divmod(remainder, 1000)
            result.append(f"[{minutes:02d}:{seconds:02d}.{fraction:03d}]{block[2]}")
    return "\n".join(result)
