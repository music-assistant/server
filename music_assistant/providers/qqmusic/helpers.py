"""Helper utilities for QQ Music provider."""

from __future__ import annotations

import html
import re
from typing import Any

_HTML_TAG_PATTERN = re.compile(r"<[^>]+>")


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
