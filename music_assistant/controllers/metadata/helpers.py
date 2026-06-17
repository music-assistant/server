"""
Helper utilities for the Metadata Controller.

Pure functions used by the controller and its mixins that do not need access to
the controller instance.
"""

from __future__ import annotations

import ipaddress
import pathlib
import urllib.parse

from .constants import _ALLOWED_IMAGEPROXY_REQUEST_SCHEMES, _IMAGEPROXY_CONTENT_TYPES


def _detect_image_format(path: str) -> str:
    """Detect image format from file path extension, defaulting to jpg."""
    # strip any query suffix (e.g. a cache-busting ?cs=) before extension detection
    match pathlib.PurePath(path.split("?", 1)[0]).suffix.lower():
        case ".svg":
            return "svg"
        case ".png":
            return "png"
        case _:
            return "jpg"


def _normalize_imageproxy_format(value: str | None) -> str | None:
    """Return a validated, lowercase imageproxy format, or None when invalid."""
    if not value:
        return None
    normalized = value.strip().lower()
    if normalized in _IMAGEPROXY_CONTENT_TYPES:
        return normalized
    return None


def _is_safe_imageproxy_request_path(path: str) -> bool:
    r"""
    Return True if `path` is safe to fetch on behalf of an imageproxy client.

    Rejects any input containing control characters or surrounding whitespace
    (so a leading `\t`, ` `, or `\x00` cannot mask an otherwise-forbidden
    scheme), restricts the scheme to http, https, or empty (local / relative
    path), and for http(s) targets rejects IP-literal hosts that resolve to
    loopback, private, link-local or multicast ranges. DNS-resolved hostnames
    are trusted; full DNS-rebinding mitigation is out of scope here.
    """
    if any(ord(c) < 0x20 for c in path) or path != path.strip():
        return False
    parsed = urllib.parse.urlparse(path)
    scheme = parsed.scheme.lower()
    if scheme not in _ALLOWED_IMAGEPROXY_REQUEST_SCHEMES:
        return False
    if scheme in ("http", "https"):
        host = parsed.hostname  # already lowercased; brackets stripped for IPv6
        if not host or host == "localhost":
            return False
        try:
            ip = ipaddress.ip_address(host)
        except ValueError:
            return True
        if ip.is_loopback or ip.is_private or ip.is_link_local or ip.is_multicast:
            return False
    return True
