"""Setup flow for the BookOrbit provider."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, Any

from aiohttp import ClientError
from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import ConfigEntryType

from music_assistant.models.setup_flow import SetupFlowError
from music_assistant.providers.bookorbit.client import BookOrbitAuthError, BookOrbitClient
from music_assistant.providers.bookorbit.constants import (
    CONF_API_TOKEN,
    CONF_PASSWORD,
    CONF_URL,
    CONF_USERNAME,
    CONF_VERIFY_SSL,
    DOMAIN,
)
from music_assistant.providers.bookorbit.helpers import normalize_base_url

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
        required=True,
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
        key=CONF_VERIFY_SSL,
        type=ConfigEntryType.BOOLEAN,
        required=False,
        advanced=True,
        default_value=True,
    ),
)


async def _validate_connection(session: SetupSession, setup_data: dict[str, Any]) -> None:
    """Log in to BookOrbit and list libraries before the provider is saved."""
    base_url = normalize_base_url(str(setup_data[CONF_URL]))
    username = str(setup_data[CONF_USERNAME])
    password = str(setup_data[CONF_PASSWORD])
    verify_ssl = bool(setup_data.get(CONF_VERIFY_SSL, True))
    client = BookOrbitClient(
        session=session.mass.http_session,
        base_url=base_url,
        username=username,
        password=password,
        verify_ssl=verify_ssl,
    )
    try:
        await client.login()
        await client.list_libraries()
    except BookOrbitAuthError as err:
        raise SetupFlowError(
            f"Login to BookOrbit at {base_url} failed.",
            translation_key="login_failed",
            translation_args=[base_url],
            translation_owner=DOMAIN,
        ) from err
    except ClientError as err:
        raise SetupFlowError(
            f"Could not reach BookOrbit at {base_url}.",
            translation_key="connection_failed",
            translation_args=[base_url],
            translation_owner=DOMAIN,
        ) from err


async def run_setup(session: SetupSession) -> None:
    """Run the setup flow: collect the connection details and create the provider."""
    errors: dict[str, str | SetupFlowError] | None = None
    setup_data = dict(session.context.setup_data)
    while True:
        entries = [
            replace(entry, value=setup_data.get(entry.key, entry.value)) for entry in _ENTRIES
        ]
        submitted = await session.form(entries, step_id="user", errors=errors, last_step=True)
        prior_username = setup_data.get(CONF_USERNAME)
        setup_data.update(
            (key, value)
            for key, value in submitted.items()
            if key not in (CONF_PASSWORD, CONF_API_TOKEN) or value not in (None, "")
        )
        if not submitted.get(CONF_PASSWORD) and prior_username != setup_data.get(CONF_USERNAME):
            setup_data[CONF_PASSWORD] = None
        try:
            await _validate_connection(session, setup_data)
            await session.finish(setup_data)
            return
        except SetupFlowError as err:
            errors = {"base": err}
