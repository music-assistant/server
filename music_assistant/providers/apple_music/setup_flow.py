"""Setup flow for the Apple Music provider."""

from __future__ import annotations

import pathlib
import re
from typing import TYPE_CHECKING

from aiohttp import ClientTimeout, web
from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import ConfigEntryType

from music_assistant.constants import CONF_ENTRY_UNOFFICIAL_PROVIDER
from music_assistant.models.setup_flow import AbortFlow, SetupFlowError

from .constants import (
    CONF_MUSIC_APP_TOKEN,
    CONF_MUSIC_USER_MANUAL_TOKEN,
    CONF_MUSIC_USER_TOKEN,
    CONF_MUSIC_USER_TOKEN_TIMESTAMP,
    MUSIC_APP_TOKEN,
)

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigValueType

    from music_assistant import MusicAssistant
    from music_assistant.models.setup_flow import SetupSession

# the MusicKit page self-closes and posts its (possibly empty) token this many seconds
# before the server-side external-step deadline, so the flow always resumes with params
MUSICKIT_FLOW_TIMEOUT = 600


async def run_setup(session: SetupSession) -> None:
    """
    Run the Apple Music setup flow.

    Resolves the Apple Music developer (app) token - preferring the bundled one and
    prompting for a manual token only when the bundle is empty/expired - then obtains a
    music user token via the MusicKit browser sign-in (or an advanced manual override)
    and persists them as setup data.

    :param session: The setup session driving the flow.
    """
    mass = session.mass
    collected: dict[str, ConfigValueType] = {}
    # prefer the bundled developer token; only prompt for (and store) a manual one when
    # the bundle ships an empty/expired token, e.g. on development builds
    app_token = MUSIC_APP_TOKEN
    if not await _app_token_valid(mass, app_token):
        app_token_errors: dict[str, str] | None = None
        while True:
            values = await session.form(
                [
                    ConfigEntry(
                        key=CONF_MUSIC_APP_TOKEN,
                        type=ConfigEntryType.SECURE_STRING,
                        required=True,
                    )
                ],
                step_id="app_token",
                errors=app_token_errors,
            )
            app_token = str(values.get(CONF_MUSIC_APP_TOKEN) or "")
            if await _app_token_valid(mass, app_token):
                break
            app_token_errors = {CONF_MUSIC_APP_TOKEN: "invalid_value"}
        collected[CONF_MUSIC_APP_TOKEN] = app_token

    errors: dict[str, str] | None = None
    while True:
        user_values = await session.form(
            [
                CONF_ENTRY_UNOFFICIAL_PROVIDER,
                ConfigEntry(
                    key=CONF_MUSIC_USER_MANUAL_TOKEN,
                    type=ConfigEntryType.SECURE_STRING,
                    required=False,
                    advanced=True,
                    help_link="https://www.music-assistant.io/music-providers/apple-music/",
                ),
            ],
            step_id="user",
            errors=errors,
        )
        manual_token = str(user_values.get(CONF_MUSIC_USER_MANUAL_TOKEN) or "").strip()
        attempt = dict(collected)
        if manual_token:
            # advanced escape hatch: a manual user token skips the browser sign-in
            # (e.g. child accounts, where MusicKit authorize() is unavailable)
            attempt[CONF_MUSIC_USER_MANUAL_TOKEN] = manual_token
        else:
            params = await _musickit_authenticate(session, app_token)
            token = params.get("music-user-token")
            if not token:
                # the page closed or timed out without returning a token
                raise AbortFlow("auth_cancelled")
            attempt[CONF_MUSIC_USER_TOKEN] = token
            # the callback stringifies posted values, so coerce the timestamp back to an
            # int; CONF_MUSIC_USER_TOKEN_TIMESTAMP is stored as INTEGER, not encrypted
            attempt[CONF_MUSIC_USER_TOKEN_TIMESTAMP] = int(
                params.get("music-user-token-timestamp", 0)
            )
        try:
            await session.finish(attempt)
            return
        except SetupFlowError as err:
            errors = {"base": err.translation_key or str(err)}


async def _app_token_valid(mass: MusicAssistant, app_token: str) -> bool:
    """
    Return whether the given Apple Music developer (app) token is accepted by the API.

    :param mass: The MusicAssistant instance.
    :param app_token: The developer (app) token to validate.
    """
    if not app_token:
        return False
    async with mass.http_session.get(
        "https://api.music.apple.com/v1/test",
        headers={"Authorization": f"Bearer {app_token}"},
        ssl=True,
        timeout=ClientTimeout(total=10),
    ) as response:
        return response.status == 200


def _validate_user_token(token: ConfigValueType) -> bool:
    """
    Return whether the given value looks like a (base64) Apple Music user token.

    :param token: The candidate music user token to check.
    """
    if not isinstance(token, str):
        return False
    return bool(re.findall(r"[a-zA-Z0-9=/+]{32,}==$", token))


async def _musickit_authenticate(session: SetupSession, app_token: str) -> dict[str, str]:
    """
    Serve the MusicKit JS sign-in page and return the params posted back to the flow.

    Registers the (flow-scoped) page/style/glue routes, sends the user to the page via an
    external step and always unregisters the routes afterwards. The page must stay
    MA-hosted: MusicKit's authorize() popup and postMessage need a real HTTP origin that
    can reach the local http callback.

    :param session: The setup session driving the flow.
    :param app_token: The (validated) developer token the MusicKit page configures with.
    """
    mass = session.mass
    asset_dir = pathlib.Path(__file__).parent.joinpath("musickit_auth")
    prefill = session.context.setup_data
    prefill_token = prefill.get(CONF_MUSIC_USER_TOKEN)
    user_token = prefill_token if _validate_user_token(prefill_token) else ""
    user_token_timestamp = prefill.get(CONF_MUSIC_USER_TOKEN_TIMESTAMP) or 0

    async def serve_mk_auth_page(request: web.Request) -> web.FileResponse:  # noqa: ARG001
        return web.FileResponse(
            asset_dir.joinpath("musickit_wrapper.html"),
            headers={"content-type": "text/html"},
        )

    async def serve_mk_auth_css(request: web.Request) -> web.FileResponse:  # noqa: ARG001
        return web.FileResponse(
            asset_dir.joinpath("musickit_wrapper.css"),
            headers={"content-type": "text/css"},
        )

    async def serve_mk_glue(request: web.Request) -> web.Response:  # noqa: ARG001
        glue = f"""
        const return_url='{session.callback_url}';
        const app_token='{app_token}';
        const callback_method='POST';
        const user_token='{user_token}';
        const user_token_timestamp='{user_token_timestamp}';
        const flow_timeout={max(MUSICKIT_FLOW_TIMEOUT - 10, 60)};
        const mass_version='{mass.version}';
        """
        return web.Response(body=glue, headers={"content-type": "text/javascript"})

    base_path = f"/apple_music_auth/{session.flow_id}/"
    unregister = [
        mass.webserver.register_dynamic_route(f"{base_path}index.html", serve_mk_auth_page),
        mass.webserver.register_dynamic_route(f"{base_path}index.css", serve_mk_auth_css),
        mass.webserver.register_dynamic_route(f"{base_path}index.js", serve_mk_glue),
    ]
    try:
        return await session.external(
            f"{mass.webserver.base_url}{base_path}index.html",
            step_id="auth",
            expires_in=MUSICKIT_FLOW_TIMEOUT,
        )
    finally:
        for remove in unregister:
            remove()
