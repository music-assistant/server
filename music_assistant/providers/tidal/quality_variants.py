"""Group Tidal's per-quality-tier duplicates of an album or recording, best variant first."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any, Final

from .constants import MEDIA_TAG_HIRES_LOSSLESS, SKIPPABLE_ITEM_ERRORS
from .jsonapi import JsonApiDocument

VariantKey = tuple[Any, ...]
Variant = tuple[JsonApiDocument, dict[str, Any]]

MEDIA_TAG_RANK: dict[str, int] = {
    MEDIA_TAG_HIRES_LOSSLESS: 3,
    "LOSSLESS": 2,
    "DOLBY_ATMOS": 1,
}

# key for a resource whose key_fn raised, unique so it stays its own group
_UNKEYABLE: Final = object()


def quality_rank(attributes: Mapping[str, Any]) -> int:
    """Return the highest quality rank among a resource's media tags."""
    tags = attributes.get("mediaTags") or []
    return max((MEDIA_TAG_RANK.get(tag, 0) for tag in tags), default=0)


def album_variant_key(doc: JsonApiDocument, resource: dict[str, Any]) -> VariantKey:
    """Return the key identifying the real album behind an album resource."""
    attributes = resource.get("attributes") or {}
    return (
        attributes.get("title"),
        attributes.get("version") or None,
        attributes.get("releaseDate"),
        attributes.get("numberOfItems"),
        attributes.get("explicit"),
        tuple(sorted(doc.linkage_ids(resource, "artists"))),
    )


def track_variant_key(doc: JsonApiDocument, resource: dict[str, Any]) -> VariantKey:
    """Return the key identifying the real recording behind a track resource."""
    attributes = resource.get("attributes") or {}
    if isrc := attributes.get("isrc"):
        return ("isrc", isrc)
    return (
        "meta",
        attributes.get("title"),
        attributes.get("version") or None,
        attributes.get("duration"),
        tuple(sorted(doc.linkage_ids(resource, "artists"))),
    )


def group_quality_variants(
    pages: Sequence[JsonApiDocument],
    key_fn: Callable[[JsonApiDocument, dict[str, Any]], VariantKey],
) -> list[list[Variant]]:
    """
    Group the resources of one item across all pages, best quality first, ties by first seen.

    :param pages: All pages of the listing, since variants can straddle a page boundary.
    :param key_fn: Groups a resolved resource by the real item it represents.
    """
    order: list[VariantKey] = []
    groups: dict[VariantKey, list[Variant]] = {}
    for doc in pages:
        for identifier in doc.data_list:
            if not (resource := doc.resolve(identifier)):
                continue
            try:
                key = key_fn(doc, resource)
            except SKIPPABLE_ITEM_ERRORS:
                key = (_UNKEYABLE, id(resource))
            if key not in groups:
                order.append(key)
                groups[key] = []
            groups[key].append((doc, resource))
    return [
        sorted(
            groups[key],
            key=lambda variant: quality_rank(variant[1].get("attributes") or {}),
            reverse=True,
        )
        for key in order
    ]
