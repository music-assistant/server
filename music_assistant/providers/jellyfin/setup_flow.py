"""Setup flow for the Jellyfin provider."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

import aiohttp
from aiojellyfin.session import SessionConfiguration
from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption, ConfigValueType
from music_assistant_models.enums import ConfigEntryType

from music_assistant.models.setup_flow import SetupFlowError, StepExpiredError
from music_assistant.providers.jellyfin import (
    AUTH_METHOD_PASSWORD,
    AUTH_METHOD_QUICK_CONNECT,
    CONF_ACCESS_TOKEN,
    CONF_DEVICE_ID,
    CONF_PASSWORD,
    CONF_URL,
    CONF_USER_ID,
    CONF_USERNAME,
    CONF_VERIFY_SSL,
    authenticate_with_quick_connect,
    initiate_quick_connect,
)

if TYPE_CHECKING:
    from music_assistant.models.setup_flow import SetupSession

_ENTRIES = (
    ConfigEntry(key=CONF_URL, type=ConfigEntryType.STRING, required=True),
    ConfigEntry(key=CONF_USERNAME, type=ConfigEntryType.STRING, required=True),
    ConfigEntry(key=CONF_PASSWORD, type=ConfigEntryType.SECURE_STRING, required=False),
    ConfigEntry(
        key=CONF_VERIFY_SSL,
        type=ConfigEntryType.BOOLEAN,
        required=False,
        advanced=True,
        default_value=True,
    ),
)
_QUICK_CONNECT_TIMEOUT = 600


async def run_setup(session: SetupSession) -> None:
    """Run the setup flow: collect the connection details and create the provider."""
    method = (
        await session.form(
            [
                ConfigEntry(
                    key="auth_method",
                    type=ConfigEntryType.STRING,
                    required=True,
                    default_value=AUTH_METHOD_PASSWORD,
                    options=[
                        ConfigValueOption(value=AUTH_METHOD_PASSWORD),
                        ConfigValueOption(value=AUTH_METHOD_QUICK_CONNECT),
                    ],
                )
            ],
            step_id="auth_method",
        )
    )["auth_method"]
    if method == AUTH_METHOD_QUICK_CONNECT:
        await _run_quick_connect(session)
        return

    errors: dict[str, str] | None = None
    setup_data = dict(session.context.setup_data)
    setup_data.pop(CONF_ACCESS_TOKEN, None)
    setup_data.pop(CONF_USER_ID, None)
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


async def _run_quick_connect(session: SetupSession) -> None:
    """Connect to Jellyfin through its Quick Connect device flow."""
    errors: dict[str, str] | None = None
    setup_data: dict[str, ConfigValueType] = dict(session.context.setup_data)
    setup_data.pop(CONF_USERNAME, None)
    setup_data.pop(CONF_PASSWORD, None)
    while True:
        entries = [
            replace(entry, value=setup_data.get(entry.key, entry.value))
            for entry in _ENTRIES
            if entry.key not in (CONF_USERNAME, CONF_PASSWORD)
        ]
        submitted = await session.form(entries, step_id="quick_connect", errors=errors)
        setup_data.update(submitted)
        verify_ssl = bool(setup_data.get(CONF_VERIFY_SSL, True))
        http_session = session.mass.http_session if verify_ssl else session.mass.http_session_no_ssl
        device_id = setup_data.get(CONF_DEVICE_ID) or session.mass.server_id
        session_config = SessionConfiguration(
            session=http_session,
            url=str(setup_data[CONF_URL]),
            verify_ssl=verify_ssl,
            app_name="Music Assistant",
            app_version=session.mass.version,
            device_name="Music Assistant",
            device_id=str(device_id),
        )
        try:
            secret, code = await initiate_quick_connect(session_config)
            client = await session.external_until(
                authenticate_with_quick_connect(session_config, secret),
                url=f"{session_config.url.rstrip('/')}/web/#/quickconnect",
                step_id="quick_connect_approval",
                expires_in=_QUICK_CONNECT_TIMEOUT,
                translation_params=[code],
                copy_text=code,
            )
        except StepExpiredError:
            errors = {"base": "quick_connect_timeout"}
            continue
        except (aiohttp.ClientError, KeyError) as err:
            errors = {"base": str(err)}
            continue
        setup_data.update(
            {
                CONF_ACCESS_TOKEN: client._access_token,
                CONF_USER_ID: client._user_id,
                CONF_DEVICE_ID: str(device_id),
            }
        )
        try:
            await session.finish(setup_data)
            return
        except SetupFlowError as err:
            errors = {"base": err.translation_key or str(err)}
