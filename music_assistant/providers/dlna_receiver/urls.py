"""
URL helpers shared by provider and renderer.

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
    """
    Return a log-safe copy of a URL: strip userinfo and query/fragment.

    The query string on DLNA/streaming URLs commonly carries bearer tokens
    (``?token=...``), pre-signed GET parameters (``?sig=...&expires=...``),
    session keys, etc.; logging them would defeat the point of redacting
    userinfo. Drop both query and fragment unconditionally and replace any
    ``user[:pass]@`` with ``***@`` — callers only need something the
    operator can correlate, not a replayable URL.
    """
    try:
        parts = urlsplit(uri)
    except ValueError:
        return "<invalid-url>"
    host = parts.hostname or ""
    # urlsplit strips the enclosing brackets from IPv6 hostnames; restore them
    # so the reconstructed URL is syntactically valid.
    if ":" in host:
        host = f"[{host}]"
    netloc = host
    if parts.port:
        netloc = f"{netloc}:{parts.port}"
    if parts.username or parts.password:
        netloc = f"***@{netloc}"
    # Drop query + fragment (positions 3 and 4) regardless of whether
    # userinfo was present — query params are the common source of
    # accidental secret leakage into logs.
    return urlunsplit((parts.scheme, netloc, parts.path, "", ""))
