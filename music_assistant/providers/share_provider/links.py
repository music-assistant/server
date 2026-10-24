"""Provider URL mapping helpers for share payloads."""

from __future__ import annotations

from typing import TYPE_CHECKING
from urllib.parse import quote, urlsplit

if TYPE_CHECKING:
    from music_assistant_models.media_items import MediaItemType

PROVIDER_URL_SCHEMAS: dict[tuple[str, str], str] = {
    ("spotify", "track"): "https://open.spotify.com/track/{id}",
    ("spotify", "album"): "https://open.spotify.com/album/{id}",
    ("spotify", "artist"): "https://open.spotify.com/artist/{id}",
    ("tidal", "track"): "https://tidal.com/track/{id}",
    ("tidal", "album"): "https://tidal.com/album/{id}",
    ("tidal", "artist"): "https://tidal.com/artist/{id}",
    ("qobuz", "track"): "https://open.qobuz.com/track/{id}",
    ("qobuz", "album"): "https://open.qobuz.com/album/{id}",
    ("qobuz", "artist"): "https://open.qobuz.com/artist/{id}",
    ("ytmusic", "track"): "https://music.youtube.com/watch?v={id}",
    ("ytmusic", "artist"): "https://music.youtube.com/channel/{id}",
    ("deezer", "track"): "https://www.deezer.com/track/{id}",
    ("deezer", "album"): "https://www.deezer.com/album/{id}",
    ("deezer", "artist"): "https://www.deezer.com/artist/{id}",
}
PROVIDER_BASE_URLS: dict[str, str] = {
    "spotify": "https://open.spotify.com",
    "tidal": "https://tidal.com",
    "qobuz": "https://open.qobuz.com",
    "ytmusic": "https://music.youtube.com",
    "deezer": "https://www.deezer.com",
    "apple_music": "https://music.apple.com",
}
PROVIDER_LINK_HOSTS: dict[str, frozenset[str]] = {
    "spotify": frozenset({"open.spotify.com"}),
    "tidal": frozenset({"tidal.com", "www.tidal.com"}),
    "qobuz": frozenset({"open.qobuz.com"}),
    "ytmusic": frozenset({"music.youtube.com"}),
    "deezer": frozenset({"deezer.com", "www.deezer.com"}),
    "apple_music": frozenset({"music.apple.com"}),
}
ARTWORK_HOST_SUFFIXES: dict[str, str] = {
    "mzstatic.com": "m",
    "scdn.co": "s",
    "tidal.com": "t",
    "qobuz.com": "q",
    "dzcdn.net": "d",
    "bcbits.com": "b",
    "googleusercontent.com": "g",
    "ytimg.com": "y",
    "coverartarchive.org": "c",
    "archive.org": "a",
    "theaudiodb.com": "u",
    "fanart.tv": "f",
    "last.fm": "l",
    "lastfm.freetls.fastly.net": "L",
    "ssl-images-amazon.com": "z",
}
ARTWORK_SUFFIX_BY_CODE = {code: suffix for suffix, code in ARTWORK_HOST_SUFFIXES.items()}


def is_valid_provider_link(domain: str, value: str) -> bool:
    """Check that a provider link uses that provider's HTTPS host."""
    try:
        parsed = urlsplit(value)
    except ValueError:
        return False
    return (
        parsed.scheme == "https"
        and parsed.hostname in PROVIDER_LINK_HOSTS.get(domain, frozenset())
        and parsed.username is None
        and parsed.password is None
    )


def is_valid_artwork_url(value: str) -> bool:
    """Check that artwork is an HTTPS URL on a public hostname."""
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


def build_provider_links(item: MediaItemType, media_type: str) -> dict[str, str]:
    """Build provider URLs from source mappings or known URL schemas."""
    links: dict[str, str] = {}
    if not hasattr(item, "provider_mappings"):
        return links
    for mapping in item.provider_mappings:
        if isinstance(mapping.url, str) and is_valid_provider_link(
            mapping.provider_domain, mapping.url
        ):
            links[mapping.provider_domain] = mapping.url
            continue
        if (
            mapping.provider_domain == "qobuz"
            and media_type == "artist"
            and not mapping.item_id.isdecimal()
        ):
            continue
        schema = PROVIDER_URL_SCHEMAS.get((mapping.provider_domain, media_type))
        if schema:
            links[mapping.provider_domain] = schema.format(id=quote(mapping.item_id, safe=""))
    return links


def build_provider_refs(item: MediaItemType, media_type: str) -> dict[str, str]:
    """Build compact provider path/query references for share payloads."""
    return {
        domain: compact_provider_ref(domain, url)
        for domain, url in build_provider_links(item, media_type).items()
    }


def compact_provider_ref(domain: str, url: str) -> str:
    """Remove the fixed provider host from a validated URL."""
    if not is_valid_provider_link(domain, url):
        return ""
    parsed = urlsplit(url)
    path = parsed.path
    if domain == "apple_music":
        parts = path.split("/")
        if len(parts) >= 5 and parts[2] == "album" and parts[-1].isdigit():
            path = f"/{parts[1]}/album/{parts[-1]}"
    return path + (f"?{parsed.query}" if parsed.query else "")


def expand_provider_ref(domain: str, value: str) -> str | None:
    """Expand a compact relative provider reference into a validated HTTPS URL."""
    base_url = PROVIDER_BASE_URLS.get(domain)
    if not base_url or not value.startswith("/") or value.startswith("//") or "\\" in value:
        return None
    parsed = urlsplit(value)
    if parsed.scheme or parsed.netloc:
        return None
    url = f"{base_url}{value}"
    return url if is_valid_provider_link(domain, url) else None


def compact_artwork_ref(url: str) -> str | None:
    """Encode an approved artwork URL as a short host code and path."""
    if not is_valid_artwork_url(url):
        return None
    parsed = urlsplit(url)
    hostname = (parsed.hostname or "").lower().rstrip(".")
    for suffix, host_code in ARTWORK_HOST_SUFFIXES.items():
        if hostname == suffix or hostname.endswith(f".{suffix}"):
            subdomain = hostname[: -len(suffix)].rstrip(".")
            host = f"{host_code}:{subdomain}" if subdomain else host_code
            return f"{host}:{parsed.path}" + (f"?{parsed.query}" if parsed.query else "")
    return None


def expand_artwork_ref(value: str) -> str | None:
    """Expand a short artwork host code into a validated HTTPS URL."""
    parts = value.split(":", maxsplit=2)
    if len(parts) == 2:
        host_code, path_and_query = parts
        subdomain = ""
    elif len(parts) == 3:
        host_code, subdomain, path_and_query = parts
    else:
        return None
    suffix = ARTWORK_SUFFIX_BY_CODE.get(host_code)
    if not suffix or not path_and_query.startswith("/"):
        return None
    if subdomain and not all(
        part and part.replace("-", "").isalnum() for part in subdomain.split(".")
    ):
        return None
    hostname = f"{subdomain}.{suffix}" if subdomain else suffix
    url = f"https://{hostname}{path_and_query}"
    return url if is_valid_artwork_url(url) else None
