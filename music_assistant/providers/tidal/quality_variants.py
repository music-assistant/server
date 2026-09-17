"""
Collapse Tidal's per-quality-tier duplicate resources to one per real item.

Tidal's official API exposes the same album (or track recording) once per
audio quality tier it is available in, as separate resources sharing every
attribute except quality-related ones and the id. Artist listings therefore
show duplicates unless those variants are grouped back together first.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any

from .constants import MEDIA_TAG_HIRES_LOSSLESS
from .jsonapi import JsonApiDocument

VariantKey = tuple[Any, ...]

# Rank of Tidal's mediaTags values, highest quality first. Anything else
# (unknown or missing tags) ranks below all of these.
MEDIA_TAG_RANK: dict[str, int] = {
    MEDIA_TAG_HIRES_LOSSLESS: 3,
    "LOSSLESS": 2,
    "DOLBY_ATMOS": 1,
}


def quality_rank(attributes: Mapping[str, Any]) -> int:
    """
    Return the highest known quality rank among a resource's media tags.

    :param attributes: The resource's ``attributes`` object.
    """
    tags = attributes.get("mediaTags") or []
    return max((MEDIA_TAG_RANK.get(tag, 0) for tag in tags), default=0)


def album_variant_key(doc: JsonApiDocument, resource: dict[str, Any]) -> VariantKey:
    """
    Build a key identifying the real-world album behind a per-quality album resource.

    :param doc: The document the resource was resolved from.
    :param resource: The album resource.
    """
    attributes = resource.get("attributes", {})
    return (
        attributes.get("title"),
        attributes.get("version") or None,
        attributes.get("releaseDate"),
        attributes.get("numberOfItems"),
        attributes.get("explicit"),
        tuple(sorted(doc.linkage_ids(resource, "artists"))),
    )


def track_variant_key(doc: JsonApiDocument, resource: dict[str, Any]) -> VariantKey:
    """
    Build a key identifying the real-world recording behind a per-quality track resource.

    :param doc: The document the resource was resolved from.
    :param resource: The track resource.
    """
    attributes = resource.get("attributes", {})
    if isrc := attributes.get("isrc"):
        return ("isrc", isrc)
    return (
        "meta",
        attributes.get("title"),
        attributes.get("version") or None,
        attributes.get("duration"),
        tuple(sorted(doc.linkage_ids(resource, "artists"))),
    )


def collapse_quality_variants(
    pages: Sequence[JsonApiDocument],
    key_fn: Callable[[JsonApiDocument, dict[str, Any]], VariantKey],
) -> list[tuple[JsonApiDocument, dict[str, Any]]]:
    """
    Collapse per-quality-tier duplicate resources across one or more pages.

    Resources sharing a key (per ``key_fn``) are one real item; the highest-ranked
    (per :func:`quality_rank`) is kept, ties keep whichever was seen first. The
    result preserves the order in which each key first appeared.

    :param pages: The JSON:API pages the collection was read from. Variants of the
        same item can straddle a page boundary, so all pages must be considered
        together; each surviving resource is paired with the page it came from,
        since included resources are resolved per page.
    :param key_fn: Groups a resolved resource by the real-world item it represents.
    """
    order: list[VariantKey] = []
    best: dict[VariantKey, tuple[JsonApiDocument, dict[str, Any]]] = {}
    for doc in pages:
        for identifier in doc.data_list:
            if not (resource := doc.resolve(identifier)):
                continue
            key = key_fn(doc, resource)
            if key not in best:
                order.append(key)
                best[key] = (doc, resource)
                continue
            current_rank = quality_rank(resource.get("attributes", {}))
            best_rank = quality_rank(best[key][1].get("attributes", {}))
            if current_rank > best_rank:
                best[key] = (doc, resource)
    return [best[key] for key in order]
