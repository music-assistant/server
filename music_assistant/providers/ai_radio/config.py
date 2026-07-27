"""Configuration entries for the AI Radio plugin."""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant_models.config_entries import ConfigEntry, ConfigValueType
from music_assistant_models.enums import ConfigEntryType

from .constants import CONF_UI_AUTO_REFRESH_SECONDS

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant


async def get_config_entries(
    mass: MusicAssistant,
    instance_id: str | None = None,  # noqa: ARG001
    action: str | None = None,  # noqa: ARG001
    values: dict[str, ConfigValueType] | None = None,  # noqa: ARG001
) -> tuple[ConfigEntry, ...]:
    """Return Config entries to setup this provider."""
    base_url = mass.webserver.base_url.rstrip("/")
    web_ui_url = f"{base_url}/#/ai-radio"
    return (
        ConfigEntry(
            key="web_ui_url",
            type=ConfigEntryType.LABEL,
            translation_params=[web_ui_url],
        ),
        ConfigEntry(
            key=CONF_UI_AUTO_REFRESH_SECONDS,
            type=ConfigEntryType.INTEGER,
            default_value=2,
            range=(1, 30),
            category="advanced",
        ),
    )
