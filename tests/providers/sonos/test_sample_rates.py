"""Tests for the sample rates a Sonos player offers and resolves to."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from music_assistant_models.config_entries import MULTI_VALUE_SPLITTER

from music_assistant.constants import CONF_SAMPLE_RATES
from music_assistant.models.player import DeviceInfo
from music_assistant.providers.sonos.player import SonosPlayer

HI_RES_MODEL = "Era 300"
# a model from NON_HIRES_MODELS, which Sonos plays back at 16 bit only
NON_HI_RES_MODEL = "Play:1"


def _make_player(model: str, config_value: Any = None) -> SonosPlayer:
    """Create a SonosPlayer for the given model, with an optional stored rate selection."""
    player = SonosPlayer.__new__(SonosPlayer)
    player._attr_device_info = DeviceInfo(model=model, manufacturer="Sonos")
    player._attr_supported_sample_rates = None
    player._cache = {}
    config = MagicMock()
    config.get_value = lambda key, default=None: (
        config_value if key == CONF_SAMPLE_RATES else default
    )
    player._config = config
    return player


async def _sample_rates_entry(player: SonosPlayer) -> Any:
    """Return the sample rates config entry the player offers."""
    entries = await player.get_config_entries()
    return next(entry for entry in entries if entry.key == CONF_SAMPLE_RATES)


async def test_player_offers_the_sample_rates_setting() -> None:
    """The setting has to come from the player, otherwise it never reaches the user."""
    entry = await _sample_rates_entry(_make_player(HI_RES_MODEL))

    assert entry.options
    assert [option.value for option in entry.options] == [
        f"44100{MULTI_VALUE_SPLITTER}16",
        f"44100{MULTI_VALUE_SPLITTER}24",
        f"48000{MULTI_VALUE_SPLITTER}16",
        f"48000{MULTI_VALUE_SPLITTER}24",
    ]


async def test_non_hi_res_model_is_not_offered_24_bit() -> None:
    """Sonos plays the older models back at 16 bit, so 24 bit is not a real choice there."""
    entry = await _sample_rates_entry(_make_player(NON_HI_RES_MODEL))

    assert [option.value for option in entry.options or []] == [
        f"44100{MULTI_VALUE_SPLITTER}16",
        f"48000{MULTI_VALUE_SPLITTER}16",
    ]


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        (HI_RES_MODEL, [(44100, 16), (44100, 24), (48000, 16), (48000, 24)]),
        (NON_HI_RES_MODEL, [(44100, 16), (48000, 16)]),
    ],
)
async def test_default_selection_covers_the_hardware_range(
    model: str, expected: list[tuple[int, int]]
) -> None:
    """Out of the box the player keeps every rate its hardware accepts."""
    entry = await _sample_rates_entry(_make_player(model))
    # a player whose setting is left alone reports the entry's default back
    player = _make_player(model, config_value=entry.default_value)

    assert player.get_supported_sample_rates() == expected


async def test_selection_narrows_the_output_format() -> None:
    """Selecting a single pair is what pins the output to one format."""
    player = _make_player(HI_RES_MODEL, config_value=[f"48000{MULTI_VALUE_SPLITTER}16"])

    assert player.get_supported_sample_rates() == [(48000, 16)]


async def test_player_no_longer_declares_its_rates() -> None:
    """Declaring them would make the config controller skip the setting entirely."""
    player = _make_player(HI_RES_MODEL)

    assert player.supported_sample_rates is None
    assert player.declares_supported_sample_rates is False
