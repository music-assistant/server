"""Setup flow for the Spotify provider."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING
from urllib.parse import urlencode

import pkce
from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import ConfigEntryType

from music_assistant.helpers.app_vars import app_var
from music_assistant.helpers.oauth import (
    HOSTED_CALLBACK_URL,
    authorization_code_from_params,
    hosted_bounce_redirect,
)
from music_assistant.models.setup_flow import SetupFlowError

from .constants import (
    CONF_CLIENT_ID,
    CONF_REFRESH_TOKEN_DEV,
    CONF_REFRESH_TOKEN_GLOBAL,
    SCOPE,
)

if TYPE_CHECKING:
    from music_assistant.models.setup_flow import SetupSession

AUTHORIZE_URL = "https://accounts.spotify.com/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"

# the developer client id is a public OAuth identifier (not a secret), so it is a plain
# STRING that can be prefilled on reconfigure; the user must register HOSTED_CALLBACK_URL
# as a redirect URI on their own Spotify developer application
CONF_ENTRY_DEV_CLIENT_ID = ConfigEntry(
    key=CONF_CLIENT_ID,
    type=ConfigEntryType.STRING,
    required=False,
    translation_params=[HOSTED_CALLBACK_URL],
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
    # optional developer session using the user's own Spotify client id
    client_id_default = str(session.context.setup_data.get(CONF_CLIENT_ID) or "")
    errors: dict[str, str] | None = None
    while True:
        dev_values = await session.form(
            [replace(CONF_ENTRY_DEV_CLIENT_ID, value=client_id_default)],
            step_id="developer",
            errors=errors,
            last_step=True,
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
        f"{AUTHORIZE_URL}?{urlencode(params)}", step_id=step_id
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
    return str(token_result["refresh_token"])
