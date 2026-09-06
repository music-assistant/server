"""Setup flow for the AmpliPi provider."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from typing import TYPE_CHECKING

from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import ConfigEntryType
from zeroconf.asyncio import AsyncServiceInfo

from music_assistant.helpers.util import get_primary_ip_address_from_zeroconf
from music_assistant.models.setup_flow import SetupFlowError
from music_assistant.providers.amplipi.constants import CONF_HOST, DEFAULT_HOST, MDNS_TYPE

if TYPE_CHECKING:
    from music_assistant.models.setup_flow import SetupSession

LOGGER = logging.getLogger(__name__)

# how long the form waits for an AmpliPi to answer on mDNS before falling back to the
# default hostname; kept short so setup does not appear to hang on a network without one.
_DISCOVERY_TIMEOUT = 3.0
# how often the mDNS cache is re-checked while waiting for the browser to fill it
_DISCOVERY_POLL_INTERVAL = 0.25

_ENTRIES = (
    ConfigEntry(
        key=CONF_HOST,
        type=ConfigEntryType.STRING,
        required=True,
    ),
)


async def run_setup(session: SetupSession) -> None:
    """Run the setup flow: collect the connection details and create the provider."""
    errors: dict[str, str] | None = None
    setup_data = dict(session.context.setup_data)
    if CONF_HOST not in setup_data:
        setup_data[CONF_HOST] = await _discover_host(session)
    while True:
        entries = [
            replace(entry, value=setup_data.get(entry.key, entry.value)) for entry in _ENTRIES
        ]
        submitted = await session.form(entries, step_id="user", errors=errors, last_step=True)
        setup_data.update(submitted)
        try:
            await session.finish(setup_data)
            return
        except SetupFlowError as err:
            errors = {"base": err.translation_key or str(err)}


async def _discover_host(session: SetupSession) -> str:
    """Return the address to prefill the host field with."""
    discovery_info = await _find_amplipi(session)
    if discovery_info is None:
        LOGGER.debug("No %s service found on mDNS, offering %s", MDNS_TYPE, DEFAULT_HOST)
        return DEFAULT_HOST
    if hostname := (discovery_info.server or "").rstrip("."):
        LOGGER.debug("Discovered AmpliPi at %s", hostname)
        return hostname
    # a record without a hostname still carries addresses to fall back on
    address = get_primary_ip_address_from_zeroconf(discovery_info)
    host = _as_url_host(address) if address else DEFAULT_HOST
    LOGGER.debug("Discovered AmpliPi advertising no hostname, offering %s", host)
    return host


async def _find_amplipi(session: SetupSession) -> AsyncServiceInfo | None:
    """
    Return the mDNS record of an AmpliPi on the network, or None if none answers.

    The instance name carries the controller's MAC, so any instance of the AmpliPi
    service type is accepted rather than one fixed name. The provider manifest subscribes
    to that type, so the shared browser is normally already filling the cache; the poll
    covers a cache that is still cold when setup is opened.
    """
    loop = asyncio.get_running_loop()
    zeroconf = session.mass.discovery.aiozc.zeroconf
    deadline = loop.time() + _DISCOVERY_TIMEOUT
    while True:
        for mdns_name in set(zeroconf.cache.cache):
            if not mdns_name.endswith(MDNS_TYPE) or mdns_name == MDNS_TYPE:
                continue
            # spend only what is left of the budget, so a stale record that no longer
            # answers cannot extend the wait past _DISCOVERY_TIMEOUT
            remaining = deadline - loop.time()
            if remaining <= 0:
                return None
            info = AsyncServiceInfo(MDNS_TYPE, mdns_name)
            if await info.async_request(zeroconf, remaining * 1000):
                return info
        if loop.time() >= deadline:
            return None
        await asyncio.sleep(_DISCOVERY_POLL_INTERVAL)


def _as_url_host(address: str) -> str:
    """
    Return an address in the form a URL can carry.

    The provider builds its endpoint as "http://<host>/api", which an IPv6 literal only
    survives in brackets.

    :param address: The address discovered over mDNS.
    """
    return f"[{address}]" if ":" in address else address
