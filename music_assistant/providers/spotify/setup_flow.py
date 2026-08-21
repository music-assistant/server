"""Setup flow for the Spotify provider."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import replace
from typing import TYPE_CHECKING, Any
from urllib.parse import urlencode

import aiohttp
import pkce
from aiohttp import ClientError
from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption
from music_assistant_models.enums import ConfigEntryType
from music_assistant_models.errors import LoginFailed

from music_assistant.constants import CONF_PROVIDERS
from music_assistant.helpers.app_vars import app_var
from music_assistant.helpers.oauth import (
    HOSTED_CALLBACK_URL,
    OAUTH_STEP_TIMEOUT,
    authorization_code_from_params,
    authorization_code_from_url,
    hosted_bounce_redirect,
)
from music_assistant.models.setup_flow import SetupFlowError, StepExpiredError
from music_assistant.providers.spotify_connect import (
    BACKEND_SOLOIST as CONNECT_BACKEND_SOLOIST,
)
from music_assistant.providers.spotify_connect import (
    CONF_API_KEY,
    CONF_MASS_PLAYER_ID,
    CONF_PUBLISH_NAME,
    CONF_SETUP_PENDING,
    CONF_SOLOIST_CONSENT,
    CONF_SYSTEM_MANAGED,
    DEFAULT_PUBLISH_NAME,
    PLAYER_ID_AUTO,
    SpotifyConnectProvider,
)
from music_assistant.providers.spotify_connect import (
    CONF_BACKEND as CONNECT_CONF_BACKEND,
)
from music_assistant.providers.spotify_connect.setup_flow import MIN_API_KEY_LENGTH
from music_assistant.providers.spotify_connect.soloist import (
    UnsupportedPlatformError,
    verify_platform_supported,
)

from .constants import (
    BACKEND_CONNECT,
    BACKEND_LIBRESPOT,
    CONF_CLIENT_ID,
    CONF_LIBRESPOT_CREDENTIALS,
    CONF_PLAYBACK_BACKEND,
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
    get_system_wide_connect_config_id,
    has_running_system_wide_connect,
    librespot_credentials_via_pairing,
    librespot_credentials_via_token,
)

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant
    from music_assistant.models.setup_flow import SetupSession

AUTHORIZE_URL = "https://accounts.spotify.com/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"

# How often the Connect pairing step polls the account's device list for the
# just-paired device to show up.
CONNECT_PAIR_POLL_INTERVAL = 5

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
    # playback authorization is separate from the Web API tokens and depends on the
    # explicitly chosen playback mode; the exchange's access token is reused for the
    # Connect account confirmation (minting a fresh one would rotate — and thereby
    # revoke — the refresh token just stored above)
    await _setup_playback(session, setup_data, str(token_result["access_token"]))
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


async def _setup_playback(
    session: SetupSession, setup_data: dict[str, Any], access_token: str
) -> None:
    """
    Choose the playback mode and run its branch.

    :param session: The setup session driving the flow.
    :param setup_data: The setup data collected so far, updated in place.
    :param access_token: A live Web API access token for this account (used by the
        Connect branch to confirm the paired device belongs to this account).
    """
    stored_backend = str(setup_data.get(CONF_PLAYBACK_BACKEND) or "")
    if stored_backend:
        preselect = stored_backend
    elif session.context.instance_id:
        # an instance predating the mode choice runs librespot; a routine
        # reconfigure must not silently change the playback path
        preselect = BACKEND_LIBRESPOT
    else:
        # fresh setup: prefer Connect when a Spotify Connect instance already runs
        preselect = (
            BACKEND_CONNECT
            if session.mass.get_provider("spotify_connect") is not None
            else BACKEND_LIBRESPOT
        )
    errors: dict[str, str] | None = None
    while True:
        selected = await _choose_playback_backend(session, preselect, errors)
        errors = None
        if selected == BACKEND_CONNECT:
            try:
                if not await _setup_connect_playback(session, access_token):
                    # consent refused: back to the mode choice with a clear error
                    preselect = BACKEND_CONNECT
                    errors = {"base": "soloist_consent_required"}
                    continue
            except UnsupportedPlatformError:
                preselect = BACKEND_LIBRESPOT
                errors = {"base": "soloist_unsupported_platform"}
                continue
            except SetupFlowError as err:
                preselect = BACKEND_CONNECT
                errors = {"base": err.translation_key or str(err)}
                continue
            # the librespot credential is of no further use; it only reaches the
            # stored setup_data when finish() succeeds, so an aborted or failed
            # switch keeps it intact
            setup_data[CONF_LIBRESPOT_CREDENTIALS] = None
        else:
            setup_data[CONF_LIBRESPOT_CREDENTIALS] = await _authorize_playback(session)
        setup_data[CONF_PLAYBACK_BACKEND] = selected
        return


async def _choose_playback_backend(
    session: SetupSession, preselect: str, errors: dict[str, str] | None
) -> str:
    """
    Show the playback mode choice step and return the selected mode.

    :param session: The setup session driving the flow.
    :param preselect: Mode to preselect (the stored or default one).
    :param errors: Optional errors to display on the first render.
    """
    values = await session.form(
        [
            ConfigEntry(
                key=CONF_PLAYBACK_BACKEND,
                type=ConfigEntryType.STRING,
                required=True,
                default_value=BACKEND_LIBRESPOT,
                value=preselect,
                options=[
                    ConfigValueOption(BACKEND_LIBRESPOT),
                    ConfigValueOption(BACKEND_CONNECT),
                ],
            ),
        ],
        step_id="playback_backend",
        errors=errors,
    )
    return str(values[CONF_PLAYBACK_BACKEND])


async def _setup_connect_playback(session: SetupSession, access_token: str) -> bool:
    """
    Set up the system-wide Spotify Connect instance inline and pair it to this account.

    A running instance already paired to this account is reused as-is. Otherwise the
    instance is provisioned with the official Soloist engine (the at-own-risk warning
    and API key are collected right here), started, and paired: the user selects the
    device in their Spotify app, confirmed by the device appearing in this account's
    own device list — which also proves the session belongs to the account that just
    authenticated. Playback redirects need a logged-in session, so the flow only
    completes once that confirmation arrived.

    :param session: The setup session driving the flow.
    :param access_token: A live Web API access token for this account.
    :return: True when set up and paired, False when the Soloist consent was refused.
    :raises UnsupportedPlatformError: When Soloist cannot run on this system.
    :raises SetupFlowError: When the instance could not be started.
    """
    mass = session.mass
    instance_id = await get_system_wide_connect_config_id(mass)
    publish_name = (
        str(mass.config.get_provider_setup_value(instance_id, CONF_PUBLISH_NAME) or "")
        if instance_id
        else ""
    ) or DEFAULT_PUBLISH_NAME
    if has_running_system_wide_connect(mass):
        if await _find_connect_device(mass, access_token, publish_name):
            # running and already paired to this account: nothing to do
            await session.form(
                [ConfigEntry(key="connect_ready", type=ConfigEntryType.LABEL)],
                step_id="connect_mode",
            )
            await _stamp_verified_connect_account(mass, access_token, instance_id)
            return True
    else:
        stored_engine = (
            str(mass.config.get_provider_setup_value(instance_id, CONNECT_CONF_BACKEND) or "")
            if instance_id
            else ""
        )
        if stored_engine:
            # a configured instance exists but is not running: start it as-is
            await _start_connect_instance(session, instance_id)
        else:
            # fresh instance: the official Soloist engine is set up, guarded by its
            # at-own-risk warning/consent and the personal API key
            verify_platform_supported()
            consented = bool(
                instance_id
                and mass.config.get_provider_setup_value(instance_id, CONF_SOLOIST_CONSENT)
            )
            if not await _ask_connect_consent(session, consented):
                return False
            has_stored_key = bool(
                instance_id and mass.config.get_provider_setup_value(instance_id, CONF_API_KEY)
            )
            key_errors: dict[str, str] | None = None
            while True:
                collected: dict[str, Any] = {
                    CONNECT_CONF_BACKEND: CONNECT_BACKEND_SOLOIST,
                    CONF_SOLOIST_CONSENT: True,
                }
                await _ask_connect_api_key(session, collected, has_stored_key, key_errors)
                try:
                    instance_id = await _provision_connect_instance(session, instance_id, collected)
                    break
                except SetupFlowError as err:
                    key_errors = {"base": err.translation_key or str(err)}
    # pairing: the user selects the device in the Spotify app; the device showing
    # up in this account's own device list confirms both the pairing and that the
    # session belongs to this account
    while True:
        try:
            await session.progress_until(
                _await_connect_device(mass, access_token, publish_name),
                step_id="connect_pairing",
                text="connect_pairing_instructions",
                expires_in=PAIRING_TIMEOUT,
            )
            break
        except StepExpiredError:
            await session.form(
                [ConfigEntry(key="connect_pairing_retry", type=ConfigEntryType.LABEL)],
                step_id="connect_pairing_retry",
                errors={"base": "connect_pairing_not_completed"},
            )
    await _stamp_verified_connect_account(mass, access_token, instance_id)
    return True


async def _start_connect_instance(session: SetupSession, instance_id: str | None) -> None:
    """
    (Re)start an already-configured Connect instance as-is.

    :param session: The setup session driving the flow.
    :param instance_id: The configured system-wide instance to start.
    :raises SetupFlowError: When the instance did not start.
    """
    assert instance_id is not None  # guarded by the caller
    try:
        config = await session.mass.config.get_provider_config(instance_id)
        await session.mass.load_provider_config(config)
    except Exception as err:
        raise SetupFlowError(
            str(err) or err.__class__.__name__,
            translation_key=getattr(err, "translation_key", None),
        ) from err


async def _find_connect_device(mass: MusicAssistant, access_token: str, device_name: str) -> bool:
    """
    Return whether this account's Connect device list holds a device with the given name.

    :param mass: The MusicAssistant instance.
    :param access_token: A live Web API access token for this account.
    :param device_name: The published device name to look for.
    """
    try:
        async with mass.http_session.get(
            "https://api.spotify.com/v1/me/player/devices",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=aiohttp.ClientTimeout(total=30),
        ) as response:
            if response.status != 200:
                return False
            data = await response.json()
    except aiohttp.ClientError, TimeoutError:
        return False
    return any(device.get("name") == device_name for device in data.get("devices", []))


async def _await_connect_device(mass: MusicAssistant, access_token: str, device_name: str) -> None:
    """
    Wait until a Connect device with the given name appears in this account's device list.

    :param mass: The MusicAssistant instance.
    :param access_token: A live Web API access token for this account.
    :param device_name: The published device name to wait for.
    """
    while not await _find_connect_device(mass, access_token, device_name):
        await asyncio.sleep(CONNECT_PAIR_POLL_INTERVAL)


async def _stamp_verified_connect_account(
    mass: MusicAssistant, access_token: str, instance_id: str | None
) -> None:
    """
    Record the just-confirmed account on the running Connect instance (best effort).

    Saves the first playback redirect its verification round-trip; failures are
    ignored — the redirect verifies on demand anyway.

    :param mass: The MusicAssistant instance.
    :param access_token: A live Web API access token for this account.
    :param instance_id: The Connect instance the account was confirmed for.
    """
    if instance_id is None:
        return
    plugin = mass.get_provider(instance_id)
    if not isinstance(plugin, SpotifyConnectProvider):
        return
    with suppress(aiohttp.ClientError, TimeoutError, KeyError):
        async with mass.http_session.get(
            "https://api.spotify.com/v1/me",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=aiohttp.ClientTimeout(total=30),
        ) as response:
            if response.status != 200:
                return
            userinfo = await response.json()
        plugin.set_verified_account_id(str(userinfo["id"]))


async def _ask_connect_consent(session: SetupSession, prefill: bool) -> bool:
    """
    Show the Soloist warning/consent step and return whether consent was given.

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


