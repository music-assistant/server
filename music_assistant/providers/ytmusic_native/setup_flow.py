"""Setup flow for the native YouTube Music provider."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import ConfigEntryType

from music_assistant.models.setup_flow import SetupFlowError

from .constants import CONF_COOKIE, CONF_KEEP_SESSION_ALIVE

if TYPE_CHECKING:
    from music_assistant.models.setup_flow import SetupSession

# Keep-alive is asked here rather than in the options: the provider refreshes the
# cookie on its first load already, before the options could be changed.
_ENTRIES = (
    ConfigEntry(key=CONF_COOKIE, type=ConfigEntryType.SECURE_STRING, required=True),
    ConfigEntry(key=CONF_KEEP_SESSION_ALIVE, type=ConfigEntryType.BOOLEAN, default_value=True),
)


async def run_setup(session: SetupSession) -> None:
    """Collect the session cookie and create the provider (which verifies it)."""
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
