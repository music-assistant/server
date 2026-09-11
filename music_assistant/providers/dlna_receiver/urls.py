"""
URL helpers shared by provider and renderer.

Lives in its own module (no Music Assistant dependency) so its pure,
security-sensitive helpers can be exercised by unit tests without
requiring the full MA runtime to be importable.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
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
    try:
        _ = parts.port
    except ValueError:
        return None
    return uri


async def validate_outbound_url(uri: str) -> str | None:
    """Return an outbound URL when every resolved destination address is allowed."""
    if validate_stream_url(uri) is None:
        return None
    hostname = urlsplit(uri).hostname
    if hostname is None:
        return None

    try:
        literal = ipaddress.ip_address(hostname)
    except ValueError:
        try:
            answers = await asyncio.get_running_loop().getaddrinfo(
                hostname,
                None,
                type=socket.SOCK_STREAM,
            )
        except OSError, UnicodeError:
            return None
        if not answers:
            return None
        try:
            addresses = [ipaddress.ip_address(answer[4][0]) for answer in answers]
        except IndexError, TypeError, ValueError:
            return None
        if not all(_is_allowed_destination(address) for address in addresses):
            return None
        return uri

    return uri if _is_allowed_destination(literal) else None


def _is_allowed_destination(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Return whether an IP address is permitted for outbound DLNA traffic."""
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return not (
        address.is_unspecified
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
    )


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
        port = parts.port
    except ValueError:
        return "<invalid-url>"
    host = parts.hostname or ""
    # urlsplit strips the enclosing brackets from IPv6 hostnames; restore them
    # so the reconstructed URL is syntactically valid.
    if ":" in host:
        host = f"[{host}]"
    netloc = host
    if port:
        netloc = f"{netloc}:{port}"
    if parts.username or parts.password:
        netloc = f"***@{netloc}"
    # Drop query + fragment (positions 3 and 4) regardless of whether
    # userinfo was present — query params are the common source of
    # accidental secret leakage into logs.
    return urlunsplit((parts.scheme, netloc, parts.path, "", ""))
