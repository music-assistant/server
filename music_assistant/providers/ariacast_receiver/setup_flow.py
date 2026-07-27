"""Setup flow for the AriaCast Receiver plugin."""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant.constants import CONF_ENTRY_WARN_PREVIEW
from music_assistant.helpers.config_entries import create_player_selector
from music_assistant.models.setup_flow import SetupFlowError

from . import CONF_MASS_PLAYER_ID, PLAYER_ID_AUTO

if TYPE_CHECKING:
    from music_assistant.models.setup_flow import SetupSession


async def run_setup(session: SetupSession) -> None:
    """
    Configure the player that receives AriaCast audio.

    :param session: The setup session driving the flow.
    """
    setup_data = dict(session.context.setup_data)
    errors: dict[str, str] | None = None
    while True:
        prefill = {**session.context.values, **setup_data}
        values = await session.form(
            [
                CONF_ENTRY_WARN_PREVIEW,
                create_player_selector(
                    session.mass,
                    CONF_MASS_PLAYER_ID,
                    prefill.get(CONF_MASS_PLAYER_ID),
                    PLAYER_ID_AUTO,
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
