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
from music_assistant_models.errors import SetupFailedError

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
    used_ports = {_port_from_config(sibling) for sibling in siblings}
    port = DEFAULT_PORT
    while port in used_ports:
        port += 1
    return port


async def run_setup(session: SetupSession) -> None:
    """Run the setup flow: let the user confirm or change the auto-suggested zone port."""
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
        try:
            await session.finish(submitted)
            return
        except SetupFlowError as err:
            errors = {"base": err.translation_key or str(err)}
        except SetupFailedError as err:
            # Raised by handle_async_init's duplicate-port check -- reachable if
            # another instance claims the suggested port between the scan above
            # and this submit (race condition), not just the framework's own
            # SetupFlowError family.
            errors = {"base": str(err)}
