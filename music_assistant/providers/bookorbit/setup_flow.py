"""Setup flow for the BookOrbit provider."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, Any

from aiohttp import ClientError, DummyCookieJar
from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import ConfigEntryType

from music_assistant.helpers.aiohttp_client import create_clientsession
from music_assistant.models.setup_flow import SetupFlowError
from music_assistant.providers.bookorbit.client import BookOrbitAuthError, BookOrbitClient
from music_assistant.providers.bookorbit.constants import (
    AIOHTTP_TIMEOUT,
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
        key=CONF_VERIFY_SSL,
        type=ConfigEntryType.BOOLEAN,
        required=False,
        advanced=True,
        default_value=True,
    ),
)


def _require_password(setup_data: dict[str, Any]) -> str:
    """Return a non-empty password or raise SetupFlowError."""
    password = setup_data.get(CONF_PASSWORD)
    if password in (None, ""):
        raise SetupFlowError(
            "Password is required.",
            translation_key="password_required",
            translation_owner=DOMAIN,
        )
    return str(password)


async def _validate_connection(session: SetupSession, setup_data: dict[str, Any]) -> None:
    """Log in to BookOrbit and list libraries before the provider is saved."""
    base_url = normalize_base_url(str(setup_data[CONF_URL]))
    username = str(setup_data[CONF_USERNAME])
    password = _require_password(setup_data)
    verify_ssl = bool(setup_data.get(CONF_VERIFY_SSL, True))
    http_session = create_clientsession(
        session.mass,
        verify_ssl=verify_ssl,
        cookie_jar=DummyCookieJar(),
        timeout=AIOHTTP_TIMEOUT,
    )
    client = BookOrbitClient(
        base_url=base_url,
        username=username,
        password=password,
        verify_ssl=verify_ssl,
        session=http_session,
        owns_session=True,
    )
    try:
        await _check_login(client, base_url)
    finally:
        await client.close()


async def _check_login(client: BookOrbitClient, base_url: str) -> None:
    """Log in and list libraries, raising SetupFlowError on failure."""
    try:
        await client.login()
    except BookOrbitAuthError as err:
        raise SetupFlowError(
            f"Login to BookOrbit at {base_url} failed.",
            translation_key="login_failed",
            translation_args=[base_url],
            translation_owner=DOMAIN,
        ) from err
    except (ClientError, TimeoutError) as err:
        raise SetupFlowError(
            f"Could not reach BookOrbit at {base_url}.",
            translation_key="connection_failed",
            translation_args=[base_url],
            translation_owner=DOMAIN,
        ) from err
    try:
        await client.list_libraries()
    except BookOrbitAuthError as err:
        raise SetupFlowError(
            f"Login to BookOrbit at {base_url} failed.",
            translation_key="login_failed",
            translation_args=[base_url],
            translation_owner=DOMAIN,
        ) from err
    except (ClientError, TimeoutError) as err:
        raise SetupFlowError(
            f"Could not list libraries on BookOrbit at {base_url}.",
            translation_key="connection_failed",
            translation_args=[base_url],
            translation_owner=DOMAIN,
        ) from err


def _same_username(saved: dict[str, Any], submitted: dict[str, Any]) -> bool:
    """Return whether the submitted form keeps the username that is already saved."""
    saved_username = str(saved.get(CONF_USERNAME) or "").strip()
    return (
        bool(saved_username) and saved_username == str(submitted.get(CONF_USERNAME) or "").strip()
    )


async def run_setup(session: SetupSession) -> None:
    """Run the setup flow: collect the connection details and create the provider."""
    errors: dict[str, str | SetupFlowError] | None = None
    setup_data = dict(session.context.setup_data)
    while True:
        entries = [
            replace(entry, value=setup_data.get(entry.key, entry.value)) for entry in _ENTRIES
        ]
        submitted = await session.form(entries, step_id="user", errors=errors, last_step=True)
        # Blank password on retry must keep the in-progress password (e.g. a replacement
        # entered before a transient validation failure), not only the originally saved one.
        if not submitted.get(CONF_PASSWORD) and _same_username(setup_data, submitted):
            submitted = dict(submitted)
            submitted[CONF_PASSWORD] = setup_data.get(CONF_PASSWORD)
        prior_username = setup_data.get(CONF_USERNAME)
        setup_data.update(
            (key, value)
            for key, value in submitted.items()
            if key != CONF_PASSWORD or value not in (None, "")
        )
        if not submitted.get(CONF_PASSWORD) and prior_username != setup_data.get(CONF_USERNAME):
            setup_data[CONF_PASSWORD] = None
        try:
            await _validate_connection(session, setup_data)
            await session.finish(setup_data)
            return
        except SetupFlowError as err:
            errors = {"base": err}
