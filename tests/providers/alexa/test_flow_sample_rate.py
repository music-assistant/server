"""Tests for the flow mode sample rate an Echo gets by default."""

from __future__ import annotations

from typing import Any, cast
from unittest.mock import MagicMock

from music_assistant_models.config_entries import PlayerConfig

from music_assistant.constants import (
    CONF_ENTRY_FLOW_MODE_SAMPLE_RATE,
    CONF_FLOW_MODE_SAMPLE_RATE,
    FLOW_MODE_SAMPLE_RATE_48000,
    FLOW_MODE_SAMPLE_RATE_SMART,
)
from music_assistant.providers.alexa import AlexaPlayer
from tests.common import MockProvider


def _player() -> AlexaPlayer:
    """Return a player on an Echo."""
    provider = cast("Any", MockProvider("alexa", instance_id="alexa--1"))
    provider.mass.config.get_base_protocol_config.return_value = PlayerConfig(
        values={}, provider="alexa--1", player_id="Echo", default_name="Echo"
    )
    return AlexaPlayer(provider, "Echo", MagicMock(device_serial_number="SERIAL"))


async def test_echo_defaults_to_a_fixed_flow_sample_rate() -> None:
    """An Echo cannot report the end of a flow stream, so it never gets the restarting mode."""
    entries = {entry.key: entry for entry in await _player().get_config_entries()}
    sample_rate = entries[CONF_FLOW_MODE_SAMPLE_RATE]
    assert sample_rate.default_value == FLOW_MODE_SAMPLE_RATE_48000
    # still a choice for the user, with every option of the generic entry
    assert sample_rate.options == CONF_ENTRY_FLOW_MODE_SAMPLE_RATE.options


async def test_generic_entry_is_left_untouched() -> None:
    """Overriding the default for Echos does not change it for other players."""
    await _player().get_config_entries()
    assert CONF_ENTRY_FLOW_MODE_SAMPLE_RATE.default_value == FLOW_MODE_SAMPLE_RATE_SMART
