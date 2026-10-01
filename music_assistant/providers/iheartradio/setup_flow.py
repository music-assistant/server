"""Setup flow for the iHeartRadio provider."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, Any

from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption
from music_assistant_models.enums import ConfigEntryType

from music_assistant.constants import CONF_PASSWORD, CONF_USERNAME
from music_assistant.models.setup_flow import SetupFlowError

from .constants import (
    API_BASE_URLS,
    CONF_COUNTRY,
    CONF_PROFILE_ID,
    CONF_SESSION_ID,
    CONF_SESSION_USERNAME,
    DEFAULT_COUNTRY,
)

if TYPE_CHECKING:
    from music_assistant.models.setup_flow import SetupSession

_ENTRIES = (
    ConfigEntry(
        key=CONF_COUNTRY,
        type=ConfigEntryType.STRING,
        required=True,
        default_value=DEFAULT_COUNTRY,
        options=[ConfigValueOption(country) for country in API_BASE_URLS],
    ),
    # signing in is optional and adds the account's followed stations and podcasts
    ConfigEntry(key=CONF_USERNAME, type=ConfigEntryType.STRING, required=False),
    ConfigEntry(key=CONF_PASSWORD, type=ConfigEntryType.SECURE_STRING, required=False),
)


async def run_setup(session: SetupSession) -> None:
    """Run the setup flow to pick the country and optionally sign in."""
    errors: dict[str, str | SetupFlowError] | None = None
    saved_setup_data = dict(session.context.setup_data)
    setup_data = dict(saved_setup_data)
    while True:
        entries = [
            replace(entry, value=setup_data.get(entry.key, entry.value)) for entry in _ENTRIES
        ]
        submitted = await session.form(entries, step_id="user", errors=errors, last_step=True)
        if not submitted.get(CONF_PASSWORD) and _same_account(saved_setup_data, submitted):
            submitted[CONF_PASSWORD] = saved_setup_data.get(CONF_PASSWORD)
        elif submitted.get(CONF_PASSWORD):
            # a saved session would be reused without checking the new password
            for key in (CONF_PROFILE_ID, CONF_SESSION_ID, CONF_SESSION_USERNAME):
                setup_data.pop(key, None)
        setup_data.update(submitted)
        try:
            await session.finish(setup_data)
            return
        except SetupFlowError as err:
            errors = {"base": err}


def _same_account(setup_data: dict[str, Any], submitted: dict[str, Any]) -> bool:
    """Return whether the submitted form keeps the account that is already saved."""
    saved = str(setup_data.get(CONF_USERNAME) or "").strip()
    return bool(saved) and saved == str(submitted.get(CONF_USERNAME) or "").strip()
