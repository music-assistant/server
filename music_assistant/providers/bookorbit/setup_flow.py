"""Setup flow for the BookOrbit provider."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, Any

from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import ConfigEntryType

from music_assistant.constants import CONF_PASSWORD, CONF_USERNAME, CONF_VERIFY_SSL
from music_assistant.models.setup_flow import SetupFlowError
from music_assistant.providers.bookorbit.constants import CONF_URL

if TYPE_CHECKING:
    from music_assistant.models.setup_flow import SetupSession

_ENTRIES = (
    ConfigEntry(
        key="label",
        type=ConfigEntryType.LABEL,
    ),
    ConfigEntry(
        key=CONF_URL,
        type=ConfigEntryType.STRING,
        required=True,
    ),
    ConfigEntry(
        key=CONF_USERNAME,
        type=ConfigEntryType.STRING,
        required=True,
    ),
    ConfigEntry(
        key=CONF_PASSWORD,
        type=ConfigEntryType.SECURE_STRING,
        required=False,
    ),
    ConfigEntry(
        key=CONF_VERIFY_SSL,
        type=ConfigEntryType.BOOLEAN,
        required=False,
        advanced=True,
        default_value=True,
    ),
)


def _same_username(saved: dict[str, Any], submitted: dict[str, Any]) -> bool:
    """Return whether the submitted form keeps the username that is already saved."""
    saved_username = str(saved.get(CONF_USERNAME) or "").strip()
    return (
        bool(saved_username) and saved_username == str(submitted.get(CONF_USERNAME) or "").strip()
    )


async def run_setup(session: SetupSession) -> None:
    """Run the setup flow: collect the connection details and create the provider."""
    errors: dict[str, str | SetupFlowError] | None = None
    setup_data = dict(session.context.setup_data)
    while True:
        entries = [
            replace(entry, value=setup_data.get(entry.key, entry.value)) for entry in _ENTRIES
        ]
        submitted = await session.form(entries, step_id="user", errors=errors, last_step=True)
        # Blank password on retry must keep the in-progress password (e.g. a replacement
        # entered before a transient load failure), not only the originally saved one.
        if not submitted.get(CONF_PASSWORD) and _same_username(setup_data, submitted):
            submitted = dict(submitted)
            submitted[CONF_PASSWORD] = setup_data.get(CONF_PASSWORD)
        prior_username = setup_data.get(CONF_USERNAME)
        setup_data.update(
            (key, value)
            for key, value in submitted.items()
            if key != CONF_PASSWORD or value not in (None, "")
        )
        if not submitted.get(CONF_PASSWORD) and prior_username != setup_data.get(CONF_USERNAME):
            setup_data[CONF_PASSWORD] = None
        try:
            # session.finish loads the provider (login) and maps load errors to setup errors.
            await session.finish(setup_data)
            return
        except SetupFlowError as err:
            errors = {"base": err}
