"""Setup flow for the Local Audio Source plugin."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption
from music_assistant_models.enums import ConfigEntryType

from music_assistant.constants import CONF_ENTRY_WARN_PREVIEW
from music_assistant.models.setup_flow import SetupFlowError

from .constants import (
    CONF_FRIENDLY_NAME,
    CONF_ICON_PRESET,
    CONF_INCLUDE_MONITORS,
    CONF_INPUT_DEVICE,
    CONF_THUMBNAIL_IMAGE,
    ICON_PRESET_CUSTOM,
    ICON_PRESETS,
)
from .helpers import get_available_input_devices

if TYPE_CHECKING:
    from music_assistant.models.setup_flow import SetupSession


async def run_setup(session: SetupSession) -> None:
    """
    Configure the PulseAudio/PipeWire source this instance captures from.

    :param session: The setup session driving the flow.
    """
    setup_data = dict(session.context.setup_data)
    errors: dict[str, str] | None = None
    prefill: dict[str, Any] = {**session.context.values, **setup_data}

    monitors_values = await session.form(
        [
            CONF_ENTRY_WARN_PREVIEW,
            ConfigEntry(
                key=CONF_INCLUDE_MONITORS,
                type=ConfigEntryType.BOOLEAN,
                default_value=False,
                value=prefill.get(CONF_INCLUDE_MONITORS),
                required=False,
            ),
        ],
        step_id="monitors",
    )
    setup_data.update(monitors_values)
    include_monitors = bool(setup_data.get(CONF_INCLUDE_MONITORS, False))

    while True:
        device_options = await get_available_input_devices(include_monitors=include_monitors)
        values = await session.form(
            [
                ConfigEntry(
                    key=CONF_FRIENDLY_NAME,
                    type=ConfigEntryType.STRING,
                    default_value="Local Audio Source",
                    value=prefill.get(CONF_FRIENDLY_NAME),
                    required=True,
                ),
                ConfigEntry(
                    key=CONF_INPUT_DEVICE,
                    type=ConfigEntryType.STRING,
                    options=device_options,
                    default_value=device_options[0].value if device_options else None,
                    value=prefill.get(CONF_INPUT_DEVICE),
                    required=True,
                ),
                ConfigEntry(
                    key=CONF_ICON_PRESET,
                    type=ConfigEntryType.STRING,
                    options=[
                        ConfigValueOption(ICON_PRESET_CUSTOM),
                        *(ConfigValueOption(key) for key in ICON_PRESETS),
                    ],
                    default_value=ICON_PRESET_CUSTOM,
                    value=prefill.get(CONF_ICON_PRESET),
                    required=True,
                ),
                ConfigEntry(
                    key=CONF_THUMBNAIL_IMAGE,
                    type=ConfigEntryType.STRING,
                    default_value="",
                    value=prefill.get(CONF_THUMBNAIL_IMAGE),
                    required=False,
                    depends_on=CONF_ICON_PRESET,
                    depends_on_value=ICON_PRESET_CUSTOM,
                ),
            ],
            step_id="device",
            errors=errors,
            last_step=True,
        )
        setup_data.update(values)
        try:
            await session.finish(setup_data)
            return
        except SetupFlowError as err:
            errors = {"base": err.translation_key or str(err)}
