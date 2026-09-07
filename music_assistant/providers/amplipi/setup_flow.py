"""Setup flow for the AmpliPi provider."""

from __future__ import annotations

import logging
from dataclasses import replace
from typing import TYPE_CHECKING

from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import ConfigEntryType

from music_assistant.helpers.util import get_primary_ip_address_from_zeroconf
from music_assistant.models.setup_flow import SetupFlowError
from music_assistant.providers.amplipi.constants import CONF_HOST, DEFAULT_HOST, MDNS_TYPE

if TYPE_CHECKING:
    from music_assistant.models.setup_flow import SetupSession

LOGGER = logging.getLogger(__name__)

# how long the form waits for an AmpliPi to answer on mDNS before falling back to the
# default hostname; kept short so setup does not appear to hang on a network without one.
_DISCOVERY_TIMEOUT = 3.0

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
    # the instance name carries the controller's MAC, so any instance of the service type
    # is accepted rather than one fixed name
    discovery_info = await session.mass.discovery.async_find_mdns_service(
        MDNS_TYPE, timeout=_DISCOVERY_TIMEOUT
    )
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


def _as_url_host(address: str) -> str:
    """
    Return an address in the form a URL can carry.

    The provider builds its endpoint as "http://<host>/api", which an IPv6 literal only
    survives in brackets.

    :param address: The address discovered over mDNS.
    """
    return f"[{address}]" if ":" in address else address
