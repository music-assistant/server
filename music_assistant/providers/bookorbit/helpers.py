"""Helpers for the BookOrbit provider."""

from __future__ import annotations

from typing import Any

from music_assistant.providers.bookorbit.constants import AUDIO_FORMATS, BrowsePaths


def normalize_base_url(url: str) -> str:
    """Strip trailing slashes from a base URL."""
    return url.rstrip("/")


def is_audio_format(fmt: str | None) -> bool:
    """Return True if the given book/file format is an audiobook audio format."""
    return bool(fmt) and fmt.lower() in AUDIO_FORMATS  # type: ignore[union-attr]


def library_may_contain_audio(library: dict[str, Any]) -> bool:
    """
    Return True if a BookOrbit library should be queried during sync.

    ``allowedFormats`` is a hint only: libraries may list non-audio formats while still
    holding audiobooks, so every accessible library is queried. Books without audio
    files are dropped after ``get_book`` (query + ``_audio_files``).
    """
    _ = library
    return True


def parse_browse_path(path: str) -> tuple[int | None, list[str]]:
    """
    Split a browse path (``<instance_id>://lb <library id>/<section>/<item id>``).

    Returns the library id (None for the root) and the segments below the library
    (``[]``, ``[section]`` or ``[section, item_id]``).
    Raises ValueError for paths that are no BookOrbit browse paths.
    """
    _, _, item_path = path.partition("://")
    if not item_path:
        return None, []
    first, *segments = item_path.split("/")
    prefix, _, library_id = first.partition(" ")
    if prefix != BrowsePaths.LIBRARIES or not library_id.isdigit():
        raise ValueError(f"Invalid BookOrbit browse path {path}")
    return int(library_id), segments
