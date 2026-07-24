"""Setup flow for the NFS filesystem provider."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption
from music_assistant_models.enums import ConfigEntryType

from music_assistant.models.setup_flow import SetupFlowError
from music_assistant.providers.filesystem_local.constants import CONF_ENTRY_CONTENT_TYPE

from .constants import CONF_EXPORT_PATH, CONF_HOST, CONF_NFS_VERSION, CONF_SUBFOLDER

if TYPE_CHECKING:
    from music_assistant.models.setup_flow import SetupSession

_ENTRIES = (
    CONF_ENTRY_CONTENT_TYPE,
    ConfigEntry(key=CONF_HOST, type=ConfigEntryType.STRING, required=True),
    ConfigEntry(key=CONF_EXPORT_PATH, type=ConfigEntryType.STRING, required=True),
    ConfigEntry(key=CONF_SUBFOLDER, type=ConfigEntryType.STRING, required=False, default_value=""),
    ConfigEntry(
        key=CONF_NFS_VERSION,
        type=ConfigEntryType.STRING,
        required=False,
        advanced=True,
        default_value="",
        options=[
            ConfigValueOption(""),
            ConfigValueOption("3"),
            ConfigValueOption("4"),
            ConfigValueOption("4.1"),
            ConfigValueOption("4.2"),
        ],
    ),
)


async def run_setup(session: SetupSession) -> None:
    """Run the setup flow: collect the NFS connection details and create the provider."""
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
