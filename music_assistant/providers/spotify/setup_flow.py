"""Setup flow for the Spotify provider."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, Any
from urllib.parse import urlencode

import pkce
from aiohttp import ClientError, ClientTimeout
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
from music_assistant.models.setup_flow import AbortFlow, SetupFlowError, StepExpiredError

from .constants import (
    CONF_ACCOUNT_ID,
    CONF_CLIENT_ID,
    CONF_LIBRESPOT_CREDENTIALS,
    CONF_REFRESH_TOKEN_DEV,
    CONF_REFRESH_TOKEN_GLOBAL,
    KEYMASTER_CLIENT_ID,
    LIBRESPOT_REDIRECT_PATH,
    LIBRESPOT_REDIRECT_PORT,
    LIBRESPOT_REDIRECT_URI,
    LIBRESPOT_SCOPE,
    LOOPBACK_WAIT_TIMEOUT,
    PAIRING_DEVICE_NAME,
    PAIRING_TIMEOUT,
    SCOPE,
)
from .helpers import (
    await_loopback_authorization,
    get_librespot_binary,
    librespot_credentials_via_pairing,
    librespot_credentials_via_token,
)
from .provider import SpotifyProvider

if TYPE_CHECKING:
    from music_assistant.models.setup_flow import SetupSession

# seconds to wait for the account lookup that gates the setup
ACCOUNT_LOOKUP_TIMEOUT = 30

AUTHORIZE_URL = "https://accounts.spotify.com/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"

# the developer client id is a public OAuth identifier (not a secret), so it is a plain
# STRING that can be prefilled on reconfigure
CONF_ENTRY_DEV_CLIENT_ID = ConfigEntry(
    key=CONF_CLIENT_ID,
    type=ConfigEntryType.STRING,
    required=False,
)

CONF_USE_DEV_KEY = "use_developer_key"
CONF_ENTRY_USE_DEV_KEY = ConfigEntry(
    key=CONF_USE_DEV_KEY,
    type=ConfigEntryType.BOOLEAN,
    default_value=False,
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

    Authenticates the (required) global session with Music Assistant's own client id, authorizes
    playback separately, then optionally a developer session with the user's own client id, and
    persists the resulting tokens and credentials as setup data.

    :param session: The setup session driving the flow.
    """
    setup_data = dict(session.context.setup_data)
    # the global session always (re)authenticates: a refresh token cannot be reused across a
    # re-auth and secure values are never prefilled back into the flow
    token_result = await _pkce_authenticate(
        session, app_var("spotify_client_id"), step_id="authenticate"
    )
    setup_data[CONF_REFRESH_TOKEN_GLOBAL] = str(token_result["refresh_token"])
    # an account that cannot work is turned away before the playback authorization
    setup_data[CONF_ACCOUNT_ID] = await _verify_account(session, str(token_result["access_token"]))
    # playback needs its own credential, minted with Spotify's keymaster client id
    setup_data[CONF_LIBRESPOT_CREDENTIALS] = await _authorize_playback(session)
    # everything needed is collected by now; the developer key is a purely optional extra,
    # so it is offered as an opt-in rather than a field the user has to reason about
    client_id_default = str(session.context.setup_data.get(CONF_CLIENT_ID) or "")
    errors: dict[str, str] | None = None
    while True:
        optin_values = await session.form(
            [replace(CONF_ENTRY_USE_DEV_KEY, value=bool(client_id_default))],
            step_id="developer_optin",
            errors=errors,
            last_step=True,
        )
        if not optin_values.get(CONF_USE_DEV_KEY):
            # opted out: clear any previously stored developer session
            setup_data[CONF_CLIENT_ID] = None
            setup_data[CONF_REFRESH_TOKEN_DEV] = None
            try:
                await session.finish(setup_data)
                return
            except SetupFlowError as err:
                errors = {"base": err.translation_key or str(err)}
                continue
        client_id_default, errors = await _authorize_developer_key(
            session, setup_data, client_id_default
        )
        if errors is None:
            return


async def _authorize_developer_key(
    session: SetupSession, setup_data: dict[str, Any], client_id_default: str
) -> tuple[str, dict[str, str] | None]:
    """
    Collect and authorize the user's own Spotify developer key, then finish the flow.

    Returns the client id to prefill and the errors to show when the attempt failed; the
    errors are None once the flow has finished.

    :param session: The setup session driving the flow.
    :param setup_data: The setup data collected so far, updated in place.
    :param client_id_default: Client id to prefill in the form.
    """
    dev_values = await session.form(
        [replace(CONF_ENTRY_DEV_CLIENT_ID, value=client_id_default)],
        step_id="developer",
        last_step=True,
        translation_params=[HOSTED_CALLBACK_URL],
    )
    client_id = str(dev_values.get(CONF_CLIENT_ID) or "").strip()
    try:
        if client_id:
            setup_data[CONF_CLIENT_ID] = client_id
            dev_token_result = await _pkce_authenticate(
                session, client_id, step_id="authenticate_dev"
            )
            setup_data[CONF_REFRESH_TOKEN_DEV] = str(dev_token_result["refresh_token"])
        else:
            # opted in but left the field empty: keep using the shared key
            setup_data[CONF_CLIENT_ID] = None
            setup_data[CONF_REFRESH_TOKEN_DEV] = None
        await session.finish(setup_data)
    except SetupFlowError as err:
        return client_id, {"base": err.translation_key or str(err)}
    return client_id, None


