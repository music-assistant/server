"""Setup flow for the WebDAV filesystem provider."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption
from music_assistant_models.enums import ConfigEntryType

from music_assistant.constants import CONF_PASSWORD, CONF_USERNAME
from music_assistant.models.setup_flow import SetupFlowError

from .constants import CONF_CONTENT_TYPE, CONF_URL, CONF_VERIFY_SSL

if TYPE_CHECKING:
    from music_assistant.models.setup_flow import SetupSession

_ENTRIES = (
    ConfigEntry(
        key=CONF_CONTENT_TYPE,
        type=ConfigEntryType.STRING,
        options=[
            ConfigValueOption("music"),
            ConfigValueOption("audiobooks"),
            ConfigValueOption("podcasts"),
        ],
        default_value="music",
    ),
    ConfigEntry(key=CONF_URL, type=ConfigEntryType.STRING, required=True),
    ConfigEntry(key=CONF_USERNAME, type=ConfigEntryType.STRING, required=False),
    ConfigEntry(key=CONF_PASSWORD, type=ConfigEntryType.SECURE_STRING, required=False),
    ConfigEntry(key=CONF_VERIFY_SSL, type=ConfigEntryType.BOOLEAN, default_value=False),
)


async def run_setup(session: SetupSession) -> None:
    """Run the setup flow: collect the WebDAV connection details and create the provider."""
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
