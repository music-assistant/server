"""Setup flow for the Youtube Music provider."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import ConfigEntryType

from music_assistant.constants import CONF_USERNAME
from music_assistant.models.setup_flow import SetupFlowError
from music_assistant.providers.ytmusic import (
    CONF_COOKIE,
    CONF_PO_TOKEN_SERVER_URL,
    DEFAULT_PO_TOKEN_SERVER_URL,
)

if TYPE_CHECKING:
    from music_assistant.models.setup_flow import SetupSession

_ENTRIES = (
    ConfigEntry(key=CONF_USERNAME, type=ConfigEntryType.STRING, required=True),
    ConfigEntry(
        key=CONF_COOKIE,
        type=ConfigEntryType.SECURE_STRING,
        required=True,
    ),
    ConfigEntry(
        key=CONF_PO_TOKEN_SERVER_URL,
        type=ConfigEntryType.STRING,
        default_value=DEFAULT_PO_TOKEN_SERVER_URL,
        required=True,
    ),
)


async def run_setup(session: SetupSession) -> None:
    """Run the setup flow: collect the credentials and create the provider."""
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
            errors = {"base": str(err)}
