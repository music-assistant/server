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


def extract_lrc_lyrics(lyrics: str | None) -> str | None:
    """
    Return the given plain lyrics text if it is LRC formatted, None otherwise.

    :param lyrics: The plain lyrics text to inspect, may be None.
    """
    if not lyrics:
        return None
    stripped_lines = (line.strip() for line in lyrics.splitlines())
    content_lines = [line for line in stripped_lines if line and not _LRC_ID_TAG_RE.match(line)]
    if not content_lines:
        return None
    timestamped = sum(1 for line in content_lines if _LRC_TIMESTAMP_BLOCK_RE.match(line))
    # require most lines to carry a leading timestamp to avoid false positives on
    # plain lyrics that merely mention something like [2:30] somewhere in the text
    if timestamped * 2 < len(content_lines):
        return None
    return lyrics


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


def convert_to_lrc_lyrics(lyrics: list[tuple[str, int]]) -> str:
    """
    Convert lyrics to LRC format.

    :param lyrics: A list of (text, timestamp in ms) pairs.
    """

    def format_line(text: str, ms: int) -> str | None:
        if ms < 0:
            return None

        mins, ms = divmod(ms, 60_000)
        secs, ms = divmod(ms, 1_000)

        if mins > 99:
            return None

        # Replace newline and carriage return and strip leading/trailing whitespace
        text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\n", " ").strip()

        return f"[{mins:02d}:{secs:02d}.{ms // 10:02d}]{text}"

    return "\n".join(line for text, ms in lyrics if (line := format_line(text, ms)) is not None)
