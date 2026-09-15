"""Setup flow for the Teufel Raumfeld provider."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import ConfigEntryType

from music_assistant.models.setup_flow import SetupFlowError
from music_assistant.providers.raumfeld import CONF_HOST, CONF_PORT, DEFAULT_PORT

if TYPE_CHECKING:
    from music_assistant.models.setup_flow import SetupSession

_ENTRIES = (
    ConfigEntry(key=CONF_HOST, type=ConfigEntryType.STRING, required=True),
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
    Run the setup flow: collect the Raumfeld host connection details.

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
        try:
            await session.finish(setup_data)
            return
        except SetupFlowError as err:
            errors = {"base": err.translation_key or str(err)}
