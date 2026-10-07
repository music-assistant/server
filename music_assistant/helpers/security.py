"""Security utilities for input validation."""

from __future__ import annotations

import os
import re
from contextlib import suppress
from ipaddress import IPv4Address, IPv6Address, ip_address
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from music_assistant_models.enums import ConfigEntryType
from music_assistant_models.errors import InvalidDataError

from music_assistant.constants import ENCRYPT_SUFFIX
from music_assistant.helpers.aiohttp_client import resolve_hostname

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant
    from music_assistant.models.provider import Provider

_DEFAULT_PORTS = {"http": 80, "https": 443}
# config key parts whose bare (scheme-less) value names a server, e.g. "host" or "ip_address"
_HOST_KEY_PARTS = frozenset({"host", "hostname", "url", "baseurl", "server", "address", "ip"})
_HOST_KEY_SUFFIX_RE = re.compile(r"(ip_address|address|host|url|ip)$")
_HOSTNAME_RE = re.compile(r"^[a-z0-9_-]+(\.[a-z0-9_-]+)*$", re.IGNORECASE)


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


async def ensure_safe_outbound_url(
    mass: MusicAssistant, url: str, allowed_schemes: tuple[str, ...] = ("http", "https")
) -> None:
    """
    Ensure the server may fetch a client-supplied URL.

    Hosts in the local network are allowed; loopback, link-local, multicast and other
    special-purpose addresses are not.

    :param mass: The MusicAssistant instance.
    :param url: The URL the server is about to fetch.
    :param allowed_schemes: The URL schemes (without "://") the caller can fetch.
    :raises InvalidDataError: If the URL must not be fetched on a client's behalf.
    """
    try:
        parsed = urlsplit(url)
        hostname = parsed.hostname
    except ValueError as err:
        raise InvalidDataError("Invalid URL") from err
    if parsed.scheme not in allowed_schemes:
        msg = f"Only {', '.join(allowed_schemes)} URLs are allowed"
        raise InvalidDataError(msg)
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


def url_endpoint(url: str) -> tuple[str, int] | None:
    """
    Return the (lowercased host, port) a URL connects to.

    :param url: An absolute URL; the port defaults to that of http(s) when omitted.
    :returns: None if the URL has no host, or no port and no http(s) scheme.
    """
    try:
        parsed = urlsplit(url)
        hostname, port = parsed.hostname, parsed.port
    except ValueError:
        return None
    if not hostname or not (port := port or _DEFAULT_PORTS.get(parsed.scheme.lower())):
        return None
    return hostname, port


def provider_configured_endpoints(provider: Provider) -> frozenset[tuple[str, int]]:
    """
    Return the (lowercased host, port) endpoints a provider instance is configured with.

    Collects URL values, and bare host values (optionally with a port, or paired with a
    sibling port key) of host-like keys, from its config and its setup data. Secure config
    values are never read.

    :param provider: The loaded provider instance.
    """
    config_values = {
        key: entry.value
        for key, entry in provider.config.values.items()
        if entry.type != ConfigEntryType.SECURE_STRING
    }
    # setup data is encrypted at rest, so only host and port keys are decrypted
    setup_values = {
        key: provider.get_setup_value(key)
        for key in provider.config.setup_data
        if _is_host_key(key) or key.lower().endswith("port")
    }
    return frozenset(_collect_endpoints(config_values) | _collect_endpoints(setup_values))


def _is_host_key(key: str) -> bool:
    """Return True if a config key name suggests its value names a server."""
    lowered = key.lower()
    parts = set(re.split(r"[^a-z0-9]+", lowered))
    return not lowered.endswith("port") and bool(parts & _HOST_KEY_PARTS)


def _collect_endpoints(values: dict[str, Any]) -> set[tuple[str, int]]:
    """Return the endpoints named by URL values and by bare host values of host-like keys."""
    endpoints: set[tuple[str, int]] = set()
    for key, value in values.items():
        if not isinstance(value, str) or value.startswith(ENCRYPT_SUFFIX):
            continue
        host_like = _is_host_key(key)
        sibling_port = _sibling_port(values, key) if host_like else None
        try:
            parsed = urlsplit(value)
            hostname, port = parsed.hostname, parsed.port
        except ValueError:
            continue
        if hostname:
            if port := port or sibling_port or _DEFAULT_PORTS.get(parsed.scheme.lower()):
                endpoints.add((hostname, port))
        elif host_like:
            endpoints.update(_bare_host_endpoints(value, sibling_port))
    return endpoints


def _sibling_port(values: dict[str, Any], host_key: str) -> int | None:
    """Return the valid port stored next to a host key, e.g. local_server_port or port."""
    lowered = {key.lower(): value for key, value in values.items()}
    host_key = host_key.lower()
    for port_key in (_HOST_KEY_SUFFIX_RE.sub("port", host_key), "port"):
        value = lowered.get(port_key)
        if not isinstance(value, int | str) or isinstance(value, bool) or port_key == host_key:
            continue
        with suppress(ValueError):
            if 0 < (port := int(value)) < 65536:
                return port
    return None


def _bare_host_endpoints(value: str, sibling_port: int | None) -> tuple[tuple[str, int], ...]:
    """Return the endpoints of a bare host or IP, optionally with a port, else none."""
    if _parse_ip(value) is not None:
        hostname, port = value.lower(), None
    else:
        try:
            parsed = urlsplit(f"//{value}")
            parsed_host, port = parsed.hostname, parsed.port
        except ValueError:
            return ()
        if (
            not parsed_host
            or parsed.username is not None
            or parsed.path
            or parsed.query
            or parsed.fragment
            or not (_HOSTNAME_RE.match(parsed_host) or _parse_ip(parsed_host) is not None)
        ):
            return ()
        hostname = parsed_host
    if port := port or sibling_port:
        return ((hostname, port),)
    return (hostname, 80), (hostname, 443)


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
