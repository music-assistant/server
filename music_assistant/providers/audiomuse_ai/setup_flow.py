"""Setup flow for the AudioMuse-AI plugin."""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption
from music_assistant_models.enums import ConfigEntryType, ProviderType

from music_assistant.models.setup_flow import SetupFlowError
from music_assistant.providers.audiomuse_ai.constants import (
    CONF_API_TOKEN,
    CONF_BASE_URL,
    CONF_MEDIA_PROVIDER,
    LIBRARY_DOMAIN,
)

if TYPE_CHECKING:
    from music_assistant.models.setup_flow import SetupSession


async def run_setup(session: SetupSession) -> None:
    """Run the setup flow: collect the server connection and the media provider to map."""
    errors: dict[str, str | SetupFlowError] | None = None
    setup_data = dict(session.context.setup_data)
    provider_options = [
        ConfigValueOption(title=prov.name, value=prov.instance_id)
        for prov in session.mass.get_providers(ProviderType.MUSIC)
        if prov.domain != LIBRARY_DOMAIN
    ]
    while True:
        entries = [
            ConfigEntry(
                key=CONF_BASE_URL,
                type=ConfigEntryType.STRING,
                required=True,
                value=setup_data.get(CONF_BASE_URL),
            ),
            ConfigEntry(
                key=CONF_API_TOKEN,
                type=ConfigEntryType.SECURE_STRING,
                required=False,
                value=setup_data.get(CONF_API_TOKEN),
            ),
            ConfigEntry(
                key=CONF_MEDIA_PROVIDER,
                type=ConfigEntryType.STRING,
                required=True,
                options=provider_options,
                value=setup_data.get(CONF_MEDIA_PROVIDER),
            ),
        ]
        submitted = await session.form(entries, step_id="user", errors=errors, last_step=True)
        setup_data.update(submitted)
        try:
            await session.finish(setup_data)
            return
        except SetupFlowError as err:
            errors = {"base": err}
