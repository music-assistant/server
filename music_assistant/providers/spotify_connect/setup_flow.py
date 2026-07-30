"""Setup flow for the Spotify Connect plugin."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import ConfigEntryType

from music_assistant.constants import CONF_ENTRY_WARN_PREVIEW
from music_assistant.helpers.config_entries import create_player_selector
from music_assistant.models.setup_flow import SetupFlowError

from . import (
    CONF_MASS_PLAYER_ID,
    CONF_PUBLISH_NAME,
    DEFAULT_PUBLISH_NAME,
    PLAYER_ID_AUTO,
)

if TYPE_CHECKING:
    from music_assistant.models.setup_flow import SetupSession


async def run_setup(session: SetupSession) -> None:
    """
    Configure the target player and advertised Spotify Connect device name.

    :param session: The setup session driving the flow.
    """
    setup_data = dict(session.context.setup_data)
    errors: dict[str, str] | None = None
    while True:
        prefill: dict[str, Any] = {**session.context.values, **setup_data}
        publish_name = str(prefill.get(CONF_PUBLISH_NAME) or DEFAULT_PUBLISH_NAME)
        values = await session.form(
            [
                CONF_ENTRY_WARN_PREVIEW,
                create_player_selector(
                    session.mass,
                    CONF_MASS_PLAYER_ID,
                    prefill.get(CONF_MASS_PLAYER_ID),
                    PLAYER_ID_AUTO,
                ),
                ConfigEntry(
                    key=CONF_PUBLISH_NAME,
                    type=ConfigEntryType.STRING,
                    required=True,
                    default_value=publish_name,
                    value=publish_name,
                ),
            ],
            step_id="user",
            errors=errors,
            last_step=True,
        )
        setup_data.update(values)
        try:
            await session.finish(setup_data)
            return
        except SetupFlowError as err:
            errors = {"base": err.translation_key or str(err)}
