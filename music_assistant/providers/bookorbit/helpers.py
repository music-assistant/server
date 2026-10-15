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
    """Return whether a BookOrbit library should be included during sync."""
    # allowedFormats is only a hint; query every accessible library and drop non-audio books later.
    _ = library
    return True


def parse_browse_path(path: str) -> tuple[int | None, list[str]]:
    """
    Split a BookOrbit browse path into library id and path segments.

    :param path: Browse path (``<instance_id>://lb <library id>/<section>/<item id>``).
    :returns: Library id (``None`` for the root) and segments below the library.
    :raises ValueError: If the path is not a BookOrbit browse path.
    """
    _, _, item_path = path.partition("://")
    if not item_path:
        return None, []
    first, *segments = item_path.split("/")
    prefix, _, library_id = first.partition(" ")
    if prefix != BrowsePaths.LIBRARIES or not library_id.isdigit():
        raise ValueError(f"Invalid BookOrbit browse path {path}")
    return int(library_id), segments
