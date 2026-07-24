"""Setup flow for the Bose SoundTouch provider."""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import ConfigEntryType

from music_assistant.models.setup_flow import SetupFlowError
from music_assistant.providers.bose_soundtouch.const import CONF_APP_KEY

if TYPE_CHECKING:
    from music_assistant.models.setup_flow import SetupSession

_ENTRIES = (
    ConfigEntry(
        key=CONF_APP_KEY,
        type=ConfigEntryType.SECURE_STRING,
        translation_key="app_key",
        required=False,
    ),
)


async def run_setup(session: SetupSession) -> None:
    """Run the setup flow: collect the (optional) developer app key and create the provider."""
    errors: dict[str, str] | None = None
    setup_data = dict(session.context.setup_data)
    while True:
        submitted = await session.form(
            list(_ENTRIES), step_id="user", errors=errors, last_step=True
        )
        setup_data.update(submitted)
        try:
            await session.finish(setup_data)
            return
        except SetupFlowError as err:
            errors = {"base": err.translation_key or str(err)}
