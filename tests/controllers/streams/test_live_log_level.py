"""Tests for applying the streams log levels when the streams settings change."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from music_assistant.constants import CONF_LOG_LEVEL
from music_assistant.controllers.streams.constants import CONF_SMART_FADES_LOG_LEVEL
from music_assistant.helpers.ffmpeg import LOGGER as FFMPEG_LOGGER

if TYPE_CHECKING:
    from music_assistant.controllers.streams.controller import StreamsController


async def test_smart_fades_log_level_change_applies_live(
    streams_controller: StreamsController,
) -> None:
    """A changed smart fades log level reaches the mixer logger without a reload."""
    streams_controller.audio.setup()
    mixer_logger = streams_controller.audio.smart_fades_mixer.logger
    assert mixer_logger.level != logging.ERROR
    config = await streams_controller.mass.config.get_core_config(streams_controller.domain)
    changed_keys = config.update({CONF_SMART_FADES_LOG_LEVEL: "ERROR"})
    assert changed_keys == {f"values/{CONF_SMART_FADES_LOG_LEVEL}"}

    await streams_controller.update_config(config, changed_keys)

    assert mixer_logger.level == logging.ERROR


async def test_main_log_level_change_reaches_all_derived_loggers(
    streams_controller: StreamsController,
) -> None:
    """A live streams log level change reaches the audio, ffmpeg and (GLOBAL) mixer loggers."""
    streams_controller.audio.setup()
    derived_loggers = (
        streams_controller.audio.logger,
        FFMPEG_LOGGER,
        streams_controller.audio.smart_fades_mixer.logger,
    )
    assert all(logger.level != logging.ERROR for logger in derived_loggers)
    config = await streams_controller.mass.config.get_core_config(streams_controller.domain)
    assert config.get_value(CONF_SMART_FADES_LOG_LEVEL) == "GLOBAL"
    changed_keys = config.update({CONF_LOG_LEVEL: "ERROR"})
    assert changed_keys == {f"values/{CONF_LOG_LEVEL}"}

    await streams_controller.update_config(config, changed_keys)

    assert streams_controller.logger.level == logging.ERROR
    assert all(logger.level == logging.ERROR for logger in derived_loggers)
