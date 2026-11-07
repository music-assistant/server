"""Turn Music Assistant lyric text into timed lines for the kiosk."""

from __future__ import annotations

import re
from typing import Any

# One leading [mm:ss.xx] tag. The fraction separator may be "." or ":".
_LRC_LINE = re.compile(r"^\[(\d{1,3}):(\d{1,2}(?:[.:]\d{1,3})?)\]\s*(.*)$")


def parse_track_lyrics(plain: str | None, lrc: str | None) -> list[dict[str, Any]]:
    """
    Return lyric lines as ``{"t": seconds or None, "text": ...}``.

    Timed LRC wins over the plain text. Plain lines have no timestamp. The
    page then spreads them evenly across the song. Empty input is an empty list.
    """
    timed = _parse_lrc(lrc) or _parse_lrc(plain)
    if timed:
        return timed
    if not plain:
        return []
    return [{"t": None, "text": line.strip()} for line in plain.splitlines() if line.strip()]


def _parse_lrc(source: str | None) -> list[dict[str, Any]]:
    """Return timestamped lines, or an empty list when the text is not LRC."""
    if not source:
        return []
    lines: list[dict[str, Any]] = []
    for raw in source.splitlines():
        match = _LRC_LINE.match(raw.strip())
        if match is None:
            continue
        text = match.group(3).strip()
        if not text:
            continue
        minutes = int(match.group(1))
        seconds = float(match.group(2).replace(":", "."))
        lines.append({"t": minutes * 60 + seconds, "text": text})
    return lines
