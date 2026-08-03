"""Setup flow for the OpenAI Compatible provider."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import ConfigEntryType
from music_assistant_models.errors import LoginFailed, MusicAssistantError

from music_assistant.models.setup_flow import SetupFlowError
from music_assistant.providers.openai_compatible.constants import (
    CONF_API_KEY,
    CONF_BASE_URL,
    DEFAULT_BASE_URL,
    MODELS_REQUEST_TIMEOUT,
)
from music_assistant.providers.openai_compatible.helpers import list_models

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigValueType

    from music_assistant.models.setup_flow import SetupSession

_ENTRIES = (
    ConfigEntry(
        key=CONF_BASE_URL,
        type=ConfigEntryType.STRING,
        required=True,
        default_value=DEFAULT_BASE_URL,
    ),
    ConfigEntry(key=CONF_API_KEY, type=ConfigEntryType.SECURE_STRING, required=False),
)

CONF_CLEAR_API_KEY = "clear_api_key"


async def run_setup(session: SetupSession) -> None:
    """Run the setup flow: collect the endpoint details and create the provider."""
    errors: dict[str, str] | None = None
    setup_data = dict(session.context.setup_data)
    while True:
        entries = [
            replace(entry, value=setup_data.get(entry.key, entry.value)) for entry in _ENTRIES
        ]
        if setup_data.get(CONF_API_KEY):
            entries.append(
                ConfigEntry(
                    key=CONF_CLEAR_API_KEY,
                    type=ConfigEntryType.BOOLEAN,
                    required=False,
                    default_value=False,
                )
            )
        submitted = await session.form(entries, step_id="user", errors=errors, last_step=True)
        if submitted.pop(CONF_CLEAR_API_KEY, False):
            setup_data[CONF_API_KEY] = ""
            submitted.pop(CONF_API_KEY, None)
        elif not submitted.get(CONF_API_KEY):
            # the stored key is never sent to the client, so an empty field on a
            # reconfigure means "keep the current one"; clearing it is explicit
            submitted.pop(CONF_API_KEY, None)
        setup_data.update(submitted)
        if error := await _probe(session, setup_data):
            errors = {"base": error}
            continue
        try:
            await session.finish(setup_data)
            return
        except SetupFlowError as err:
            errors = {"base": err.translation_key or str(err)}


async def _probe(session: SetupSession, setup_data: dict[str, ConfigValueType]) -> str | None:
    """Return the error slug for an endpoint that cannot be used, None when it can."""
    base_url = str(setup_data.get(CONF_BASE_URL) or "").strip().rstrip("/")
    api_key = str(setup_data.get(CONF_API_KEY) or "").strip()
    try:
        # an endpoint without a model listing still answers here, so only a refused
        # key or an unreachable host stops the setup
        await list_models(session.mass, base_url, api_key, MODELS_REQUEST_TIMEOUT)
    except LoginFailed:
        return "invalid_api_key"
    except MusicAssistantError:
        return "cannot_connect"
    return None
