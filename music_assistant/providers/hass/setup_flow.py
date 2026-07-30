"""Setup flow for the Home Assistant provider."""

from __future__ import annotations

from typing import TYPE_CHECKING

import shortuuid
from hass_client.utils import base_url, get_auth_url, get_long_lived_token, get_token
from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import ConfigEntryType

from music_assistant.constants import MASS_LOGO_ONLINE
from music_assistant.models.setup_flow import AbortFlow, SetupFlowError

from . import CONF_AUTH_TOKEN, CONF_URL, CONF_VERIFY_SSL

if TYPE_CHECKING:
    from music_assistant.models.setup_flow import SetupSession


async def run_setup(session: SetupSession) -> None:
    """
    Run the Home Assistant setup flow.

    Collects the instance URL and a long-lived access token, either pasted in manually
    or obtained through Home Assistant's own OAuth flow, using the flow's local callback
    URL directly as the redirect URI.

    :param session: The setup session driving the flow.
    """
    if session.mass.running_as_hass_addon:
        # add-on installs are auto-configured against the supervisor's internal api
        raise AbortFlow("nothing_to_configure")

    prefill_url = session.context.setup_data.get(CONF_URL)
    prefill_ssl = session.context.setup_data.get(CONF_VERIFY_SSL, True)
    errors: dict[str, str] | None = None
    while True:
        values = await session.form(
            [
                ConfigEntry(
                    key=CONF_URL, type=ConfigEntryType.STRING, required=True, value=prefill_url
                ),
                ConfigEntry(
                    key=CONF_VERIFY_SSL,
                    type=ConfigEntryType.BOOLEAN,
                    default_value=True,
                    value=prefill_ssl,
                ),
                # lets a user paste a long-lived token here to skip OAuth entirely
                ConfigEntry(
                    key=CONF_AUTH_TOKEN,
                    type=ConfigEntryType.SECURE_STRING,
                    required=False,
                    advanced=True,
                ),
            ],
            step_id="user",
            errors=errors,
            last_step=False,
        )
        hass_url = str(values[CONF_URL]).strip()
        verify_ssl = bool(values[CONF_VERIFY_SSL])
        # re-prefill with what was just submitted, so a failed attempt below doesn't
        # revert the form back to the original (reconfigure) values
        prefill_url, prefill_ssl = hass_url, verify_ssl
        manual_token = str(values.get(CONF_AUTH_TOKEN) or "").strip()

        try:
            token = manual_token if manual_token else await _authenticate(session, hass_url)
        except SetupFlowError as err:
            errors = {"base": err.translation_key or str(err)}
            continue

        try:
            await session.finish(
                {CONF_URL: hass_url, CONF_AUTH_TOKEN: token, CONF_VERIFY_SSL: verify_ssl}
            )
            return
        except SetupFlowError as err:
            errors = {"base": err.translation_key or str(err)}


async def _authenticate(session: SetupSession, hass_url: str) -> str:
    """
    Run Home Assistant's OAuth flow via the setup session and return a long-lived token.

    :param session: The setup session driving the flow.
    :param hass_url: The Home Assistant instance URL entered by the user.
    """
    callback = session.callback_url
    # Home Assistant's indieauth-style auth accepts any redirect URI on the same origin
    # as the client id, so the local callback is used directly - unlike Spotify/Google/
    # Microsoft, no fixed hosted-bounce redirect needs to be pre-registered
    client_id = base_url(callback)
    auth_url = get_auth_url(hass_url, callback, client_id=client_id, state=session.flow_id)
    params = await session.external(auth_url, step_id="auth")
    if params.get("state") != session.flow_id:
        raise SetupFlowError("Authentication failed: state mismatch")
    if not params.get("code"):
        raise SetupFlowError("Authentication failed: no authorization code returned")
    try:
        token_details = await get_token(hass_url, params["code"], client_id=client_id)
        return str(
            await get_long_lived_token(
                hass_url,
                token_details["access_token"],
                client_name=f"Music Assistant {shortuuid.random(6)}",
                client_icon=MASS_LOGO_ONLINE,
                lifespan=365 * 2,
            )
        )
    except Exception as err:
        # get_token/get_long_lived_token surface different error shapes (RuntimeError,
        # BaseHassClientError, plain connection errors); collapse them into one retryable slug
        raise SetupFlowError("auth_failed") from err
