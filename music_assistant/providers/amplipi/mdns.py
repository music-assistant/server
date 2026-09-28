"""mDNS helpers for the AmpliPi provider: find controllers and tie a host to one of them."""

from __future__ import annotations

from typing import TYPE_CHECKING
from urllib.parse import urlparse

from zeroconf.asyncio import AsyncServiceInfo

from music_assistant.helpers.util import format_ip_for_url, get_primary_ip_address_from_zeroconf

from .constants import CONF_MDNS_NAME, DOMAIN, MDNS_TYPE

if TYPE_CHECKING:
    from music_assistant import MusicAssistant

_RESOLVE_TIMEOUT_MS = 1000


async def discovered_controllers(mass: MusicAssistant) -> list[AsyncServiceInfo]:
    """
    Return every AmpliPi controller currently reachable on the network.

    :param mass: The MusicAssistant instance.
    """
    controllers: list[AsyncServiceInfo] = []
    zeroconf = mass.discovery.aiozc.zeroconf
    for mdns_name in sorted(set(zeroconf.cache.cache)):
        if not mdns_name.endswith(MDNS_TYPE) or mdns_name == MDNS_TYPE:
            continue
        info = AsyncServiceInfo(MDNS_TYPE, mdns_name)
        if await info.async_request(zeroconf, _RESOLVE_TIMEOUT_MS):
            controllers.append(info)
    return controllers


def controller_host(info: AsyncServiceInfo) -> str | None:
    """
    Return the address to reach the given controller on, or None if it advertises none.

    :param info: The controller's resolved mDNS record.
    """
    if hostname := (info.server or "").rstrip("."):
        return hostname
    if address := get_primary_ip_address_from_zeroconf(info):
        return format_ip_for_url(address)
    return None


def controller_matches_host(info: AsyncServiceInfo, host: str) -> bool:
    """
    Return whether the given controller is the one a configured host points at.

    :param info: The controller's resolved mDNS record.
    :param host: The host as entered during setup (a bare host, host:port or full URL).
    """
    if not (hostname := _hostname_of(host)):
        return False
    if hostname == (info.server or "").rstrip(".").lower():
        return True
    return hostname in {address.lower() for address in info.parsed_addresses()}


def controller_id(info: AsyncServiceInfo) -> str:
    """
    Return the identity to record for the given controller.

    :param info: The controller's resolved mDNS record.
    """
    # the mDNS name carries the MAC; lowercased as live callbacks and the cache differ in case
    return info.name.lower()


def claimed_controllers(mass: MusicAssistant, own_instance_id: str | None) -> set[str]:
    """
    Return the ids (see controller_id) of the controllers other AmpliPi instances are set up for.

    :param mass: The MusicAssistant instance.
    :param own_instance_id: The instance being (re)configured, excluded from the result.
    """
    claimed: set[str] = set()
    for instance_id, conf in mass.config.get("providers", {}).items():
        if conf.get("domain") != DOMAIN or instance_id == own_instance_id:
            continue
        if mdns_name := mass.config.get_provider_setup_value(instance_id, CONF_MDNS_NAME):
            claimed.add(str(mdns_name).lower())
    return claimed


def _hostname_of(host: str) -> str | None:
    """Return the lowercased hostname part of a bare host, host:port or full URL."""
    url = host if "://" in host else f"//{host}"
    try:
        hostname = urlparse(url).hostname
    except ValueError:
        return None
    return hostname.lower() if hostname else None
