"""Interactive setup flow for the myNoise provider."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

import aiohttp
from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import ConfigEntryType

from music_assistant.models.setup_flow import SetupFlowError

from .catalog import patron_login
from .constants import CONF_PATRON_EMAIL

if TYPE_CHECKING:
    from music_assistant.models.setup_flow import SetupSession

_ENTRIES = (ConfigEntry(key=CONF_PATRON_EMAIL, type=ConfigEntryType.STRING, required=True),)


async def run_setup(session: SetupSession) -> None:
    """
    Collect the patron email, verify it with myNoise and create the instance.

    myNoise only grants access to its (donation-licensed) machines to donors, so
    the flow does not finish until myNoise has accepted the email. A rejected
    email re-prompts (and the outcome stays locked in the provider until the
    refresh action retries it); a site that cannot be reached simply re-prompts
    so the user can retry.
    """
    errors: dict[str, str | SetupFlowError] | None = None
    setup_data = dict(session.context.setup_data)
    while True:
        entries = [
            replace(entry, value=setup_data.get(entry.key, entry.value)) for entry in _ENTRIES
        ]
        submitted = await session.form(entries, step_id="user", errors=errors, last_step=True)
        setup_data.update(submitted)
        email = str(setup_data.get(CONF_PATRON_EMAIL, "")).strip()
        try:
            accepted = await patron_login(session.mass, email)
        except aiohttp.ClientError, TimeoutError, OSError:
            errors = {
                "base": SetupFlowError(
                    "Could not reach myNoise to verify the patron email, try again"
                )
            }
            continue
        if not accepted:
            errors = {
                "base": SetupFlowError(
                    "myNoise did not accept this email: it must match the address of "
                    "your myNoise donation"
                )
            }
            continue
        try:
            await session.finish(setup_data)
        except SetupFlowError as err:
            errors = {"base": err}
            continue
        return
