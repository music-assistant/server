"""
Setup flow for the Audible provider.

Audible sign-in happens on Amazon's own web page (which handles the password, any
CAPTCHA and OTP), so Music Assistant cannot receive an OAuth callback. The flow instead
sends the user to Amazon's authorize URL and asks them to paste the resulting
"page not found" redirect URL, from which the device is registered. The registration
tokens are written to a file under the MA storage path; only that file path (and the
marketplace locale) are persisted as setup data.
"""

from __future__ import annotations

import asyncio
import os
from contextlib import suppress
from typing import TYPE_CHECKING
from uuid import uuid4

from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption
from music_assistant_models.enums import ConfigEntryType
from music_assistant_models.errors import LoginFailed

from music_assistant.models.setup_flow import SetupFlowError

from . import CONF_AUTH_FILE, CONF_LOCALE
from .audible_helper import (
    audible_custom_login,
    audible_get_auth_info,
    check_file_exists,
    deregister_auth_file,
    evict_cached_authenticator,
    remove_file,
)

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigValueType

    from music_assistant.models.setup_flow import SetupSession

# form field key of the pasted post-login ("page not found") redirect URL
CONF_POST_LOGIN_URL = "post_login_url"

_LOCALES = ("us", "ca", "uk", "au", "fr", "de", "jp", "it", "in", "es", "br")


async def run_setup(session: SetupSession) -> None:
    """
    Run the Audible sign-in flow: marketplace, then authorize + paste redirect URL.

    :param session: The setup session driving the flow.
    """
    locale_default = str(session.context.setup_data.get(CONF_LOCALE) or "us")
    values = await session.form(
        [
            ConfigEntry(
                key=CONF_LOCALE,
                type=ConfigEntryType.STRING,
                required=True,
                default_value="us",
                value=locale_default,
                options=[ConfigValueOption(loc) for loc in _LOCALES],
            ),
        ],
        step_id="user",
    )
    locale = str(values[CONF_LOCALE])

    errors: dict[str, str] | None = None
    while True:
        # a fresh authorize URL (+ PKCE verifier + device serial) per attempt, since the
        # pasted redirect carries a single-use authorization code
        code_verifier, login_url, serial = await audible_get_auth_info(locale)
        values = await session.form(
            [
                ConfigEntry(
                    key="auth_link",
                    type=ConfigEntryType.LABEL,
                    translation_params=[login_url],
                ),
                ConfigEntry(
                    key=CONF_POST_LOGIN_URL,
                    type=ConfigEntryType.STRING,
                    required=True,
                ),
            ],
            step_id="authenticate",
            last_step=True,
            errors=errors,
        )
        post_login_url = str(values[CONF_POST_LOGIN_URL])
        try:
            auth = await audible_custom_login(code_verifier, post_login_url, serial, locale)
            if not (auth.adp_token and auth.device_private_key):
                raise LoginFailed(
                    "Registration succeeded but signing keys were not obtained. Please try again."
                )
        except LoginFailed as err:
            errors = {"base": err.translation_key or str(err)}
            continue
        except Exception as err:
            errors = {"base": str(err)}
            continue
        auth_file_path = os.path.join(session.mass.storage_path, f"audible_auth_{uuid4().hex}.json")
        await asyncio.to_thread(auth.to_file, auth_file_path)
        collected: dict[str, ConfigValueType] = {
            CONF_AUTH_FILE: auth_file_path,
            CONF_LOCALE: locale,
        }
        try:
            await session.finish(collected)
        except SetupFlowError as err:
            # the just-written token file is unusable if the load failed; drop it
            evict_cached_authenticator(auth_file_path)
            await remove_file(auth_file_path)
            errors = {"base": err.translation_key or str(err)}
            continue
        # the new registration replaces the previous one; retire the old device
        # registration and its token file
        previous_auth_file = str(session.context.setup_data.get(CONF_AUTH_FILE) or "")
        if previous_auth_file and previous_auth_file != auth_file_path:
            with suppress(Exception):
                await deregister_auth_file(previous_auth_file)
            if await check_file_exists(previous_auth_file):
                with suppress(OSError):
                    await remove_file(previous_auth_file)
        return
