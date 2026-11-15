"""Helpers for the BookOrbit provider."""

from __future__ import annotations

import re
from typing import Any

from music_assistant_models.errors import InvalidDataError

from music_assistant.providers.bookorbit.constants import AUDIO_FORMATS, BrowsePaths

# Match BookOrbit packages/types series-index.ts.
_SERIES_INDEX_MAX_LENGTH = 20
_SERIES_INDEX_PATTERN = re.compile(r"^\d+(?:\.\d+)?$")


def normalize_base_url(url: str) -> str:
    """Strip trailing slashes from a base URL."""
    return url.rstrip("/")


def is_audio_format(fmt: str | None) -> bool:
    """Return True if the given book/file format is an audiobook audio format."""
    return bool(fmt) and fmt.lower() in AUDIO_FORMATS  # type: ignore[union-attr]


def card_may_contain_audio(card: dict[str, Any]) -> bool:
    """Return whether a BookOrbit search card lists at least one audio file."""
    files = card.get("files") or []
    return any(isinstance(file, dict) and is_audio_format(file.get("format")) for file in files)


def book_with_manifest_playback_order(book: dict[str, Any], manifest: Any) -> dict[str, Any]:
    """
    Return a book copy whose files carry ``sortOrder`` from the manifest sequence.

    BookDetail omits ``sortOrder``; playback order lives on the audiobook manifest.
    Raises ``InvalidDataError`` when the manifest cannot establish a playback order so
    callers do not cache or publish unordered multipart BookDetail.
    """
    if not isinstance(manifest, dict):
        raise InvalidDataError("BookOrbit manifest is missing or invalid")
    by_id = {
        int(file["id"]): dict(file)
        for file in (book.get("files") or [])
        if isinstance(file, dict) and file.get("id") is not None
    }
    if not by_id:
        raise InvalidDataError("BookOrbit book has no files to order from the manifest")
    assets = sorted(
        (asset for asset in (manifest.get("assets") or []) if asset.get("assetId")),
        key=lambda asset: int(asset.get("sequence") or 0),
    )
    ordered: list[dict[str, Any]] = []
    seen: set[int] = set()
    for sequence, asset in enumerate(assets):
        file_id = asset.get("fileId")
        if file_id is None:
            continue
        fid = int(file_id)
        if fid not in by_id:
            continue
        file = by_id[fid]
        file["sortOrder"] = sequence
        ordered.append(file)
        seen.add(fid)
    if not ordered:
        raise InvalidDataError("BookOrbit manifest has no mappable playback assets")
    # Partial mapping invents chapter/progress offsets; require every audio part.
    audio_ids = {fid for fid, file in by_id.items() if is_audio_format(file.get("format"))}
    if not audio_ids <= seen:
        raise InvalidDataError("BookOrbit manifest is missing playback assets for some audio files")
    for fid, file in by_id.items():
        if fid not in seen:
            ordered.append(file)
    # Preserve non-dict / id-less entries after ordered audio parts.
    extras = [
        file
        for file in (book.get("files") or [])
        if not (isinstance(file, dict) and file.get("id") is not None)
    ]
    return {**book, "files": [*ordered, *extras]}


def library_may_contain_audio(library: dict[str, Any]) -> bool:
    """Return whether a BookOrbit library should be queried for audiobooks."""
    # Podcast libraries reject /libraries/{id}/books with HTTP 400.
    # allowedFormats is only a hint; ignore it and drop non-audio books later.
    # Older BookOrbit builds omit type; treat those as book libraries.
    return str(library.get("type") or "books") == "books"


def parse_series_index(value: Any) -> str | None:
    """Return a BookOrbit series index string, or None when invalid."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        candidate = str(value)
    elif isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):  # noqa: PLR0124
            return None
        candidate = str(int(value)) if value.is_integer() else str(value)
    elif isinstance(value, str):
        candidate = value.strip()
    else:
        return None
    if len(candidate) > _SERIES_INDEX_MAX_LENGTH or not _SERIES_INDEX_PATTERN.fullmatch(candidate):
        return None
    return candidate


def _normalized_integer_segment(value: str) -> str:
    normalized = value.lstrip("0")
    return normalized or "0"


def compare_series_indices(left: str, right: str) -> int:
    """
    Compare series indices using BookOrbit segment semantics.

    Whole and fractional parts are integer segments (so ``1.10`` > ``1.2``),
    missing fractional segments sort before present ones, then original text ties.
    """
    left_parts = left.split(".", 1)
    right_parts = right.split(".", 1)
    whole = _compare_integer_segments(left_parts[0], right_parts[0])
    if whole:
        return whole
    left_has_frac = len(left_parts) > 1
    right_has_frac = len(right_parts) > 1
    if not left_has_frac and right_has_frac:
        return -1
    if left_has_frac and not right_has_frac:
        return 1
    if left_has_frac and right_has_frac:
        fraction = _compare_integer_segments(left_parts[1], right_parts[1])
        if fraction:
            return fraction
    if left == right:
        return 0
    return -1 if left < right else 1


def _compare_integer_segments(left: str, right: str) -> int:
    normalized_left = _normalized_integer_segment(left)
    normalized_right = _normalized_integer_segment(right)
    if len(normalized_left) != len(normalized_right):
        return -1 if len(normalized_left) < len(normalized_right) else 1
    if normalized_left == normalized_right:
        return 0
    return -1 if normalized_left < normalized_right else 1


def series_index_sort_key(index: str | None) -> tuple[Any, ...]:
    """Return a sort key matching :func:`compare_series_indices` (None last)."""
    if index is None:
        return (1,)
    whole, sep, fraction = index.partition(".")
    whole_n = _normalized_integer_segment(whole)
    if not sep:
        frac_key: tuple[Any, ...] = (0,)
    else:
        frac_n = _normalized_integer_segment(fraction)
        frac_key = (1, len(frac_n), frac_n)
    return (0, len(whole_n), whole_n, frac_key, index)


def collection_sequence_key(value: Any) -> str | None:
    """Return a plain series index for MediaItemCollection.sequence display."""
    return parse_series_index(value)


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
