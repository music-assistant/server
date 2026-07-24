"""
Setup flow for the Alexa player provider.

NOTE: Amazon offers no usable OAuth for this integration. Authentication is a
credential-autofilling reverse proxy (alexapy's AlexaProxy) that routes Amazon's own
login pages through Music Assistant and captures the resulting session cookies. The
user still solves any CAPTCHA / 2FA on the real Amazon page. This flow is the closest
clean expression of that mechanism: one credentials form followed by one external step
that opens the proxied Amazon login.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import aiohttp
from aiohttp import web
from alexapy import AlexaLogin, AlexaProxy
from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import ConfigEntryType

from music_assistant.constants import CONF_PASSWORD, CONF_USERNAME
from music_assistant.models.setup_flow import SetupFlowError
from music_assistant.providers.alexa import (
    CONF_API_BASIC_AUTH_PASSWORD,
    CONF_API_BASIC_AUTH_USERNAME,
    CONF_API_URL,
    CONF_AUTH_SECRET,
    CONF_URL,
    save_cookie,
)

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigValueType

    from music_assistant.models.setup_flow import SetupSession

_SUCCESS_HTML = (
    "<html><body><h2>Login successful!</h2><p>You may now close this window.</p></body></html>"
)


async def run_setup(session: SetupSession) -> None:
    """
    Run the Alexa setup flow.

    Collects the Amazon + companion-API credentials, drives the proxied Amazon login and
    persists the credentials (the captured session cookie is stored out-of-band on disk).

    :param session: The setup session driving the flow.
    """
    collected = dict(session.context.setup_data)
    errors: dict[str, str] | None = None
    while True:
        values = await session.form(
            _credential_entries(collected), step_id="credentials", errors=errors, last_step=False
        )
        collected.update(values)
        login = AlexaLogin(
            url=str(values[CONF_URL]),
            email=str(values[CONF_USERNAME]),
            password=str(values[CONF_PASSWORD]),
            otp_secret=str(values.get(CONF_AUTH_SECRET) or ""),
            outputpath=lambda path: path,
        )
        if not await _proxy_login(session, login):
            errors = {"base": "login_failed"}
            continue
        # the captured session cookie is pickled to disk (keyed by username) before the
        # provider is loaded, since loaded_in_mass restores auth from that file
        await save_cookie(login, str(values[CONF_USERNAME]), session.mass)
        try:
            await session.finish(collected)
            return
        except SetupFlowError as err:
            errors = {"base": err.translation_key or str(err)}


def _credential_entries(prefill: dict[str, ConfigValueType]) -> list[ConfigEntry]:
    """Return the credential form entries, prefilling the non-secret fields."""
    return [
        ConfigEntry(
            key=CONF_URL,
            type=ConfigEntryType.STRING,
            required=True,
            default_value="amazon.com",
            value=prefill.get(CONF_URL),
        ),
        ConfigEntry(
            key=CONF_USERNAME,
            type=ConfigEntryType.STRING,
            required=True,
            value=prefill.get(CONF_USERNAME),
        ),
        ConfigEntry(key=CONF_PASSWORD, type=ConfigEntryType.SECURE_STRING, required=True),
        ConfigEntry(key=CONF_AUTH_SECRET, type=ConfigEntryType.SECURE_STRING, required=False),
        ConfigEntry(
            key=CONF_API_URL,
            type=ConfigEntryType.STRING,
            required=True,
            default_value="http://localhost:5000",
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


async def _proxy_login(session: SetupSession, login: AlexaLogin) -> bool:
    """
    Open the proxied Amazon login as an external step and report whether it succeeded.

    :param session: The setup session driving the flow.
    :param login: The AlexaLogin the reverse proxy autofills and captures cookies into.
    """
    proxy_path = f"/setup_flow/alexa_proxy/{session.flow_id}/"
    post_path = f"{proxy_path}ap/signin/*"
    proxy_url = f"{session.mass.webserver.base_url.rstrip('/')}{proxy_path}"
    proxy = AlexaProxy(login, proxy_url)

    async def proxy_handler(request: web.Request) -> web.StreamResponse:
        response = await proxy.all_handler(request)
        if "Successfully logged in" in getattr(response, "text", ""):
            # the proxy captured the cookies; poke the flow's callback to resume it
            async with aiohttp.ClientSession() as http:
                await http.get(session.callback_url)
            return web.Response(text=_SUCCESS_HTML, content_type="text/html")
        return cast("web.StreamResponse", response)

    session.mass.webserver.register_dynamic_route(proxy_path, proxy_handler, "GET")
    session.mass.webserver.register_dynamic_route(post_path, proxy_handler, "POST")
    try:
        await session.external(proxy_url, step_id="amazon_login", expires_in=300)
        return bool(await login.test_loggedin())
    finally:
        session.mass.webserver.unregister_dynamic_route(proxy_path, "GET")
        session.mass.webserver.unregister_dynamic_route(post_path, "POST")
