"""Security utilities for input validation."""

from __future__ import annotations

import os
from ipaddress import IPv4Address, IPv6Address, ip_address
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from music_assistant_models.errors import InvalidDataError

from music_assistant.constants import ENCRYPT_SUFFIX
from music_assistant.helpers.aiohttp_client import resolve_hostname

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant


def is_safe_path(path: str, base_path: str | None = None) -> bool:
    """
    Check if path is free from path traversal components.

    :param path: The path to validate.
    :param base_path: If given, additionally require that path (resolved against
        base_path when relative) stays inside this base directory.
    """
    norm_path = os.path.normpath(path)
    if norm_path.startswith("..") or "/../" in norm_path or "\\..\\" in norm_path:
        return False
    if base_path is None:
        return True
    # Purely lexical containment check: no filesystem IO, so safe to call on the event loop.
    norm_base = os.path.normpath(base_path)
    if not Path(norm_path).is_absolute():
        norm_path = os.path.normpath(os.path.join(norm_base, norm_path))
    try:
        return os.path.commonpath((norm_base, norm_path)) == norm_base
    except ValueError:
        # commonpath raises for paths on different (Windows) drives
        return False


async def ensure_safe_outbound_url(mass: MusicAssistant, url: str) -> None:
    """
    Ensure the server may fetch a client-supplied URL.

    Hosts in the local network are allowed; loopback, link-local, multicast and other
    special-purpose addresses are not.

    :param mass: The MusicAssistant instance.
    :param url: The URL the server is about to fetch.
    :raises InvalidDataError: If the URL must not be fetched on a client's behalf.
    """
    try:
        parsed = urlsplit(url)
        hostname = parsed.hostname
    except ValueError as err:
        raise InvalidDataError("Invalid URL") from err
    if parsed.scheme not in ("http", "https"):
        raise InvalidDataError("Only http(s) URLs are allowed")
    if not hostname:
        raise InvalidDataError("Invalid URL")
    addresses: list[IPv4Address | IPv6Address | None]
    if (literal := _parse_ip(hostname)) is not None:
        addresses = [literal]
    else:
        try:
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
            resolved = await resolve_hostname(mass, hostname, port)
        except (OSError, ValueError) as err:
            raise InvalidDataError("URL could not be resolved") from err
        if not resolved:
            raise InvalidDataError("URL could not be resolved")
        addresses = [_parse_ip(host) for host in resolved]
    if any(address is None or _is_blocked_address(address) for address in addresses):
        raise InvalidDataError("URL points to a blocked address")


def is_safe_name(name: str) -> bool:
    """Check if name is safe for use (no path separators or traversal components)."""
    return not ("/" in name or "\\" in name or ".." in name)


def contains_encrypted_value(value: Any) -> bool:
    """Check if value is, or holds in a nested list or dict, an encrypted config string."""
    if isinstance(value, str):
        return value.startswith(ENCRYPT_SUFFIX)
    if isinstance(value, dict):
        return any(contains_encrypted_value(item) for item in value.values())
    if isinstance(value, list):
        return any(contains_encrypted_value(item) for item in value)
    return False


def _parse_ip(value: str) -> IPv4Address | IPv6Address | None:
    """Return value as IP address without zone or IPv4 mapping, or None if it is not one."""
    try:
        address = ip_address(value.split("%", 1)[0])
    except ValueError:
        return None
    if isinstance(address, IPv6Address) and address.ipv4_mapped is not None:
        return address.ipv4_mapped
    return address


def _is_blocked_address(address: IPv4Address | IPv6Address) -> bool:
    """Return True if the server must not connect to address on a client's behalf."""
    return (
        address.is_loopback
        or address.is_link_local
        or address.is_unspecified
        or address.is_multicast
        or address.is_reserved
    )
