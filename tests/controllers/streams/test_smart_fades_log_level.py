"""Tests for applying the smart fades log level when the streams settings change."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import pytest

from music_assistant.constants import CONF_LOG_LEVEL
from music_assistant.controllers.streams.constants import CONF_SMART_FADES_LOG_LEVEL

if TYPE_CHECKING:
    from collections.abc import Iterator

    from music_assistant.controllers.streams.controller import StreamsController


@pytest.fixture
def streams(streams_controller: StreamsController) -> Iterator[StreamsController]:
    """
    Yield a streams controller with its smart fades mixer created.

    :param streams_controller: StreamsController attached to a minimal server.
    """
    streams_controller.audio.setup()
    # a main log level change sets the controller's own process-global logger, which the
    # shared fixture does not restore
    saved_level = streams_controller.logger.level
    try:
        yield streams_controller
    finally:
        streams_controller.logger.setLevel(saved_level)


async def test_smart_fades_log_level_change_applies_live(streams: StreamsController) -> None:
    """A changed smart fades log level reaches the mixer logger without a reload."""
    mixer_logger = streams.audio.smart_fades_mixer.logger
    assert mixer_logger.level != logging.ERROR
    config = await streams.mass.config.get_core_config(streams.domain)
    changed_keys = config.update({CONF_SMART_FADES_LOG_LEVEL: "ERROR"})
    assert changed_keys == {f"values/{CONF_SMART_FADES_LOG_LEVEL}"}

    await streams.update_config(config, changed_keys)

    assert mixer_logger.level == logging.ERROR


async def test_global_smart_fades_log_level_follows_a_main_level_change(
    streams: StreamsController,
) -> None:
    """With GLOBAL, the mixer logger follows a live change of the streams log level."""
    mixer_logger = streams.audio.smart_fades_mixer.logger
    assert mixer_logger.level != logging.ERROR
    config = await streams.mass.config.get_core_config(streams.domain)
    assert config.get_value(CONF_SMART_FADES_LOG_LEVEL) == "GLOBAL"
    changed_keys = config.update({CONF_LOG_LEVEL: "ERROR"})
    assert changed_keys == {f"values/{CONF_LOG_LEVEL}"}

    await streams.update_config(config, changed_keys)

    assert streams.logger.level == logging.ERROR
    assert mixer_logger.level == logging.ERROR