async def _ask_connect_api_key(
    session: SetupSession,
    collected: dict[str, Any],
    has_stored_key: bool,
    errors: dict[str, str] | None = None,
) -> None:
    """
    Collect the Soloist API key for the Connect instance.

    An already stored key is kept when the field is left empty; it is never shown
    back to the user.

    :param session: The setup session driving the flow.
    :param collected: The values collected so far, updated in place.
    :param has_stored_key: Whether the instance already stores an API key.
    :param errors: Optional errors to display on the first render.
    """
    while True:
        entries = [
            ConfigEntry(
                key=CONF_API_KEY,
                type=ConfigEntryType.SECURE_STRING,
                required=not has_stored_key,
            ),
        ]
        if has_stored_key:
            entries.insert(0, ConfigEntry(key="soloist_api_key_hint", type=ConfigEntryType.LABEL))
        values = await session.form(entries, step_id="soloist_api_key", errors=errors)
        api_key = str(values.get(CONF_API_KEY) or "").strip()
        if api_key or not has_stored_key:
            if len(api_key) < MIN_API_KEY_LENGTH:
                errors = {CONF_API_KEY: "soloist_api_key_invalid"}
                continue
            collected[CONF_API_KEY] = api_key
        return


async def _provision_connect_instance(
    session: SetupSession, instance_id: str | None, collected: dict[str, Any]
) -> str:
    """
    Create or complete the system-wide Connect instance and start it.

    Returns the instance id (also for a just-created instance, so a retry after a
    failure completes that instance instead of creating another one).

    :param session: The setup session driving the flow.
    :param instance_id: The existing system-wide instance to complete, if any.
    :param collected: The engine settings collected by the inline steps.
    :raises SetupFlowError: When the instance did not start with these settings.
    """
    mass = session.mass
    values: dict[str, Any] = {
        CONF_MASS_PLAYER_ID: PLAYER_ID_AUTO,
        CONF_SYSTEM_MANAGED: True,
        CONF_SETUP_PENDING: False,
        **collected,
    }
    encrypted = {
        key: mass.config.encrypt_string(value) if isinstance(value, str) else value
        for key, value in values.items()
    }
    if instance_id is None:
        config = await mass.config.create_pending_provider_config("spotify_connect", encrypted)
        instance_id = config.instance_id
    else:
        conf_key = f"{CONF_PROVIDERS}/{instance_id}/setup_data"
        existing = dict(mass.config.get(conf_key) or {})
        mass.config.set(conf_key, {**existing, **encrypted})
        try:
            config = await mass.config.get_provider_config(instance_id)
            await mass.load_provider_config(config)
        except Exception as err:
            raise SetupFlowError(
                str(err) or err.__class__.__name__,
                translation_key=getattr(err, "translation_key", None),
            ) from err
    if mass.get_provider(instance_id) is None:
        # the create path records the load failure instead of raising; surface it
        last_error = mass.config.get(f"{CONF_PROVIDERS}/{instance_id}/last_error") or {}
        raise SetupFlowError(
            str(last_error.get("message") or "The Spotify Connect instance failed to start"),
            translation_key=last_error.get("translation_key"),
        )
    return instance_id


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
