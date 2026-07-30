"""Setup flow for the Last.fm (and Libre.fm) scrobble provider."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import TYPE_CHECKING

import pylast
from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption, ConfigValueType
from music_assistant_models.enums import ConfigEntryType
from music_assistant_models.errors import SetupFailedError

from music_assistant.models.setup_flow import SetupFlowError
from music_assistant.providers.lastfm_scrobble import (
    CONF_API_KEY,
    CONF_API_SECRET,
    CONF_PROVIDER,
    CONF_SESSION_KEY,
    CONF_USERNAME,
    _NetworkType,
    _resolve_credentials,
    get_network,
)

if TYPE_CHECKING:
    from music_assistant.models.setup_flow import SetupSession

# Last.fm ships with a built-in app (key/secret from app_vars); only Libre.fm - or a
# custom Last.fm developer app - needs the advanced key/secret fields below
CONF_ENTRY_PROVIDER = ConfigEntry(
    key=CONF_PROVIDER,
    type=ConfigEntryType.STRING,
    required=True,
    options=[
        ConfigValueOption(_NetworkType.LASTFM.value),
        ConfigValueOption(_NetworkType.LIBREFM.value),
    ],
    default_value=_NetworkType.LASTFM.value,
)
CONF_ENTRY_API_KEY = ConfigEntry(
    key=CONF_API_KEY, type=ConfigEntryType.SECURE_STRING, required=False, advanced=True
)
CONF_ENTRY_API_SECRET = ConfigEntry(
    key=CONF_API_SECRET, type=ConfigEntryType.SECURE_STRING, required=False, advanced=True
)


async def run_setup(session: SetupSession) -> None:
    """
    Run the Last.fm (or Libre.fm) scrobbler setup flow.

    Collects the network and, when overriding the built-in Last.fm app, its API
    credentials, then completes the web-auth handshake and persists the resulting
    (permanent) session key.

    :param session: The setup session driving the flow.
    """
    network_default = str(session.context.setup_data.get(CONF_PROVIDER, _NetworkType.LASTFM.value))
    errors: dict[str, str] | None = None
    while True:
        values = await session.form(
            [
                replace(CONF_ENTRY_PROVIDER, value=network_default),
                CONF_ENTRY_API_KEY,
                CONF_ENTRY_API_SECRET,
            ],
            step_id="user",
            errors=errors,
        )
        network_default = str(values[CONF_PROVIDER])
        network_type = _NetworkType(network_default)
        try:
            api_key, api_secret = _resolve_credentials(values, network_type)
        except SetupFailedError as err:
            errors = {"base": str(err)}
            continue

        network = await asyncio.to_thread(
            get_network, {**values, CONF_API_KEY: api_key, CONF_API_SECRET: api_secret}
        )
        # pylast only implements desktop auth; build the web-auth URL (with our own
        # dynamic callback) ourselves - the session key exchange below doesn't care
        # how the token was obtained
        url = f"{network.homepage}/api/auth/?api_key={network.api_key}&cb={session.callback_url}"
        params = await session.external(url, step_id="auth")
        if not (token := params.get("token")):
            errors = {"base": "auth_failed"}
            continue

        try:
            # blocking IO (and MD5 request signing, handled internally by pylast); the
            # url argument is unused by pylast here, only the token is
            session_key, username = await asyncio.to_thread(
                pylast.SessionKeyGenerator(network).get_web_auth_session_key_username, url, token
            )
        except pylast.PyLastError as err:
            errors = {"base": str(err)}
            continue

        finish_values: dict[str, ConfigValueType] = {
            CONF_PROVIDER: network_type.value,
            CONF_SESSION_KEY: session_key,
            CONF_USERNAME: username,
        }
        # only persist a custom key/secret when the user actually entered one; the
        # built-in Last.fm credentials are re-resolved from app_vars at runtime
        if values.get(CONF_API_KEY) and values.get(CONF_API_SECRET):
            finish_values[CONF_API_KEY] = api_key
            finish_values[CONF_API_SECRET] = api_secret
        try:
            await session.finish(finish_values)
            return
        except SetupFlowError as err:
            errors = {"base": err.translation_key or str(err)}
