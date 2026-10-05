"""Setup flow for the AI Radio plugin."""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant.helpers.plugin_engines import (
    create_ai_engine_config_entries,
    create_tts_engine_config_entries,
    get_ai_engines,
    get_tts_engines,
)
from music_assistant.models.setup_flow import AbortFlow

from .config import get_weather_location_entries
from .constants import CONF_AI_ENGINE, CONF_TTS_ENGINE

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigEntry

    from music_assistant.models.setup_flow import SetupSession


async def run_setup(session: SetupSession) -> None:
    """
    Run the AI Radio setup flow.

    Collects the mandatory AI and text-to-speech engines the stations run on, plus the
    optional weather location used by stations that reference weather placeholders.
    The flow aborts when no plugin currently provides an AI or TTS engine.

    :param session: The setup session driving the flow.
    """
    if not await get_ai_engines(session.mass):
        raise AbortFlow("no_ai_engine")
    if not await get_tts_engines(session.mass):
        raise AbortFlow("no_tts_engine")
    weather_city, weather_country = await get_weather_location_entries(
        session.mass, session.context.instance_id
    )
    entries: list[ConfigEntry] = [
        *await create_ai_engine_config_entries(session.mass, CONF_AI_ENGINE, required=True),
        *await create_tts_engine_config_entries(session.mass, CONF_TTS_ENGINE, required=True),
        weather_city,
        weather_country,
    ]
    for entry in entries:
        if (prefill := session.context.setup_data.get(entry.key)) is not None:
            entry.value = prefill
    values = await session.form(entries, step_id="user", last_step=True)
    await session.finish(values)
