"""Setup flow for the Deezer provider."""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import ConfigEntryType

from music_assistant.models.setup_flow import SetupFlowError
from music_assistant.providers.deezer.provider import CONF_ARL_TOKEN

if TYPE_CHECKING:
    from music_assistant.models.setup_flow import SetupSession

_ENTRIES = (ConfigEntry(key=CONF_ARL_TOKEN, type=ConfigEntryType.SECURE_STRING, required=True),)


async def run_setup(session: SetupSession) -> None:
    """Run the setup flow: collect the ARL token and create the provider."""
    errors: dict[str, str] | None = None
    setup_data = dict(session.context.setup_data)
    while True:
        submitted = await session.form(
            list(_ENTRIES), step_id="user", errors=errors, last_step=True
        )
        setup_data.update(submitted)
        try:
            await session.finish(setup_data)
            return
        except SetupFlowError as err:
            errors = {"base": str(err)}
