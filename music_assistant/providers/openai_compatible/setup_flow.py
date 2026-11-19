"""Setup flow for the OpenAI Compatible provider."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption
from music_assistant_models.enums import ConfigEntryType
from music_assistant_models.errors import LoginFailed, MusicAssistantError

from music_assistant.models.setup_flow import SetupFlowError
from music_assistant.providers.openai_compatible import model_config_entries, parse_models
from music_assistant.providers.openai_compatible.constants import (
    CONF_API_KEY,
    CONF_BASE_URL,
    CONF_IMAGE_MODELS,
    CONF_MODELS,
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
    """Run the setup flow: pick a service, collect its details and models, create the provider."""
    setup_data = dict(session.context.setup_data)
    picks: dict[str, ConfigValueType] = {
        key: parse_models(session.context.values.get(key))
        for key in (CONF_MODELS, CONF_IMAGE_MODELS)
    }
    service = await _select_service(session, setup_data)
    errors: dict[str, str | SetupFlowError] | None = None
    while True:
        submitted = await session.form(
            _connection_entries(setup_data, service),
            step_id="connection",
            errors=errors,
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
        try:
            available = await _probe(session, setup_data)
            picks = await _select_models(session, available, picks)
            await session.finish(setup_data, picks)
            return
        except SetupFlowError as err:
            errors = {"base": err}


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


async def _probe(session: SetupSession, setup_data: dict[str, ConfigValueType]) -> list[str]:
    """Return the models the endpoint advertises, raising SetupFlowError when it can't be used."""
    base_url = str(setup_data.get(CONF_BASE_URL) or "").strip().rstrip("/")
    api_key = str(setup_data.get(CONF_API_KEY) or "").strip()
    try:
        # a mistyped address is the easiest thing to get wrong here, so anything short
        # of a real answer from the listing endpoint stops the setup
        return await list_models(session.mass, base_url, api_key, MODELS_REQUEST_TIMEOUT)
    except LoginFailed as err:
        raise SetupFlowError(str(err), "invalid_api_key") from err
    except MusicAssistantError as err:
        raise SetupFlowError(str(err), "cannot_connect") from err


async def _select_models(
    session: SetupSession, available: list[str], picks: dict[str, ConfigValueType]
) -> dict[str, ConfigValueType]:
    """
    Return the text and image models the user picked.

    :param session: The setup session driving the flow.
    :param available: The models the endpoint advertises.
    :param picks: The models picked so far, to prefill the form with.
    """
    entries = model_config_entries(
        available, parse_models(picks[CONF_MODELS]), parse_models(picks[CONF_IMAGE_MODELS])
    )
    return await session.form(
        [replace(entry, value=picks[entry.key]) for entry in entries],
        step_id="models",
        last_step=True,
    )
