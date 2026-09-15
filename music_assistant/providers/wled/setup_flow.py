"""
Setup flow for the WLED Audio Sync provider.

Picks a free zone port *before* the instance is created. Without this, a new
instance would always start at the hardcoded default port (see
provider.py's get_config_entries()), colliding with any existing WLED
instance and getting rejected by handle_async_init's duplicate-port check
before the user ever gets a chance to change it -- making it impossible to
add a second zone through the normal "add provider" flow.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import ConfigEntryType

from music_assistant.helpers.util import try_parse_int
from music_assistant.models.setup_flow import SetupFlowError

from .constants import CONF_PORT, DEFAULT_PORT
from .provider import _port_from_config

if TYPE_CHECKING:
    from music_assistant.models.setup_flow import SetupSession


async def _next_free_port(session: SetupSession) -> int:
    """Return the lowest zone port >= DEFAULT_PORT not already used by another WLED instance."""
    siblings = await session.mass.config.get_provider_configs(
        provider_domain="wled", include_values=True
    )
    used_ports = {_port_from_config(session.mass, sibling) for sibling in siblings}
    port = DEFAULT_PORT
    while port in used_ports:
        port += 1
    return port


async def run_setup(session: SetupSession) -> None:
    """Run the setup flow: let the user confirm or change the auto-suggested zone port."""
    if session.context.kind == "reconfigure":
        # Keep the zone's existing port by default -- scanning for a new free port
        # here (as a fresh setup does) would preselect a different port than the one
        # this instance already uses, since that scan counts the instance's own
        # current port as "used". Mirrors _port_from_config's own precedence: an
        # override in values wins over the port chosen at initial setup_data.
        stored_port = session.context.values.get(CONF_PORT)
        if stored_port is None:
            stored_port = session.context.setup_data.get(CONF_PORT, DEFAULT_PORT)
        suggested_port = try_parse_int(stored_port, DEFAULT_PORT) or DEFAULT_PORT
    else:
        suggested_port = await _next_free_port(session)
    entries = [
        ConfigEntry(
            key=CONF_PORT,
            type=ConfigEntryType.INTEGER,
            default_value=suggested_port,
            range=(1024, 65535),
        ),
    ]
    errors: dict[str, str] | None = None
    while True:
        submitted = await session.form(entries, step_id="user", errors=errors, last_step=True)
        # session.finish() only ever persists into setup_data, but _port_from_config
        # prefers a "values" override over setup_data (see ConfigController.
        # set_raw_provider_config_value) -- and finish() *also* reloads the provider
        # for a reconfigure, before we would otherwise get a chance to update that
        # override. So apply it first: reloading then reads the new port, not a stale
        # one, and this avoids a second, separate reload just to pick the fix up.
        applied_port_override_instance_id: str | None = None
        previous_port_override = session.context.values.get(CONF_PORT)
        if session.context.kind == "reconfigure" and previous_port_override is not None:
            assert session.context.instance_id is not None  # always set for reconfigure
            applied_port_override_instance_id = session.context.instance_id
            session.mass.config.set_raw_provider_config_value(
                session.context.instance_id,
                CONF_PORT,
                submitted.get(CONF_PORT, suggested_port),
            )
        finished = False
        try:
            await session.finish(submitted)
            finished = True
        except SetupFlowError as err:
            # The engine wraps every finish-time failure (including
            # handle_async_init's duplicate-port check -- reachable if another
            # instance claims the suggested port between the scan above and
            # this submit) in SetupFlowError before it reaches this coroutine.
            errors = {"base": err.translation_key or str(err)}
        finally:
            if not finished and applied_port_override_instance_id is not None:
                # finish() failed (or was cancelled) after we already wrote the new
                # port above -- put the override back so a rejected/aborted submit
                # doesn't leave the port pointed at a value that was never accepted.
                session.mass.config.set_raw_provider_config_value(
                    applied_port_override_instance_id, CONF_PORT, previous_port_override
                )
        if finished:
            return
