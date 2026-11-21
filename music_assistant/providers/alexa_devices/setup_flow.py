"""
Setup flow for the Alexa Devices player provider.

aioamazondevices logs in with the account e-mail, password and a one-time code from
the authenticator app set up for Amazon two-step verification; no proxied Amazon
login page is involved. Only the resulting login data (tokens and cookies) is stored,
not the password: a new sign-in asks for the password and a fresh code again.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import aiohttp
from aioamazondevices.exceptions import AmazonError, CannotAuthenticate, CannotConnect
from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import ConfigEntryType

from music_assistant.constants import CONF_PASSWORD, CONF_USERNAME
from music_assistant.models.setup_flow import SetupFlowError, StepExpiredError

from .constants import (
    CONF_API_BASIC_AUTH_PASSWORD,
    CONF_API_BASIC_AUTH_USERNAME,
    CONF_API_URL,
    CONF_LOGIN_DATA,
    CONF_OTP_CODE,
    DEFAULT_API_URL,
)
from .provider import create_api

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigValueType

    from music_assistant.mass import MusicAssistant
    from music_assistant.models.setup_flow import SetupSession

LOGIN_TIMEOUT = 90


async def run_setup(session: SetupSession) -> None:
    """
    Run the Alexa Devices setup flow.

    :param session: The setup session driving the flow.
    """
    collected: dict[str, ConfigValueType] = dict(session.context.setup_data)
    errors: dict[str, str | SetupFlowError] | None = None
    while True:
        values = await session.form(
            _credential_entries(collected), step_id="credentials", errors=errors, last_step=True
        )
        try:
            login_data = await session.progress_until(
                _login(
                    session.mass,
                    str(values[CONF_USERNAME]),
                    str(values[CONF_PASSWORD]),
                    str(values[CONF_OTP_CODE]),
                ),
                step_id="login",
                text="login",
                expires_in=LOGIN_TIMEOUT,
            )
        except CannotAuthenticate:
            errors = {"base": "invalid_auth"}
            continue
        except CannotConnect, StepExpiredError:
            errors = {"base": "cannot_connect"}
            continue
        except AmazonError:
            errors = {"base": "cannot_retrieve_data"}
            continue

        previous_api_password = collected.get(CONF_API_BASIC_AUTH_PASSWORD)
        collected = {
            CONF_USERNAME: values[CONF_USERNAME],
            CONF_LOGIN_DATA: json.dumps(login_data),
            CONF_API_URL: values[CONF_API_URL],
            CONF_API_BASIC_AUTH_USERNAME: values.get(CONF_API_BASIC_AUTH_USERNAME),
            # secure values are never prefilled: keep the stored one when left empty
            CONF_API_BASIC_AUTH_PASSWORD: values.get(CONF_API_BASIC_AUTH_PASSWORD)
            or previous_api_password,
        }
        try:
            await session.finish(collected)
            return
        except SetupFlowError as err:
            errors = {"base": err}


async def _login(
    mass: MusicAssistant, username: str, password: str, otp_code: str
) -> dict[str, Any]:
    """Log in to Amazon interactively and return the login data to store."""
    async with aiohttp.ClientSession() as http_session:
        api = create_api(http_session, username, password, mass.storage_path)
        return await api.login.login_mode_interactive(otp_code)


def _credential_entries(prefill: dict[str, ConfigValueType]) -> list[ConfigEntry]:
    """Return the credential form entries, prefilling the non-secret fields."""
    return [
        ConfigEntry(
            key=CONF_USERNAME,
            type=ConfigEntryType.STRING,
            required=True,
            value=prefill.get(CONF_USERNAME),
        ),
        ConfigEntry(key=CONF_PASSWORD, type=ConfigEntryType.SECURE_STRING, required=True),
        ConfigEntry(key=CONF_OTP_CODE, type=ConfigEntryType.STRING, required=True),
        ConfigEntry(
            key=CONF_API_URL,
            type=ConfigEntryType.STRING,
            required=True,
            default_value=DEFAULT_API_URL,
            value=prefill.get(CONF_API_URL),
        ),
        ConfigEntry(
            key=CONF_API_BASIC_AUTH_USERNAME,
            type=ConfigEntryType.STRING,
            required=False,
            value=prefill.get(CONF_API_BASIC_AUTH_USERNAME),
        ),
        ConfigEntry(
            key=CONF_API_BASIC_AUTH_PASSWORD, type=ConfigEntryType.SECURE_STRING, required=False
        ),
    ]
