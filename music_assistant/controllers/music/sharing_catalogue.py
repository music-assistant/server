"""Verified public catalogue routes for additional music providers."""

import re
from datetime import date
from typing import TYPE_CHECKING

from music_assistant_models.enums import MediaType

if TYPE_CHECKING:
    from music_assistant_models.media_items import ProviderMapping


def catalogue_url(media_type: MediaType, mapping: ProviderMapping) -> str | None:
    """Return a verified public catalogue URL, or None for unsupported mappings."""
    item_id = mapping.item_id
    if mapping.provider_domain == "phishin" and media_type == MediaType.ALBUM:
        if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", item_id):
            return None
        try:
            date.fromisoformat(item_id)
        except ValueError:
            return None
        return f"https://phish.in/{item_id}"
    if not re.fullmatch(r"[0-9]+", item_id):
        return None
    if mapping.provider_domain == "telmore" and media_type == MediaType.ARTIST:
        return f"https://musik.telmore.dk/kunstner/{item_id}"
    if mapping.provider_domain != "musicme" or not mapping.url:
        return None
    slug = r"[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*"
    if media_type == MediaType.ARTIST:
        pattern = rf"https://www\.musicme\.com/{slug}/"
    elif media_type == MediaType.ALBUM:
        pattern = rf"https://www\.musicme\.com/{slug}/albums/{slug}-{item_id}\.html"
    else:
        return None
    return mapping.url if re.fullmatch(pattern, mapping.url) else None
