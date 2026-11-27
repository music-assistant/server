"""Tests for the streams settings that join the queue settings."""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant.constants import (
    CONF_VOLUME_NORMALIZATION_FIXED_GAIN_RADIO,
    CONF_VOLUME_NORMALIZATION_FIXED_GAIN_TRACKS,
    CONF_VOLUME_NORMALIZATION_RADIO,
    CONF_VOLUME_NORMALIZATION_TARGET,
    CONF_VOLUME_NORMALIZATION_TRACKS,
)
from music_assistant.controllers.player_queues.config import (
    CATEGORY_CROSSFADE,
    CATEGORY_VOLUME_NORMALIZATION,
)
from music_assistant.controllers.streams.constants import CONF_ALLOW_CROSSFADE_SAME_ALBUM

if TYPE_CHECKING:
    from music_assistant.controllers.streams.controller import StreamsController


async def test_crossfade_and_normalization_share_the_queue_categories(
    streams_controller: StreamsController,
) -> None:
    """The crossfade and volume normalization settings sit in the categories of the queue."""
    categories = {
        entry.key: entry.category for entry in await streams_controller.get_config_entries()
    }

    assert categories[CONF_ALLOW_CROSSFADE_SAME_ALBUM] == CATEGORY_CROSSFADE
    for key in (
        CONF_VOLUME_NORMALIZATION_RADIO,
        CONF_VOLUME_NORMALIZATION_TRACKS,
        CONF_VOLUME_NORMALIZATION_FIXED_GAIN_RADIO,
        CONF_VOLUME_NORMALIZATION_FIXED_GAIN_TRACKS,
        CONF_VOLUME_NORMALIZATION_TARGET,
    ):
        assert categories[key] == CATEGORY_VOLUME_NORMALIZATION, key


async def test_crossfade_and_normalization_tuning_is_advanced(
    streams_controller: StreamsController,
) -> None:
    """The crossfade and volume normalization fine-tuning only shows with the advanced settings."""
    entries = {entry.key: entry for entry in await streams_controller.get_config_entries()}

    for key in (
        CONF_ALLOW_CROSSFADE_SAME_ALBUM,
        CONF_VOLUME_NORMALIZATION_RADIO,
        CONF_VOLUME_NORMALIZATION_TRACKS,
        CONF_VOLUME_NORMALIZATION_FIXED_GAIN_RADIO,
        CONF_VOLUME_NORMALIZATION_FIXED_GAIN_TRACKS,
        CONF_VOLUME_NORMALIZATION_TARGET,
    ):
        assert entries[key].advanced, key
