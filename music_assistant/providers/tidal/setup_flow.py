"""
Setup flow for the Tidal music provider.

Tidal uses OAuth PKCE, but its authorize page redirects to Tidal's own
"Page Not Found" page instead of a callback Music Assistant can receive. The flow
therefore shows the authorize link and a field for the user to paste that redirected
URL, then exchanges it for tokens. The PKCE ``code_verifier`` lives only as a local
for the duration of the flow (it used to be smuggled through a hidden config value).
"""

from __future__ import annotations

import asyncio
import json
from contextlib import suppress
from typing import TYPE_CHECKING

from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import ConfigEntryType
from music_assistant_models.errors import LoginFailed

from music_assistant.models.setup_flow import SetupFlowError, StepExpiredError

from .auth_manager import TidalAuthManager
from .constants import (
    CONF_AUTH_TOKEN,
    CONF_EXPIRY_TIME,
    CONF_OOPS_URL,
    CONF_QUALITY,
    CONF_REFRESH_TOKEN,
    CONF_USER_ID,
)

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigValueType

    from music_assistant.models.setup_flow import SetupSession

DEFAULT_QUALITY = "HI_RES_LOSSLESS"


async def run_setup(session: SetupSession) -> None:
    """
    Run the Tidal PKCE login flow: show the authorize link, exchange the pasted URL.

    :param session: The setup session driving the flow.
    """
    # quality stays a genuine option; it is only carried through the exchange, so a
    # (reconfigure) prefill or the default is enough here - the user edits it later
    quality = str(session.context.values.get(CONF_QUALITY) or DEFAULT_QUALITY)
    # the PKCE verifier now lives only in this local auth_params blob
    authorize_url, auth_params = TidalAuthManager.build_pkce_login(quality)

    errors: dict[str, str] | None = None
    while True:
        values = await session.form(
            [
                ConfigEntry(
                    key="auth_instructions",
                    type=ConfigEntryType.LABEL,
                )
            ],
            step_id="user",
            errors=errors,
        )
        with suppress(StepExpiredError, OSError):
            await session.external_until(
                asyncio.Future(),
                authorize_url,
                step_id="authorize",
                expires_in=30,
            )
        values = await session.form(
            [
                ConfigEntry(key=CONF_OOPS_URL, type=ConfigEntryType.STRING, required=True),
            ],
            step_id="user",
            errors=errors,
            last_step=True,
        )
        oops_url = str(values[CONF_OOPS_URL])
        try:
            auth_data = await TidalAuthManager.process_pkce_login(
                session.mass.http_session, json.dumps(auth_params), oops_url
            )
        except LoginFailed as err:
            errors = {"base": err.translation_key or str(err)}
            continue
        collected: dict[str, ConfigValueType] = {
            CONF_AUTH_TOKEN: auth_data["access_token"],
            CONF_REFRESH_TOKEN: auth_data["refresh_token"],
            CONF_EXPIRY_TIME: auth_data["expires_at"],
            CONF_USER_ID: str(auth_data["userId"]),
        }
        try:
            await session.finish(collected)
            return
        except SetupFlowError as err:
            errors = {"base": err.translation_key or str(err)}
