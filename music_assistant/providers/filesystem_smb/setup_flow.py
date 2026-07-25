"""Setup flow for the SMB filesystem provider."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption
from music_assistant_models.enums import ConfigEntryType

from music_assistant.constants import CONF_PASSWORD, CONF_USERNAME
from music_assistant.models.setup_flow import SetupFlowError
from music_assistant.providers.filesystem_local.constants import CONF_ENTRY_CONTENT_TYPE
from music_assistant.providers.filesystem_smb import (
    CONF_HOST,
    CONF_SHARE,
    CONF_SMB_VERSION,
    CONF_SUBFOLDER,
)

if TYPE_CHECKING:
    from music_assistant.models.setup_flow import SetupSession

_ENTRIES = (
    CONF_ENTRY_CONTENT_TYPE,
    ConfigEntry(key=CONF_HOST, type=ConfigEntryType.STRING, required=True),
    ConfigEntry(key=CONF_SHARE, type=ConfigEntryType.STRING, required=True),
    ConfigEntry(
        key=CONF_USERNAME, type=ConfigEntryType.STRING, required=False, default_value="guest"
    ),
    ConfigEntry(
        key=CONF_PASSWORD, type=ConfigEntryType.SECURE_STRING, required=False, default_value=None
    ),
    ConfigEntry(key=CONF_SUBFOLDER, type=ConfigEntryType.STRING, required=False, default_value=""),
    ConfigEntry(
        key=CONF_SMB_VERSION,
        type=ConfigEntryType.STRING,
        required=False,
        advanced=True,
        default_value="3.0",
        options=[
            ConfigValueOption(""),
            ConfigValueOption("1.0"),
            ConfigValueOption("2.0"),
            ConfigValueOption("2.1"),
            ConfigValueOption("3.0"),
            ConfigValueOption("3.1.1"),
        ],
    ),
)


async def run_setup(session: SetupSession) -> None:
    """Run the setup flow: collect the SMB connection details and create the provider."""
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
