"""
Setup flow for the ARD Audiothek provider.

Signing in to an ARD account is optional. ARD only accepts passwords on its own login
page, so the account is linked with a device login: the user confirms a short code on
accounts.ard.de and the flow polls until ARD hands out the tokens. A code that expires
before it was confirmed is minted afresh and the step re-shown, until the overall time for
signing in ran out.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import ConfigEntryType
from music_assistant_models.errors import LoginFailed

from music_assistant.constants import CONF_PASSWORD
from music_assistant.models.setup_flow import SetupFlowError, StepExpiredError
from music_assistant.providers.ard_audiothek import (
    CONF_DISPLAY_NAME,
    CONF_EMAIL,
    CONF_EXPIRY_TIME,
    CONF_REFRESH_TOKEN,
    CONF_TOKEN_BEARER,
    CONF_USERID,
)
from music_assistant.providers.ard_audiothek.auth import (
    ArdTokens,
    poll_device_token,
    request_device_code,
)

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigValueType

    from music_assistant.models.setup_flow import SetupSession

CONF_SIGN_IN = "sign_in"
AUTHORIZATION_TIMEOUT = 15 * 60.0


async def run_setup(session: SetupSession) -> None:
    """
    Run the setup flow: optionally link an ARD account, then create the provider.

    :param session: The setup session driving the flow.
    """
    errors: dict[str, str | SetupFlowError] | None = None
    signed_in = bool(session.context.setup_data.get(CONF_REFRESH_TOKEN))
    while True:
        values = await session.form(
            [
                ConfigEntry(
                    key=CONF_SIGN_IN,
                    type=ConfigEntryType.BOOLEAN,
                    required=False,
                    default_value=signed_in,
                )
            ],
            step_id="user",
            errors=errors,
        )
        # the email/password keys of the former password login are cleared on every save
        collected: dict[str, ConfigValueType] = {
            CONF_EMAIL: None,
            CONF_PASSWORD: None,
            CONF_REFRESH_TOKEN: None,
            CONF_TOKEN_BEARER: None,
            CONF_USERID: None,
            CONF_DISPLAY_NAME: None,
            CONF_EXPIRY_TIME: None,
        }
        if values[CONF_SIGN_IN]:
            try:
                tokens = await _device_login(session)
            except LoginFailed as err:
                session.mass.logger.warning("ARD account login failed: %s", err)
                # the reason stays in the log, the form shows a message that can be translated
                errors = {
                    "base": SetupFlowError(
                        "Signing in to your ARD account failed. Please try again.",
                        translation_key="login_failed",
                    )
                }
                continue
            collected.update(
                {
                    CONF_EMAIL: tokens.email,
                    CONF_REFRESH_TOKEN: tokens.refresh_token,
                    CONF_TOKEN_BEARER: tokens.id_token,
                    CONF_USERID: tokens.user_id,
                    CONF_DISPLAY_NAME: tokens.display_name,
                    CONF_EXPIRY_TIME: str(tokens.expires_at - 60),
                }
            )
        try:
            await session.finish(collected)
            return
        except SetupFlowError as err:
            errors = {"base": err}


async def _device_login(session: SetupSession) -> ArdTokens:
    """
    Show a device login code until the user confirmed one, returning the account tokens.

    :param session: The setup session driving the flow.
    :raises StepExpiredError: When the user did not sign in within AUTHORIZATION_TIMEOUT.
    """
    http_session = session.mass.http_session
    deadline = time.monotonic() + AUTHORIZATION_TIMEOUT
    while (remaining := deadline - time.monotonic()) > 0:
        device = await request_device_code(http_session)
        user_code = str(device["user_code"])
        # the step countdown never outlives the overall budget
        remaining = deadline - time.monotonic()
        try:
            return await session.external_until(
                poll_device_token(http_session, device),
                url=str(device["verification_uri"]),
                step_id="device_login",
                expires_in=max(0.0, min(float(device.get("expires_in") or 180), remaining)),
                copy_text=user_code,
                translation_params=[user_code],
            )
        except StepExpiredError:
            continue
    raise StepExpiredError
