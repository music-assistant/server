"""Setup flow for the OpenAI Compatible provider."""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption
from music_assistant_models.enums import ConfigEntryType
from music_assistant_models.errors import LoginFailed, MusicAssistantError

from music_assistant.models.setup_flow import SetupFlowError
from music_assistant.providers.openai_compatible.constants import (
    CONF_API_KEY,
    CONF_BASE_URL,
    CONF_SERVICE,
    DEFAULT_SERVICE,
    MODELS_REQUEST_TIMEOUT,
    SERVICE_BASE_URLS,
    SERVICE_CUSTOM,
)
from music_assistant.providers.openai_compatible.helpers import list_models

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigValueType

    from music_assistant.models.setup_flow import SetupSession

CONF_CLEAR_API_KEY = "clear_api_key"


async def run_setup(session: SetupSession) -> None:
    """Run the setup flow: pick a service, collect its details and create the provider."""
    setup_data = dict(session.context.setup_data)
    service = await _select_service(session, setup_data)
    errors: dict[str, str] | None = None
    while True:
        submitted = await session.form(
            _connection_entries(setup_data, service),
            step_id="connection",
            errors=errors,
            last_step=True,
        )
        if submitted.pop(CONF_CLEAR_API_KEY, False):
            setup_data[CONF_API_KEY] = ""
            submitted.pop(CONF_API_KEY, None)
        elif not submitted.get(CONF_API_KEY):
            # the stored key is never sent to the client, so an empty field on a
            # reconfigure means "keep the current one"; clearing it is explicit
            submitted.pop(CONF_API_KEY, None)
        setup_data.update(submitted)
        setup_data[CONF_SERVICE] = service
        if error := await _probe(session, setup_data):
            errors = {"base": error}
            continue
        try:
            await session.finish(setup_data)
            return
        except SetupFlowError as err:
            errors = {"base": err.translation_key or str(err)}


async def _select_service(session: SetupSession, setup_data: dict[str, ConfigValueType]) -> str:
    """Return the service the user picked for this instance."""
    submitted = await session.form(
        [
            ConfigEntry(
                key=CONF_SERVICE,
                type=ConfigEntryType.STRING,
                required=True,
                default_value=DEFAULT_SERVICE,
                value=str(setup_data.get(CONF_SERVICE) or DEFAULT_SERVICE),
                options=[
                    ConfigValueOption(service) for service in (*SERVICE_BASE_URLS, SERVICE_CUSTOM)
                ],
            )
        ],
        step_id="service",
    )
    return str(submitted[CONF_SERVICE])


def _connection_entries(setup_data: dict[str, ConfigValueType], service: str) -> list[ConfigEntry]:
    """Return the entries collecting the endpoint details for the chosen service."""
    stored_service = str(setup_data.get(CONF_SERVICE) or "")
    if service != stored_service and service in SERVICE_BASE_URLS:
        # switching service, so the address that belongs to it wins over the stored one
        base_url = SERVICE_BASE_URLS[service]
    else:
        base_url = str(setup_data.get(CONF_BASE_URL) or SERVICE_BASE_URLS.get(service, ""))
    entries = [
        ConfigEntry(
            key=CONF_BASE_URL,
            type=ConfigEntryType.STRING,
            required=True,
            value=base_url,
        ),
        ConfigEntry(key=CONF_API_KEY, type=ConfigEntryType.SECURE_STRING, required=False),
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
    return entries


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
