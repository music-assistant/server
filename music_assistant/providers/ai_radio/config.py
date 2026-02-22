"""Configuration entries for the AI Radio plugin."""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant_models.config_entries import ConfigEntry, ConfigValueType
from music_assistant_models.enums import ConfigEntryType

from .constants import (
    AI_RADIO_WEB_BASE_PATH,
    CONF_ELEVENLABS_API_KEY,
    CONF_OPENAI_API_KEY,
    CONF_UI_AUTO_REFRESH_SECONDS,
)


async def get_config_entries(
    mass: MusicAssistant,
    instance_id: str | None = None,  # noqa: ARG001
    action: str | None = None,  # noqa: ARG001
    values: dict[str, ConfigValueType] | None = None,  # noqa: ARG001
) -> tuple[ConfigEntry, ...]:
    """Return Config entries to setup this provider."""
    base_url = mass.webserver.base_url.rstrip("/")
    web_ui_url = f"{base_url}{AI_RADIO_WEB_BASE_PATH}/"
    return (
        ConfigEntry(
            key="web_ui_url",
            type=ConfigEntryType.LABEL,
            label="Click (?) to open AI Radio User Interface",
            description=web_ui_url,
        ),
        ConfigEntry(
            key=CONF_OPENAI_API_KEY,
            type=ConfigEntryType.SECURE_STRING,
            label="OpenAI API Key",
            description="Required for AI text generation and OpenAI TTS.",
            required=False,
        ),
        ConfigEntry(
            key=CONF_ELEVENLABS_API_KEY,
            type=ConfigEntryType.SECURE_STRING,
            label="ElevenLabs API Key",
            required=False,
        ),
        ConfigEntry(
            key=CONF_UI_AUTO_REFRESH_SECONDS,
            type=ConfigEntryType.INTEGER,
            default_value=2,
            range=(1, 30),
            label="Web UI Auto Refresh Interval (seconds)",
            description=(
                "How often the AI Radio web UI refreshes session/player status automatically."
            ),
            category="advanced",
        ),
    )


if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant
