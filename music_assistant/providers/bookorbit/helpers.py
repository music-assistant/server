"""Helpers for the BookOrbit provider."""

from __future__ import annotations

from typing import Any

from music_assistant.providers.bookorbit.constants import AUDIO_FORMATS


def normalize_base_url(url: str) -> str:
    """Strip trailing slashes from a base URL."""
    return url.rstrip("/")


def is_audio_format(fmt: str | None) -> bool:
    """Return True if the given book/file format is an audiobook audio format."""
    return bool(fmt) and fmt.lower() in AUDIO_FORMATS  # type: ignore[union-attr]


def library_may_contain_audio(library: dict[str, Any]) -> bool:
    """
    Return True if a BookOrbit library can hold audiobooks.

    Prefer the library's ``allowedFormats``. A library without any format restriction
    may hold anything, so it is kept and the (audio-only) book query decides.
    """
    allowed = {str(fmt).lower() for fmt in library.get("allowedFormats") or []}
    if not allowed:
        return True
    return not allowed.isdisjoint(AUDIO_FORMATS)
