"""Setup flow for the Spotify provider."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING
from urllib.parse import urlencode

import pkce
from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption
from music_assistant_models.enums import ConfigEntryType
from music_assistant_models.errors import LoginFailed

from music_assistant.helpers.app_vars import app_var
from music_assistant.helpers.oauth import (
    HOSTED_CALLBACK_URL,
    OAUTH_STEP_TIMEOUT,
    authorization_code_from_params,
    authorization_code_from_url,
    hosted_bounce_redirect,
)
from music_assistant.models.setup_flow import SetupFlowError, StepExpiredError

from .constants import (
    CONF_CLIENT_ID,
    CONF_LIBRESPOT_CREDENTIALS,
    CONF_REFRESH_TOKEN_DEV,
    CONF_REFRESH_TOKEN_GLOBAL,
    KEYMASTER_CLIENT_ID,
    LIBRESPOT_REDIRECT_URI,
    LIBRESPOT_SCOPE,
    PAIRING_DEVICE_NAME,
    PAIRING_TIMEOUT,
    SCOPE,
)
from .helpers import (
    get_librespot_binary,
    librespot_credentials_via_pairing,
    librespot_credentials_via_token,
)

if TYPE_CHECKING:
    from music_assistant.models.setup_flow import SetupSession

AUTHORIZE_URL = "https://accounts.spotify.com/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"

# the developer client id is a public OAuth identifier (not a secret), so it is a plain
# STRING that can be prefilled on reconfigure
CONF_ENTRY_DEV_CLIENT_ID = ConfigEntry(
    key=CONF_CLIENT_ID,
    type=ConfigEntryType.STRING,
    required=False,
)

CONF_PLAYBACK_AUTH_METHOD = "playback_auth_method"
PLAYBACK_AUTH_APP = "spotify_app"
PLAYBACK_AUTH_BROWSER = "browser"
CONF_ENTRY_PLAYBACK_AUTH_METHOD = ConfigEntry(
    key=CONF_PLAYBACK_AUTH_METHOD,
    type=ConfigEntryType.STRING,
    default_value=PLAYBACK_AUTH_APP,
    options=[ConfigValueOption(PLAYBACK_AUTH_APP), ConfigValueOption(PLAYBACK_AUTH_BROWSER)],
)

CONF_PLAYBACK_CALLBACK_URL = "playback_callback_url"
CONF_ENTRY_PLAYBACK_CALLBACK_URL = ConfigEntry(
    key=CONF_PLAYBACK_CALLBACK_URL,
    type=ConfigEntryType.STRING,
)


async def run_setup(session: SetupSession) -> None:
    """
    Run the Spotify setup flow.

    Authenticates the (required) global session with Music Assistant's own client id, then
    optionally a developer session with the user's own client id, and persists the resulting
    refresh tokens as setup data.

    :param session: The setup session driving the flow.
    """
    setup_data = dict(session.context.setup_data)
    # the global session always (re)authenticates: a refresh token cannot be reused across a
    # re-auth and secure values are never prefilled back into the flow
    setup_data[CONF_REFRESH_TOKEN_GLOBAL] = await _pkce_authenticate(
        session, app_var("spotify_client_id"), step_id="authenticate"
    )
    # playback needs its own credential, minted with Spotify's keymaster client id
    setup_data[CONF_LIBRESPOT_CREDENTIALS] = await _authorize_playback(session)
    # optional developer session using the user's own Spotify client id
    client_id_default = str(session.context.setup_data.get(CONF_CLIENT_ID) or "")
    errors: dict[str, str] | None = None
    while True:
        dev_values = await session.form(
            [replace(CONF_ENTRY_DEV_CLIENT_ID, value=client_id_default)],
            step_id="developer",
            errors=errors,
            last_step=True,
            translation_params=[HOSTED_CALLBACK_URL],
        )
        client_id = str(dev_values.get(CONF_CLIENT_ID) or "").strip()
        try:
            if client_id:
                setup_data[CONF_CLIENT_ID] = client_id
                setup_data[CONF_REFRESH_TOKEN_DEV] = await _pkce_authenticate(
                    session, client_id, step_id="authenticate_dev"
                )
            else:
                # no developer client id: clear any previously stored developer session
                setup_data[CONF_CLIENT_ID] = None
                setup_data[CONF_REFRESH_TOKEN_DEV] = None
            await session.finish(setup_data)
            return
        except SetupFlowError as err:
            errors = {"base": err.translation_key or str(err)}
            client_id_default = client_id


async def _authorize_playback(session: SetupSession) -> str:
    """
    Obtain librespot's playback credential and return it as stored-credential JSON.

    Offers pairing through the Spotify app first and falls back to a browser sign-in for
    setups where the Spotify app cannot discover Music Assistant.

    :param session: The setup session driving the flow.
    """
    librespot_bin = await get_librespot_binary()
    errors: dict[str, str] | None = None
    while True:
        method_values = await session.form(
            [CONF_ENTRY_PLAYBACK_AUTH_METHOD],
            step_id="playback_auth",
            errors=errors,
            translation_params=[PAIRING_DEVICE_NAME],
        )
        method = str(method_values.get(CONF_PLAYBACK_AUTH_METHOD) or PLAYBACK_AUTH_APP)
        try:
            if method == PLAYBACK_AUTH_APP:
                return await session.progress_until(
                    librespot_credentials_via_pairing(librespot_bin, PAIRING_DEVICE_NAME),
                    step_id="playback_pairing",
                    expires_in=PAIRING_TIMEOUT,
                )
            return await _authorize_playback_via_browser(session, librespot_bin)
        except StepExpiredError:
            errors = {"base": "pairing_not_completed"}
        except LoginFailed as err:
            raise SetupFlowError(str(err)) from err
        except SetupFlowError as err:
            errors = {"base": err.translation_key or str(err)}


async def _authorize_playback_via_browser(session: SetupSession, librespot_bin: str) -> str:
    """
    Run the keymaster sign-in and return librespot's stored credential.

    Spotify only accepts a loopback redirect for this client id, so the browser cannot report
    back to Music Assistant: the user copies the URL their browser ended up on instead.

    :param session: The setup session driving the flow.
    :param librespot_bin: Path to the librespot binary.
    """
    code_verifier, code_challenge = pkce.generate_pkce_pair()
    params = {
        "response_type": "code",
        "client_id": KEYMASTER_CLIENT_ID,
        "scope": " ".join(LIBRESPOT_SCOPE),
        "code_challenge_method": "S256",
        "code_challenge": code_challenge,
        "redirect_uri": LIBRESPOT_REDIRECT_URI,
    }
    authorize_url = f"{AUTHORIZE_URL}?{urlencode(params)}"
    values = await session.form(
        [CONF_ENTRY_PLAYBACK_CALLBACK_URL],
        step_id="playback_browser",
        expires_in=OAUTH_STEP_TIMEOUT,
        translation_params=[authorize_url],
    )
    code = authorization_code_from_url(str(values.get(CONF_PLAYBACK_CALLBACK_URL) or ""))
    token_params = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": LIBRESPOT_REDIRECT_URI,
        "client_id": KEYMASTER_CLIENT_ID,
        "code_verifier": code_verifier,
    }
    async with session.mass.http_session.post(TOKEN_URL, data=token_params) as response:
        if response.status != 200:
            raise SetupFlowError(
                f"Failed to get access token: {await response.text()}",
                translation_key="playback_code_invalid",
            )
        token_result = await response.json()
    return await librespot_credentials_via_token(librespot_bin, token_result["access_token"])


async def _pkce_authenticate(session: SetupSession, client_id: str, step_id: str) -> str:
    """
    Run the Spotify PKCE auth flow via the setup session and return a refresh token.

    :param session: The setup session driving the flow.
    :param client_id: The Spotify client id to authenticate with.
    :param step_id: The external step id (also the i18n key segment).
    """
    code_verifier, code_challenge = pkce.generate_pkce_pair()
    redirect_uri, state = hosted_bounce_redirect(session.callback_url)
    params = {
        "response_type": "code",
        "client_id": client_id,
        "scope": " ".join(SCOPE),
        "code_challenge_method": "S256",
        "code_challenge": code_challenge,
        "redirect_uri": redirect_uri,
        "state": state,
    }
    callback_params = await session.external(
        f"{AUTHORIZE_URL}?{urlencode(params)}", step_id=step_id, expires_in=OAUTH_STEP_TIMEOUT
    )
    code = authorization_code_from_params(callback_params)
    token_params = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": client_id,
        "code_verifier": code_verifier,
    }
    async with session.mass.http_session.post(TOKEN_URL, data=token_params) as response:
        if response.status != 200:
            raise SetupFlowError(f"Failed to get access token: {await response.text()}")
        token_result = await response.json()
    if not (refresh_token := token_result.get("refresh_token")):
        raise SetupFlowError("No refresh token in the token response")
    return str(refresh_token)
