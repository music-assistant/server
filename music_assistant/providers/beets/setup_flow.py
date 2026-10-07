"""Setup flow for the beets music provider."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from music_assistant.models.setup_flow import SetupFlowError

from .constants import CONF_ENTRY_BEETS_DIRECTORY, CONF_ENTRY_LIBRARY_DB, CONF_ENTRY_MUSIC_DIRECTORY

if TYPE_CHECKING:
    from music_assistant.models.setup_flow import SetupSession

_ENTRIES = (CONF_ENTRY_LIBRARY_DB, CONF_ENTRY_MUSIC_DIRECTORY, CONF_ENTRY_BEETS_DIRECTORY)


async def run_setup(session: SetupSession) -> None:
    """
    Collect where the beets library lives, then create the provider.

    :param session: The setup flow session used to interact with the user.
    """
    errors: dict[str, str | SetupFlowError] | None = None
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
            errors = {"base": err}
