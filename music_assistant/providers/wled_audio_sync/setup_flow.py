"""Setup flow for the WLED Audio Sync provider: picks the zone port."""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import ConfigEntryType

from music_assistant.constants import CONF_PORT
from music_assistant.models.setup_flow import SetupFlowError

from .constants import DEFAULT_PORT

if TYPE_CHECKING:
    from music_assistant.models.setup_flow import SetupSession


async def run_setup(session: SetupSession) -> None:
    """
    Run the WLED sync zone setup flow.

    :param session: The setup session driving the flow.
    """
    suggested_port = session.context.setup_data.get(CONF_PORT) or await _next_free_port(session)
    errors: dict[str, str] | None = None
    while True:
        values = await session.form(
            [
                ConfigEntry(
                    key=CONF_PORT,
                    type=ConfigEntryType.INTEGER,
                    default_value=suggested_port,
                    range=(1024, 65535),
                )
            ],
            step_id="user",
            errors=errors,
            last_step=True,
        )
        try:
            await session.finish(values)
            return
        except SetupFlowError as err:
            errors = {"base": err.translation_key or str(err)}


async def _next_free_port(session: SetupSession) -> int:
    """Return the lowest zone port not used by another WLED instance."""
    siblings = await session.mass.config.get_provider_configs(
        provider_domain=session.context.domain
    )
    used_ports = {
        session.mass.config.get_provider_setup_value(sibling.instance_id, CONF_PORT)
        for sibling in siblings
    }
    port = DEFAULT_PORT
    while port in used_ports:
        port += 1
    return port