async def _verify_account(session: SetupSession, access_token: str) -> str | None:
    """
    Check the just-authenticated Spotify account and return its id.

    Turns the user away when the account has no Spotify Premium (librespot, which
    streams this provider's audio, refuses to play for a free account) or when it is
    already set up on another provider instance. A lookup Spotify does not answer is
    not held against the user: the setup simply continues and None is returned.

    :param session: The setup session driving the flow.
    :param access_token: The access token from the just-completed sign-in. Reusing
        it is deliberate — minting a fresh one rotates the refresh token, which
        revokes the one just stored as setup data.
    :raises AbortFlow: When the account is non-Premium or already configured.
    """
    try:
        async with session.mass.http_session.get(
            "https://api.spotify.com/v1/me",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=ClientTimeout(total=ACCOUNT_LOOKUP_TIMEOUT),
        ) as response:
            if response.status != 200:
                return None
            # a malformed body raises ValueError, which is not a ClientError
            userinfo = await response.json()
    except ClientError, TimeoutError, ValueError:
        return None
    product = str(userinfo.get("product") or "")
    if product and product != "premium":
        raise AbortFlow("premium_required")
    if not (account_id := str(userinfo.get("id") or "")):
        return None
    if await _account_in_use(session, account_id):
        raise AbortFlow("account_already_configured")
    return account_id


async def _account_in_use(session: SetupSession, account_id: str) -> bool:
    """
    Return whether another Spotify provider instance is already set up for this account.

    Compares the account id stored with each instance's configuration, so an instance
    that is disabled or failed to load still holds its account. Configurations
    predating that stored value fall back to the running instance, which fills the
    value in on its next successful load. The instance being reconfigured is of
    course allowed to keep its own account.

    :param session: The setup session driving the flow.
    :param account_id: The Spotify user id that just signed in.
    """
    mass = session.mass
    for config in await mass.config.get_provider_configs(provider_domain="spotify"):
        if config.instance_id == session.context.instance_id:
            continue
        if stored := mass.config.get_provider_setup_value(config.instance_id, CONF_ACCOUNT_ID):
            if str(stored) == account_id:
                return True
            continue
        provider = mass.get_provider(config.instance_id, return_unavailable=True)
        if isinstance(provider, SpotifyProvider) and provider.account_id == account_id:
            return True
    return False


async def _authorize_playback(session: SetupSession) -> str:
    """
    Obtain librespot's playback credential and return it as stored-credential JSON.

    Offers pairing through the Spotify app first and falls back to a browser sign-in for
    setups where the Spotify app cannot discover Music Assistant.

    :param session: The setup session driving the flow.
    """
    try:
        librespot_bin = await get_librespot_binary()
    except RuntimeError as err:
        raise SetupFlowError(str(err), translation_key="librespot_unavailable") from err
    errors: dict[str, str] | None = None
    while True:
        method_values = await session.form(
            [CONF_ENTRY_PLAYBACK_AUTH_METHOD],
            step_id="playback_auth",
            errors=errors,
        )
        method = str(method_values.get(CONF_PLAYBACK_AUTH_METHOD) or PLAYBACK_AUTH_APP)
        # every failure loops back to this form: the account is already authorized by now, so
        # aborting the flow would throw that away over a retryable mistake
        try:
            if method == PLAYBACK_AUTH_APP:
                return await session.progress_until(
                    librespot_credentials_via_pairing(librespot_bin, PAIRING_DEVICE_NAME),
                    step_id="playback_pairing",
                    text="pairing_instructions",
                    expires_in=PAIRING_TIMEOUT,
                )
            return await _authorize_playback_via_browser(session, librespot_bin)
        except StepExpiredError:
            errors = {
                "base": "pairing_not_completed"
                if method == PLAYBACK_AUTH_APP
                else "playback_not_completed"
            }
        except SetupFlowError as err:
            errors = {"base": err.translation_key or "playback_auth_failed"}
        except LoginFailed, ClientError, KeyError:
            # librespot refusing the token, a transport failure, or a token response without a
            # token; LoginFailed's own default key is too generic to show here
            errors = {"base": "playback_auth_failed"}


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
    try:
        # the loopback target is only reachable when the browser runs on this host, in which
        # case the step completes on its own; everyone else falls through to the paste form
        callback_params = await session.external_until(
            await_loopback_authorization(LIBRESPOT_REDIRECT_PORT, LIBRESPOT_REDIRECT_PATH),
            authorize_url,
            step_id="playback_browser_open",
            expires_in=LOOPBACK_WAIT_TIMEOUT,
        )
        code = authorization_code_from_params(callback_params)
    except StepExpiredError, OSError:
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


async def _pkce_authenticate(session: SetupSession, client_id: str, step_id: str) -> dict[str, Any]:
    """
    Run the Spotify PKCE auth flow and return the token result (refresh + access token).

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
        token_result: dict[str, Any] = await response.json()
    if not token_result.get("refresh_token"):
        raise SetupFlowError("No refresh token in the token response")
    return token_result
