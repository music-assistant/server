"""Helpers for the BookOrbit provider."""

from __future__ import annotations

from music_assistant.providers.bookorbit.constants import AUDIO_FORMATS


def normalize_base_url(url: str) -> str:
    """Strip trailing slashes from a base URL."""
    return url.rstrip("/")


def is_audio_format(fmt: str | None) -> bool:
    """Return True if the given book/file format is an audiobook audio format."""
    return bool(fmt) and fmt.lower() in AUDIO_FORMATS  # type: ignore[union-attr]
