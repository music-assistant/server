"""Setup flow for the Yoto music provider."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING
from urllib.parse import urlencode

import pkce
from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import ConfigEntryType

from music_assistant.helpers.oauth import (
    OAUTH_STEP_TIMEOUT,
    authorization_code_from_params,
    hosted_bounce_redirect,
)
from music_assistant.models.setup_flow import SetupFlowError

if TYPE_CHECKING:
    from music_assistant.models.setup_flow import SetupSession

CONF_CLIENT_ID = "client_id"
CONF_REFRESH_TOKEN = "refresh_token"

CONF_ENTRY_CLIENT_ID = ConfigEntry(
    key=CONF_CLIENT_ID,
    type=ConfigEntryType.STRING,
    required=True,
)

HELP_LINK_URL = "https://www.music-assistant.io/music-providers/yoto/"
DEVELOPER_LOGIN_URL = "https://dashboard.yoto.dev/"
AUTHORIZE_URL = "https://login.yotoplay.com/authorize"
TOKEN_URL = "https://login.yotoplay.com/oauth/token"
AUDIENCE = "https://api.yotoplay.com"


async def run_setup(session: SetupSession) -> None:
    """
    Run the setup flow for the Yoto music provider.

    :param session: The setup session driving the flow.
    """
    setup_data = dict(session.context.setup_data)
    client_id_default = str(setup_data.get(CONF_CLIENT_ID) or "")
    errors: dict[str, str] | None = None

    while True:
        submitted = await session.form(
            [
                ConfigEntry(
                    key="intro",
                    type=ConfigEntryType.LABEL,
                    help_link=HELP_LINK_URL,
                ),
                replace(CONF_ENTRY_CLIENT_ID, value=client_id_default),
            ],
            step_id="user",
            errors=errors,
        )
        client_id = str(submitted.get(CONF_CLIENT_ID) or "").strip()
        if not client_id:
            errors = {"base": "missing_client_id"}
            continue

        try:
            refresh_token = await _pkce_authenticate(session, client_id)
            setup_data[CONF_CLIENT_ID] = client_id
            setup_data[CONF_REFRESH_TOKEN] = refresh_token
            await session.finish(setup_data)
            return
        except SetupFlowError as err:
            errors = {"base": err.translation_key or str(err)}


async def _pkce_authenticate(session: SetupSession, client_id: str) -> str:
    """
    Run the Yoto PKCE auth flow via setup session and return a refresh token.

    :param session: The setup session driving the flow.
    :param client_id: The Yoto developer client id.
    """
    code_verifier, code_challenge = pkce.generate_pkce_pair()
    redirect_uri, state = hosted_bounce_redirect(session.callback_url)
    params = {
        "response_type": "code",
        "client_id": client_id,
        "scope": "family:library:view offline_access",
        "audience": AUDIENCE,
        "code_challenge_method": "S256",
        "code_challenge": code_challenge,
        "redirect_uri": redirect_uri,
        "state": state,
    }
    authorize_url = f"{AUTHORIZE_URL}?{urlencode(params)}"
    callback_params = await session.external(
        authorize_url, step_id="authenticate", expires_in=OAUTH_STEP_TIMEOUT
    )
    code = authorization_code_from_params(callback_params)
    token_params = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": client_id,
        "code_verifier": code_verifier,
    }
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    async with session.mass.http_session.post(
        TOKEN_URL, data=token_params, headers=headers
    ) as response:
        if response.status != 200:
            res_text = await response.text()
            raise SetupFlowError(f"Failed to get access token ({response.status}): {res_text}")
        token_result = await response.json()

    refresh_token = token_result.get("refresh_token")
    if not refresh_token:
        raise SetupFlowError("No refresh token returned in authentication response")
    return str(refresh_token)
