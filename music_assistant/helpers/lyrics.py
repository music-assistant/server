"""Helpers for processing lyrics."""

from __future__ import annotations

import re

# LRC ID tags such as [ar:Artist] or [ti:Title] have a key that never starts with a digit,
# which distinguishes them from the [mm:ss.xx] timestamp tags on actual lyric lines.
# Also matches colon-less comment lines like [#some comment].
_LRC_ID_TAG_RE = re.compile(r"^\[(?:[a-zA-Z#][a-zA-Z0-9_]*:[^\]]*|#[^\]]*)\]\s*$")

# a single [mm:ss.xx] timestamp tag (the fraction separator may be . or :)
_LRC_TIMESTAMP_RE = re.compile(r"\[(\d{1,3}):(\d{1,2}(?:[.:]\d{1,3})?)\]")

# one or more (optionally whitespace separated) timestamp tags at the start of a lyric line
_LRC_TIMESTAMP_BLOCK_RE = re.compile(r"^((?:\[\d{1,3}:\d{1,2}(?:[.:]\d{1,3})?\]\s*)+)(.*)$")

# enhanced LRC word timing tags such as <00:22.00>, which would render as literal text
# TODO: consider supporting word level timing in the future instead of stripping it
_LRC_WORD_TIMING_RE = re.compile(r"<\d{1,3}:\d{1,2}(?:[.:]\d{1,3})?>\s?")


def normalize_lrc_lyrics(lrc_lyrics: str | None) -> str | None:
    """
    Normalize LRC formatted lyrics into simple, chronologically sorted LRC.

    Strips ID/metadata tag lines (e.g. [ar:...], [ti:...]) and word timing tags,
    and expands lines with multiple timestamps (repeating lyrics such as a chorus)
    into one line per timestamp, so clients only need a minimal LRC parser.

    :param lrc_lyrics: The LRC formatted lyrics to normalize, may be None.
    """
    if not lrc_lyrics:
        return lrc_lyrics
    entries: list[tuple[float, str]] = []
    # untimed lines inherit the previous timestamp so the (stable) sort keeps them in place
    last_time = 0.0
    for line in lrc_lyrics.splitlines():
        if _LRC_ID_TAG_RE.match(line.strip()):
            continue
        for time, lyric_line in _expand_line(_LRC_WORD_TIMING_RE.sub("", line)):
            last_time = time if time is not None else last_time
            entries.append((last_time, lyric_line))
    entries.sort(key=lambda entry: entry[0])
    return "\n".join(entry[1] for entry in entries).strip("\n") or None


def _expand_line(line: str) -> list[tuple[float | None, str]]:
    """Expand a lyric line into (time, line) entries, one per leading timestamp."""
    block_match = _LRC_TIMESTAMP_BLOCK_RE.match(line)
    if not block_match:
        return [(None, line)]
    timestamps = list(_LRC_TIMESTAMP_RE.finditer(block_match.group(1)))
    if len(timestamps) == 1:
        return [(_timestamp_to_seconds(timestamps[0]), line)]
    text = block_match.group(2)
    return [
        (_timestamp_to_seconds(ts), f"{ts.group(0)}{text}")
        for ts in sorted(timestamps, key=_timestamp_to_seconds)
    ]


def _timestamp_to_seconds(timestamp: re.Match[str]) -> float:
    """Return the time in seconds represented by a matched timestamp tag."""
    minutes = int(timestamp.group(1))
    seconds = float(timestamp.group(2).replace(":", "."))
    return minutes * 60 + seconds
