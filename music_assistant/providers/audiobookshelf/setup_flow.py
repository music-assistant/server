"""Setup flow for the Audiobookshelf provider."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import ConfigEntryType

from music_assistant.models.setup_flow import SetupFlowError
from music_assistant.providers.audiobookshelf.constants import (
    CONF_API_TOKEN,
    CONF_OLD_TOKEN,
    CONF_PASSWORD,
    CONF_URL,
    CONF_USERNAME,
    CONF_VERIFY_SSL,
)

if TYPE_CHECKING:
    from music_assistant.models.setup_flow import SetupSession

_ENTRIES = (
    ConfigEntry(
        key="label",
        type=ConfigEntryType.LABEL,
    ),
    ConfigEntry(
        key=CONF_URL,
        type=ConfigEntryType.STRING,
        required=True,
    ),
    ConfigEntry(
        key=CONF_USERNAME,
        type=ConfigEntryType.STRING,
        required=False,
    ),
    ConfigEntry(
        key=CONF_PASSWORD,
        type=ConfigEntryType.SECURE_STRING,
        required=False,
    ),
    ConfigEntry(
        key=CONF_API_TOKEN,
        type=ConfigEntryType.SECURE_STRING,
        required=False,
    ),
    ConfigEntry(
        key=CONF_OLD_TOKEN,
        type=ConfigEntryType.SECURE_STRING,
        required=False,
        hidden=True,
    ),
    ConfigEntry(
        key=CONF_VERIFY_SSL,
        type=ConfigEntryType.BOOLEAN,
        required=False,
        advanced=True,
        default_value=True,
    ),
)


async def run_setup(session: SetupSession) -> None:
    """Run the setup flow: collect the connection details and create the provider."""
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
