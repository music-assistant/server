"""Helper utilities for QQ Music provider."""

from __future__ import annotations

import html
import re
from typing import Any

_HTML_TAG_PATTERN = re.compile(r"<[^>]+>")
_QRC_LINE_TIMESTAMP_PATTERN = re.compile(r"\[\d+,\d+\]")
_QRC_LINE_PREFIX_PATTERN = re.compile(r"^\[(\d+),(\d+)\]")
_QRC_WORD_TIMESTAMP_PATTERN = re.compile(r"\(\d+,\d+\)")


def clean_text(value: Any, fallback: str = "") -> str:
    """Normalize searchable/display text from QQ payload."""
    if value is None:
        return fallback
    text = str(value).strip()
    if not text:
        return fallback
    text = html.unescape(html.unescape(text))
    text = _HTML_TAG_PATTERN.sub("", text)
    text = " ".join(text.split())
    return text or fallback


def extract_first_text(data: dict[str, Any], keys: tuple[str, ...], fallback: str = "") -> str:
    """Extract first non-empty text by key candidates."""
    for key in keys:
        val = data.get(key)
        if isinstance(val, str) and clean_text(val):
            return clean_text(val)
    return fallback


def normalize_image_url(raw: Any) -> str:
    """Normalize QQ image URL (supports protocol-relative URLs)."""
    url = clean_text(raw)
    if not url:
        return ""
    if url.startswith("//"):
        return f"https:{url}"
    if url.startswith(("http://", "https://")):
        return url
    return ""


def extract_artist_mid(artist_obj: dict[str, Any]) -> str:
    """Extract QQ singer mid from common field variants."""
    return str(
        artist_obj.get("mid")
        or artist_obj.get("MID")
        or artist_obj.get("singerMid")
        or artist_obj.get("singerMID")
        or artist_obj.get("SingerMid")
        or artist_obj.get("singer_mid")
        or ""
    )


def normalize_qq_lyric_text(raw_text: str) -> str:
    """Normalize QQ lyric/qrc payload to readable plain text."""
    text = html.unescape(raw_text).replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\\n", "\n")
    text = _QRC_LINE_TIMESTAMP_PATTERN.sub("", text)
    text = _QRC_WORD_TIMESTAMP_PATTERN.sub("", text)
    lines: list[str] = []
    for raw_line in text.split("\n"):
        cleaned_line = raw_line.strip()
        if cleaned_line:
            lines.append(cleaned_line)
    return "\n".join(lines)


def ms_to_lrc_timestamp(ms_value: int) -> str:
    """Convert milliseconds to LRC timestamp string (mm:ss.xx)."""
    minute = ms_value // 60000
    second = (ms_value % 60000) // 1000
    centisecond = (ms_value % 1000) // 10
    return f"{minute:02d}:{second:02d}.{centisecond:02d}"


def qrc_to_lrc(raw_text: str) -> str:
    """Convert QQ QRC line-timed lyric text to basic LRC format."""
    text = html.unescape(raw_text).replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\\n", "\n")
    lrc_lines: list[str] = []
    for raw_line in text.split("\n"):
        line = raw_line.strip()
        if not line:
            continue
        line_match = _QRC_LINE_PREFIX_PATTERN.match(line)
        if not line_match:
            continue
        start_ms = int(line_match.group(1))
        lyric_content = line[line_match.end() :]
        lyric_content = _QRC_WORD_TIMESTAMP_PATTERN.sub("", lyric_content).strip()
        if not lyric_content:
            continue
        lrc_lines.append(f"[{ms_to_lrc_timestamp(start_ms)}]{lyric_content}")
    return "\n".join(lrc_lines)
