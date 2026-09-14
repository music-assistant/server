"""Setup flow for the AmpliPi provider."""

from __future__ import annotations

import logging
from dataclasses import replace
from typing import TYPE_CHECKING

from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import ConfigEntryType

from music_assistant.models.setup_flow import SetupFlowError
from music_assistant.providers.amplipi.constants import (
    CONF_HOST,
    CONF_MDNS_NAME,
    DEFAULT_HOST,
    MDNS_TYPE,
)
from music_assistant.providers.amplipi.mdns import (
    claimed_controllers,
    controller_host,
    controller_id,
    controller_matches_host,
    discovered_controllers,
)

if TYPE_CHECKING:
    from zeroconf.asyncio import AsyncServiceInfo

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
    controllers = await _discover_controllers(session)
    claimed = claimed_controllers(session.mass, session.context.instance_id)
    if CONF_HOST not in setup_data:
        setup_data[CONF_HOST] = _host_to_offer(controllers, claimed)
    while True:
        entries = [
            replace(entry, value=setup_data.get(entry.key, entry.value)) for entry in _ENTRIES
        ]
        submitted = await session.form(entries, step_id="user", errors=errors, last_step=True)
        setup_data.update(submitted)
        host = str(setup_data[CONF_HOST])
        controller = next((c for c in controllers if controller_matches_host(c, host)), None)
        if controller is not None and controller_id(controller) in claimed:
            errors = {CONF_HOST: "already_configured"}
            continue
        if controller is not None:
            setup_data[CONF_MDNS_NAME] = controller_id(controller)
        else:
            setup_data.pop(CONF_MDNS_NAME, None)
        try:
            await session.finish(setup_data)
            return
        except SetupFlowError as err:
            errors = {"base": err.translation_key or str(err)}


async def _discover_controllers(session: SetupSession) -> list[AsyncServiceInfo]:
    """Return the AmpliPi controllers on the network, waiting briefly for a first answer."""
    if await session.mass.discovery.async_find_mdns_service(MDNS_TYPE, timeout=_DISCOVERY_TIMEOUT):
        return await discovered_controllers(session.mass)
    LOGGER.debug("No %s service found on mDNS", MDNS_TYPE)
    return []


def _host_to_offer(controllers: list[AsyncServiceInfo], claimed: set[str]) -> str:
    """
    Return the address to prefill the host field with.

    This is the first controller no other instance is set up for, an empty string when
    every controller is taken, or the default hostname when none was discovered.

    :param controllers: The controllers discovered on the network.
    :param claimed: Ids of the controllers other instances are set up for.
    """
    if not controllers:
        LOGGER.debug("No AmpliPi discovered, offering %s", DEFAULT_HOST)
        return DEFAULT_HOST
    for controller in controllers:
        if controller_id(controller) in claimed:
            continue
        if host := controller_host(controller):
            LOGGER.debug("Discovered AmpliPi %s at %s", controller.name, host)
            return host
    LOGGER.debug("Every discovered AmpliPi is already set up, offering no host")
    return ""
