"""Build and parse Bandcamp artist IDs."""

from __future__ import annotations

import re

# Slugs are lowercase alphanumerics joined by single hyphens.
# The colon separator between band_id and slug is unambiguous because
# band_ids are pure digits and slugs contain no colons.
_SLUG_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def slugify_performer(name: str) -> str:
    """
    Reduce a performer name to a stable lowercase slug.

    Two performer names compare equal as artists when their slugs match.
    The transform is intentionally lossy — punctuation and case are
    discarded — and ``"&"`` is normalized to ``"and"``. Bandcamp's
    per-album performer field is consistent within a band page in practice,
    so within-band collisions are rare; cross-band collisions are not a
    concern because ``band_id`` is part of the key.

    :param name: Raw performer name from the Bandcamp API.
    :returns: A slug containing only ``[a-z0-9-]`` with no leading or
        trailing hyphens. Empty string if ``name`` has no slug-eligible
        characters.
    """
    return _SLUG_NON_ALNUM.sub("-", name.lower().replace("&", "and")).strip("-")


def make_artist_id(band_id: int | str, performer: str | None = None) -> str:
    """
    Build the artist ``item_id`` used on Music Assistant media items.

    :param band_id: The Bandcamp ``band_id`` (page owner).
    :param performer: Optional performer name. If provided and produces a
        non-empty slug, the result is the synthetic form
        ``"{band_id}:{slug}"``. Otherwise the plain ``"{band_id}"`` form
        is returned (the page itself).
    """
    if not performer:
        return str(band_id)
    slug = slugify_performer(performer)
    if not slug:
        return str(band_id)
    return f"{band_id}:{slug}"


def parse_artist_id(artist_id: str) -> tuple[int, str | None]:
    """
    Split an artist ``item_id`` into ``(band_id, performer_slug)``.

    :returns: A tuple where the second element is ``None`` for plain
        ``"{band_id}"`` IDs and a non-empty slug for synthetic ones.
    :raises ValueError: If the band-id portion is not a valid integer.
    """
    band, _, slug = artist_id.partition(":")
    return int(band), (slug or None)
