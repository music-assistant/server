"""Setup flow for the Youtube Music provider."""

from __future__ import annotations

import logging
from dataclasses import replace
from typing import TYPE_CHECKING

from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import ConfigEntryType
from music_assistant_models.errors import LoginFailed, SetupFailedError

from music_assistant.constants import CONF_USERNAME
from music_assistant.models.setup_flow import SetupFlowError
from music_assistant.providers.ytmusic import (
    CONF_COOKIE,
    CONF_PO_TOKEN_SERVER_URL,
    DEFAULT_PO_TOKEN_SERVER_URL,
)
from music_assistant.providers.ytmusic.helpers import (
    build_headers,
    normalize_cookie,
    ping_po_token_server,
    verify_cookie,
)

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigValueType

    from music_assistant.models.setup_flow import SetupSession

LOGGER = logging.getLogger(__name__)

_ENTRIES = (
    ConfigEntry(key=CONF_USERNAME, type=ConfigEntryType.STRING, required=True),
    ConfigEntry(
        key=CONF_COOKIE,
        type=ConfigEntryType.SECURE_STRING,
        required=True,
    ),
    ConfigEntry(
        key=CONF_PO_TOKEN_SERVER_URL,
        type=ConfigEntryType.STRING,
        default_value=DEFAULT_PO_TOKEN_SERVER_URL,
        required=True,
    ),
)


async def run_setup(session: SetupSession) -> None:
    """Run the setup flow: collect the credentials, verify them and create the provider."""
    errors: dict[str, str] | None = None
    setup_data = dict(session.context.setup_data)
    while True:
        entries = [
            replace(entry, value=setup_data.get(entry.key, entry.value)) for entry in _ENTRIES
        ]
        submitted = await session.form(entries, step_id="user", errors=errors, last_step=True)
        setup_data.update(submitted)
        if errors := await _validate(session, setup_data):
            continue
        try:
            await session.finish(setup_data)
            return
        except SetupFlowError as err:
            errors = {"base": err.translation_key or str(err)}


async def _validate(
    session: SetupSession, setup_data: dict[str, ConfigValueType]
) -> dict[str, str]:
    """Check the cookie and PO Token server before saving, returning field errors if any."""
    errors: dict[str, str] = {}
    # whatever was pasted (header, cURL command, cookies.txt) is stored in its canonical form
    cookie = normalize_cookie(str(setup_data.get(CONF_COOKIE) or ""))
    setup_data[CONF_COOKIE] = cookie
    try:
        await verify_cookie(build_headers(cookie))
    except LoginFailed as err:
        # the form only shows the localized slug; the reason YouTube refused the cookie
        # (consent page, changed payload, ...) is only ever found in the log
        LOGGER.warning("YouTube Music cookie check failed: %s", err, exc_info=err.__cause__)
        errors[CONF_COOKIE] = err.translation_key or str(err)
    except SetupFailedError as err:
        # YouTube unreachable or rate limiting: nothing wrong with the form values
        LOGGER.warning("YouTube Music cookie check failed: %s", err, exc_info=err.__cause__)
        errors["base"] = err.translation_key or str(err)
    po_token_url = str(setup_data.get(CONF_PO_TOKEN_SERVER_URL) or DEFAULT_PO_TOKEN_SERVER_URL)
    if not await ping_po_token_server(session.mass.http_session, po_token_url):
        errors[CONF_PO_TOKEN_SERVER_URL] = "po_token_server_unreachable"
    return errors
