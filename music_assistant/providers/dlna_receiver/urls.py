"""URL helpers shared by provider and renderer.

Lives in its own module (no Music Assistant dependency) so its pure,
security-sensitive helpers can be exercised by unit tests without
requiring the full MA runtime to be importable.
"""

from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit

ALLOWED_STREAM_SCHEMES = frozenset({"http", "https"})


def validate_stream_url(uri: str) -> str | None:
    """Return the URI if it is a safe http(s) stream URL, else None."""
    if not uri:
        return None
    try:
        parts = urlsplit(uri)
    except ValueError:
        return None
    if parts.scheme.lower() not in ALLOWED_STREAM_SCHEMES:
        return None
    if not parts.hostname:
        return None
    return uri


def redact_url(uri: str) -> str:
    """Return a log-safe copy of a URL with userinfo replaced by ``***``."""
    try:
        parts = urlsplit(uri)
    except ValueError:
        return "<invalid-url>"
    if not parts.username and not parts.password:
        return uri
    host = parts.hostname or ""
    # urlsplit strips the enclosing brackets from IPv6 hostnames; restore them
    # so the reconstructed URL is syntactically valid.
    if ":" in host:
        host = f"[{host}]"
    netloc = host
    if parts.port:
        netloc = f"{netloc}:{parts.port}"
    return urlunsplit((parts.scheme, f"***@{netloc}", parts.path, parts.query, parts.fragment))
