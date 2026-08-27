"""Helpers for validating redirect URLs in OAuth/auth flows."""

from __future__ import annotations

import ipaddress
import logging
from typing import TYPE_CHECKING
from urllib.parse import quote, urlparse

from music_assistant.constants import MASS_LOGGER_NAME

if TYPE_CHECKING:
    from aiohttp import web

LOGGER = logging.getLogger(f"{MASS_LOGGER_NAME}.redirect_validation")

# Allowed redirect URI patterns
# Add custom URL schemes for mobile apps here
ALLOWED_REDIRECT_PATTERNS = [
    # Custom URL schemes for mobile apps
    "musicassistant://",  # Music Assistant mobile app
    # Home Assistant domains
    "https://my.home-assistant.io/",
    "http://homeassistant.local/",
    "https://homeassistant.local/",
]


def is_allowed_redirect_url(
    url: str,
    request: web.Request | None = None,
    base_url: str | None = None,
) -> tuple[bool, str]:
    """
    Validate if a redirect URL is allowed for OAuth/auth flows.

    Security rules (in order of priority):
    1. Must use http, https, or registered custom scheme (e.g., musicassistant://)
    2. Same origin as the request - auto-allowed (trusted)
    3. Localhost (127.0.0.1, ::1, localhost) - auto-allowed (trusted)
    4. Private network IPs (RFC 1918) - auto-allowed (trusted)
    5. Configured base_url - auto-allowed (trusted)
    6. Matches allowed redirect patterns - auto-allowed (trusted)
    7. Everything else - requires user consent (external)

    :param url: The redirect URL to validate.
    :param request: Optional aiohttp request to compare origin.
    :param base_url: Optional configured base URL to allow.
    :return: Tuple of (is_valid, category) where category is:
        - "trusted": Auto-allowed, no consent needed
        - "external": Valid but requires user consent
        - "blocked": Invalid/dangerous URL
    """
    if not url:
        return False, "blocked"

    try:
        parsed = urlparse(url)

        # Check for custom URL schemes (mobile apps)
        for pattern in ALLOWED_REDIRECT_PATTERNS:
            if url.startswith(pattern):
                LOGGER.debug("Redirect URL trusted (pattern match): %s", url)
                return True, "trusted"

        # Only http/https for web URLs
        if parsed.scheme not in ("http", "https"):
            LOGGER.warning("Redirect URL blocked (invalid scheme): %s", url)
            return False, "blocked"

        hostname = parsed.hostname
        if not hostname:
            LOGGER.warning("Redirect URL blocked (no hostname): %s", url)
            return False, "blocked"

        # 1. Same origin as request - always trusted
        if request:
            request_host = request.host
            if parsed.netloc == request_host:
                LOGGER.debug("Redirect URL trusted (same origin): %s", url)
                return True, "trusted"

        # 2. Localhost - always trusted (for development and mobile app testing)
        if hostname in ("localhost", "127.0.0.1", "::1"):
            LOGGER.debug("Redirect URL trusted (localhost): %s", url)
            return True, "trusted"

        # 3. Private network IPs - always trusted (for local network access)
        if _is_private_ip(hostname):
            LOGGER.debug("Redirect URL trusted (private IP): %s", url)
            return True, "trusted"

        # 4. Configured base_url - always trusted
        if base_url:
            base_parsed = urlparse(base_url)
            if parsed.netloc == base_parsed.netloc:
                LOGGER.debug("Redirect URL trusted (base_url): %s", url)
                return True, "trusted"

        # If we get here, URL is external and requires user consent
        LOGGER.info("Redirect URL is external (requires consent): %s", url)
        return True, "external"

    except Exception:
        LOGGER.exception("Error validating redirect URL")
        return False, "blocked"


def build_code_redirect_url(
    return_url: str,
    token: str,
    extra_params: dict[str, str] | None = None,
) -> str:
    """
    Build a redirect URL with the auth code appended to the query string.

    The code (and any extra params) are inserted before a possible hash fragment
    so they end up in the query string rather than inside the fragment.

    :param return_url: The validated destination URL to redirect to.
    :param token: The auth token to pass along as the ``code`` query parameter.
    :param extra_params: Optional additional query parameters to append.
    """
    params = f"code={quote(token, safe='')}"
    if extra_params:
        for key, value in extra_params.items():
            params += f"&{quote(key, safe='')}={quote(value, safe='')}"

    if "#" in return_url:
        base_part, hash_part = return_url.split("#", 1)
        return f"{_append_query_params(base_part, params)}#{hash_part}"

    return _append_query_params(return_url, params)


def _append_query_params(base: str, params: str) -> str:
    """Append query params to a URL part, reusing a trailing ? or & separator if present."""
    if base.endswith(("?", "&")):
        return f"{base}{params}"
    separator = "&" if "?" in base else "?"
    return f"{base}{separator}{params}"


def _is_private_ip(hostname: str) -> bool:
    """Check if hostname is a private IP address (RFC 1918)."""
    try:
        ip = ipaddress.ip_address(hostname)
        return ip.is_private
    except ValueError:
        # Not a valid IP address
        return False
