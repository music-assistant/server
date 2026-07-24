"""Setup flow for the nicovideo provider."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from music_assistant.models.setup_flow import SetupFlowError
from music_assistant.providers.nicovideo.config.factory import get_setup_config_entries

if TYPE_CHECKING:
    from music_assistant.models.setup_flow import SetupSession


async def run_setup(session: SetupSession) -> None:
    """Run the setup flow: collect the account credentials and create the provider."""
    errors: dict[str, str] | None = None
    setup_data = dict(session.context.setup_data)
    base_entries = get_setup_config_entries()
    while True:
        entries = [
            replace(entry, value=setup_data.get(entry.key, entry.value)) for entry in base_entries
        ]
        submitted = await session.form(entries, step_id="user", errors=errors, last_step=True)
        setup_data.update(submitted)
        try:
            await session.finish(setup_data)
            return
        except SetupFlowError as err:
            errors = {"base": str(err)}
