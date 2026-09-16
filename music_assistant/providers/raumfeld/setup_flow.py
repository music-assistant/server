"""Setup flow for the Teufel Raumfeld provider."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import TYPE_CHECKING

import hassfeld
from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import ConfigEntryType

from music_assistant.constants import CONF_IP_ADDRESS, CONF_PORT
from music_assistant.models.setup_flow import SetupFlowError
from music_assistant.providers.raumfeld import DEFAULT_PORT, HOST_ERRORS

if TYPE_CHECKING:
    from music_assistant.models.setup_flow import SetupSession

# how long (seconds) to wait for the host validation during setup
_VALIDATE_TIMEOUT = 10

_ENTRIES = (
    ConfigEntry(key=CONF_IP_ADDRESS, type=ConfigEntryType.STRING, required=True),
    ConfigEntry(
        key=CONF_PORT,
        type=ConfigEntryType.INTEGER,
        required=False,
        advanced=True,
        default_value=DEFAULT_PORT,
    ),
)


async def run_setup(session: SetupSession) -> None:
    """
    Run the setup flow: collect and validate the Raumfeld host connection details.

    :param session: The active setup session driving the config form.
    """
    errors: dict[str, str] | None = None
    setup_data = dict(session.context.setup_data)
    while True:
        entries = [
            replace(entry, value=setup_data.get(entry.key, entry.value)) for entry in _ENTRIES
        ]
        submitted = await session.form(entries, step_id="user", errors=errors, last_step=True)
        setup_data.update(submitted)
        host = str(setup_data.get(CONF_IP_ADDRESS) or "").strip()
        port = int(setup_data.get(CONF_PORT) or DEFAULT_PORT)
        if not await _host_reachable(session, host, port):
            errors = {CONF_IP_ADDRESS: f"No Raumfeld host found at {host}:{port}"}
            continue
        try:
            await session.finish(setup_data)
            return
        except SetupFlowError as err:
            errors = {"base": err.translation_key or str(err)}


async def _host_reachable(session: SetupSession, host: str, port: int) -> bool:
    """Return whether a valid Raumfeld host answers at the given address."""
    raumfeld_host = hassfeld.RaumfeldHost(host, port, session=session.mass.http_session)
    try:
        async with asyncio.timeout(_VALIDATE_TIMEOUT):
            return bool(await raumfeld_host.async_host_is_valid())
    except HOST_ERRORS:
        return False
