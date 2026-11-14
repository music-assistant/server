"""URL validation helpers for share payloads."""

from __future__ import annotations

from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from music_assistant.helpers.uri import share_url_provider

if TYPE_CHECKING:
    from music_assistant_models.media_items import MediaItemType

ARTWORK_HOST_SUFFIXES = (
    "mzstatic.com",
    "scdn.co",
    "tidal.com",
    "qobuz.com",
    "dzcdn.net",
    "bcbits.com",
    "googleusercontent.com",
    "ytimg.com",
    "coverartarchive.org",
    "archive.org",
    "theaudiodb.com",
    "fanart.tv",
    "last.fm",
    "lastfm.freetls.fastly.net",
    "ssl-images-amazon.com",
)


def is_valid_provider_link(domain: str, value: str) -> bool:
    """Check that core recognizes the HTTPS URL as belonging to this provider."""
    try:
        parsed = urlsplit(value)
        return (
            parsed.scheme == "https"
            and parsed.username is None
            and parsed.password is None
            and share_url_provider(value) == domain
        )
    except ValueError:
        return False


def is_valid_artwork_url(value: str) -> bool:
    """Check that artwork is an HTTPS URL on an approved public hostname."""
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        if (
            parsed.scheme != "https"
            or not hostname
            or parsed.username is not None
            or parsed.password is not None
        ):
            return False
        normalized_hostname = hostname.lower().rstrip(".")
        return any(
            normalized_hostname == suffix or normalized_hostname.endswith(f".{suffix}")
            for suffix in ARTWORK_HOST_SUFFIXES
        )
    except ValueError:
        return False


def build_provider_links(item: MediaItemType) -> dict[str, str]:
    """Return valid provider URLs already present on the item's mappings."""
    links: dict[str, str] = {}
    if not hasattr(item, "provider_mappings"):
        return links
    for mapping in item.provider_mappings:
        if isinstance(mapping.url, str) and is_valid_provider_link(
            mapping.provider_domain, mapping.url
        ):
            links[mapping.provider_domain] = mapping.url
    return links
