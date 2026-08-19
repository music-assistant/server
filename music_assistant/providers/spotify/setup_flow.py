"""Setup flow for the Spotify provider."""

from __future__ import annotations

import asyncio
import shutil
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlencode

import pkce
from aiohttp import ClientError
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
from music_assistant.providers.spotify_connect.soloist import (
    SoloistError,
    UnsupportedPlatformError,
    verify_platform_supported,
)

from .constants import (
    BACKEND_LIBRESPOT,
    BACKEND_SOLOIST,
    CONF_CLIENT_ID,
    CONF_LIBRESPOT_CREDENTIALS,
    CONF_PLAYBACK_BACKEND,
    CONF_REFRESH_TOKEN_DEV,
    CONF_REFRESH_TOKEN_GLOBAL,
    CONF_SOLOIST_API_KEY,
    CONF_SOLOIST_CONSENT,
    CONF_SOLOIST_SESSION_DIR,
    KEYMASTER_CLIENT_ID,
    LIBRESPOT_REDIRECT_PATH,
    LIBRESPOT_REDIRECT_PORT,
    LIBRESPOT_REDIRECT_URI,
    LIBRESPOT_SCOPE,
    LOOPBACK_WAIT_TIMEOUT,
    PAIRING_DEVICE_NAME,
    PAIRING_TIMEOUT,
    SCOPE,
    SOLOIST_DATA_DIR_NAME,
    SOLOIST_PAIRING_DIR,
)
from .helpers import (
    await_loopback_authorization,
    get_librespot_binary,
    librespot_credentials_via_pairing,
    librespot_credentials_via_token,
    pair_soloist_session,
    soloist_session_present,
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

CONF_SOLOIST_REPAIR = "soloist_repair"

# Minimum plausible length of a pasted Soloist API key: anything shorter is a
# partial paste. No further format rules are applied locally — Spotify rejects
# an invalid key when soloist authenticates.
MIN_API_KEY_LENGTH = 16


async def run_setup(session: SetupSession) -> None:
    """
    Run the Spotify setup flow.

    Authenticates the (required) global session with Music Assistant's own client id, authorizes
    playback separately, then optionally a developer session with the user's own client id, and
    persists the resulting tokens and credentials as setup data.

    :param session: The setup session driving the flow.
    """
    setup_data = dict(session.context.setup_data)
    try:
        # the global session always (re)authenticates: a refresh token cannot be reused across a
        # re-auth and secure values are never prefilled back into the flow
        setup_data[CONF_REFRESH_TOKEN_GLOBAL] = await _pkce_authenticate(
            session, app_var("spotify_client_id"), step_id="authenticate"
        )
        # playback authorization is separate from the Web API tokens and depends on
        # the explicitly chosen playback backend
        await _setup_playback(session, setup_data)
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
    finally:
        if not session.finished:
            # a soloist session paired by this flow holds reusable login
            # material; never leave it behind when the flow did not complete
            await _discard_pairing_dir(session)


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
            setup_data[CONF_REFRESH_TOKEN_DEV] = await _pkce_authenticate(
                session, client_id, step_id="authenticate_dev"
            )
        else:
            # opted in but left the field empty: keep using the shared key
            setup_data[CONF_CLIENT_ID] = None
            setup_data[CONF_REFRESH_TOKEN_DEV] = None
        await session.finish(setup_data)
    except SetupFlowError as err:
        return client_id, {"base": err.translation_key or str(err)}
    return client_id, None


async def _setup_playback(session: SetupSession, setup_data: dict[str, Any]) -> None:
    """
    Choose the playback backend and run its authorization branch.

    :param session: The setup session driving the flow.
    :param setup_data: The setup data collected so far, updated in place.
    """
    stored_backend = str(setup_data.get(CONF_PLAYBACK_BACKEND) or "")
    if stored_backend:
        preselect = stored_backend
    elif session.context.instance_id:
        # an instance predating the backend choice runs librespot; a routine
        # reconfigure must not nudge it onto another playback path
        preselect = BACKEND_LIBRESPOT
    else:
        preselect = _default_backend()
    errors: dict[str, str] | None = None
    while True:
        selected = await _choose_playback_backend(session, preselect, errors)
        errors = None
        if selected == BACKEND_SOLOIST:
            if not await _authorize_soloist(session, setup_data):
                # consent refused: back to the backend choice with a clear error
                preselect = BACKEND_SOLOIST
                errors = {"base": "soloist_consent_required"}
                continue
            # the librespot credential is of no further use
            setup_data[CONF_LIBRESPOT_CREDENTIALS] = None
        else:
            setup_data[CONF_LIBRESPOT_CREDENTIALS] = await _authorize_playback(session)
            # switching away from soloist: overwrite the soloist secrets; they
            # only reach the stored setup_data when finish() succeeds, so an
            # aborted or failed switch keeps them intact
            setup_data[CONF_SOLOIST_API_KEY] = None
            setup_data[CONF_SOLOIST_CONSENT] = False
            setup_data[CONF_SOLOIST_SESSION_DIR] = None
        setup_data[CONF_PLAYBACK_BACKEND] = selected
        return


def _default_backend() -> str:
    """Return the backend to preselect for a fresh setup."""
    try:
        verify_platform_supported()
    except UnsupportedPlatformError:
        return BACKEND_LIBRESPOT
    # Spotify's official client: the only option for accounts librespot cannot
    # serve (created since late 2024), hence the recommended default
    return BACKEND_SOLOIST


async def _choose_playback_backend(
    session: SetupSession, preselect: str, errors: dict[str, str] | None
) -> str:
    """
    Show the playback backend choice step until a usable backend is selected.

    :param session: The setup session driving the flow.
    :param preselect: Backend to preselect (the stored or previously chosen one).
    :param errors: Optional errors to display on the first render.
    """
    while True:
        values = await session.form(
            [
                ConfigEntry(
                    key=CONF_PLAYBACK_BACKEND,
                    type=ConfigEntryType.STRING,
                    required=True,
                    default_value=BACKEND_LIBRESPOT,
                    value=preselect,
                    options=[
                        ConfigValueOption(BACKEND_SOLOIST),
                        ConfigValueOption(BACKEND_LIBRESPOT),
                    ],
                ),
            ],
            step_id="playback_backend",
            errors=errors,
        )
        selected = str(values[CONF_PLAYBACK_BACKEND])
        if selected == BACKEND_SOLOIST:
            try:
                verify_platform_supported()
            except UnsupportedPlatformError:
                errors = {"base": "soloist_unsupported_platform"}
                preselect = BACKEND_LIBRESPOT
                continue
        return selected


async def _authorize_soloist(session: SetupSession, setup_data: dict[str, Any]) -> bool:
    """
    Run the soloist branch: consent, API key and account pairing.

    :param session: The setup session driving the flow.
    :param setup_data: The setup data collected so far, updated in place.
    :return: True when the branch completed, False when consent was refused.
    """
    if not await _ask_soloist_consent(session, bool(setup_data.get(CONF_SOLOIST_CONSENT))):
        return False
    setup_data[CONF_SOLOIST_CONSENT] = True
    # an existing paired session can be kept on reconfigure; the API key can
    # still be updated either way
    keep_session = await _has_existing_soloist_session(session) and not await _ask_soloist_repair(
        session
    )
    errors: dict[str, str] | None = None
    while True:
        await _ask_soloist_api_key(session, setup_data, errors)
        if keep_session:
            setup_data[CONF_SOLOIST_SESSION_DIR] = None
            return True
        try:
            await _pair_soloist(session, setup_data)
            return True
        except StepExpiredError:
            errors = {"base": "soloist_pairing_not_completed"}
        except SoloistError as err:
            # download/refresh problems carry their own translation keys
            errors = {"base": err.translation_key or "soloist_pairing_failed"}
        except LoginFailed:
            # a rejected key is the most likely cause; re-show the key step
            errors = {"base": "soloist_pairing_failed"}


async def _ask_soloist_consent(session: SetupSession, prefill: bool) -> bool:
    """
    Show the soloist warning/consent step and return whether consent was given.

    :param session: The setup session driving the flow.
    :param prefill: Whether consent was already given on an earlier run.
    """
    values = await session.form(
        [
            ConfigEntry(
                key=CONF_SOLOIST_CONSENT,
                type=ConfigEntryType.BOOLEAN,
                required=False,
                default_value=False,
                value=prefill,
            ),
        ],
        step_id="soloist_terms",
    )
    return bool(values.get(CONF_SOLOIST_CONSENT))


async def _ask_soloist_api_key(
    session: SetupSession, setup_data: dict[str, Any], errors: dict[str, str] | None = None
) -> None:
    """
    Collect the Soloist API key.

    An already stored key (reconfigure) is kept when the field is left empty;
    it is never shown back to the user.

    :param session: The setup session driving the flow.
    :param setup_data: The setup data collected so far, updated in place.
    :param errors: Optional errors to display on the first render.
    """
    has_stored_key = bool(setup_data.get(CONF_SOLOIST_API_KEY))
    while True:
        entries = [
            ConfigEntry(
                key=CONF_SOLOIST_API_KEY,
                type=ConfigEntryType.SECURE_STRING,
                required=not has_stored_key,
            ),
        ]
        if has_stored_key:
            entries.insert(0, ConfigEntry(key="soloist_api_key_hint", type=ConfigEntryType.LABEL))
        values = await session.form(entries, step_id="soloist_api_key", errors=errors)
        api_key = str(values.get(CONF_SOLOIST_API_KEY) or "").strip()
        if api_key or not has_stored_key:
            if len(api_key) < MIN_API_KEY_LENGTH:
                errors = {CONF_SOLOIST_API_KEY: "soloist_api_key_invalid"}
                continue
            setup_data[CONF_SOLOIST_API_KEY] = api_key
        return


async def _has_existing_soloist_session(session: SetupSession) -> bool:
    """Return whether the instance being reconfigured already has a paired session."""
    if not session.context.instance_id:
        return False
    data_dir = (
        Path(session.mass.storage_path)
        / "spotify"
        / session.context.instance_id
        / SOLOIST_DATA_DIR_NAME
    )
    return await asyncio.to_thread(soloist_session_present, data_dir)


async def _ask_soloist_repair(session: SetupSession) -> bool:
    """Ask whether the existing paired session should be replaced by a new pairing."""
    values = await session.form(
        [
            ConfigEntry(
                key=CONF_SOLOIST_REPAIR,
                type=ConfigEntryType.BOOLEAN,
                required=False,
                default_value=False,
            ),
        ],
        step_id="soloist_repair",
    )
    return bool(values.get(CONF_SOLOIST_REPAIR))


async def _pair_soloist(session: SetupSession, setup_data: dict[str, Any]) -> None:
    """
    Pair the Spotify account through the Spotify app and record the session dir.

    The session is paired into a flow-private directory (this flow may be setting
    up a brand new instance that has no instance id yet); the provider adopts it
    into its per-instance data dir on the next load.

    :param session: The setup session driving the flow.
    :param setup_data: The setup data collected so far, updated in place.
    """
    pairing_dir = f"{SOLOIST_PAIRING_DIR}/{session.flow_id}"
    api_key = str(setup_data.get(CONF_SOLOIST_API_KEY) or "")
    await session.progress_until(
        pair_soloist_session(session.mass, api_key, Path(session.mass.storage_path) / pairing_dir),
        step_id="soloist_pairing",
        text="soloist_pairing_instructions",
        expires_in=PAIRING_TIMEOUT,
    )
    setup_data[CONF_SOLOIST_SESSION_DIR] = pairing_dir


async def _discard_pairing_dir(session: SetupSession) -> None:
    """Remove this flow's private pairing directory, if it created one."""
    pairing_dir = Path(session.mass.storage_path) / SOLOIST_PAIRING_DIR / session.flow_id
    await asyncio.to_thread(shutil.rmtree, pairing_dir, ignore_errors=True)


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
