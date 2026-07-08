"""Unit tests for the native player-control config options being shown disabled, not omitted."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest
from music_assistant_models.constants import PLAYER_CONTROL_NATIVE, PLAYER_CONTROL_NONE
from music_assistant_models.enums import PlayerFeature, PlayerType

from music_assistant.constants import (
    CONF_MUTE_CONTROL,
    CONF_POWER_CONTROL,
    CONF_VOLUME_CONTROL,
)
from music_assistant.mass import MusicAssistant

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigEntry

# each control select and the player feature that enables its native option
_CONTROL_FEATURES = [
    (CONF_POWER_CONTROL, PlayerFeature.POWER),
    (CONF_VOLUME_CONTROL, PlayerFeature.VOLUME_SET),
    (CONF_MUTE_CONTROL, PlayerFeature.VOLUME_MUTE),
]
_ALL_FEATURES = {feature for _conf_key, feature in _CONTROL_FEATURES}


def _make_player(features: set[PlayerFeature]) -> MagicMock:
    """Return a mock non-group player that supports exactly the given features."""
    player = MagicMock()
    player.state.type = PlayerType.PLAYER
    player.linked_output_protocols = []
    player.supports_feature.side_effect = lambda feature: feature in features
    return player


def _entries_by_key(mass: MusicAssistant, features: set[PlayerFeature]) -> dict[str, ConfigEntry]:
    """Build the player-control config entries for a player with the given features."""
    mass.players = MagicMock()
    mass.players.player_controls.return_value = []
    entries = mass.config._create_player_control_config_entries(_make_player(features))
    return {entry.key: entry for entry in entries}


@pytest.mark.parametrize(("conf_key", "feature"), _CONTROL_FEATURES)
async def test_native_option_disabled_when_feature_unsupported(
    mass_minimal: MusicAssistant, conf_key: str, feature: PlayerFeature
) -> None:
    """The native option is always offered, disabled when its feature is unsupported."""
    # support every control except the one under test, to prove the gating is per-feature
    entry = _entries_by_key(mass_minimal, _ALL_FEATURES - {feature})[conf_key]
    native = next(option for option in entry.options if option.value == PLAYER_CONTROL_NATIVE)
    assert native.disabled is True


@pytest.mark.parametrize(("conf_key", "feature"), _CONTROL_FEATURES)
async def test_native_option_enabled_and_default_when_supported(
    mass_minimal: MusicAssistant, conf_key: str, feature: PlayerFeature
) -> None:
    """When the feature is supported the native option is enabled and picked as the default."""
    entry = _entries_by_key(mass_minimal, {feature})[conf_key]
    native = next(option for option in entry.options if option.value == PLAYER_CONTROL_NATIVE)
    assert native.disabled is False
    assert entry.default_value == PLAYER_CONTROL_NATIVE


@pytest.mark.parametrize(("conf_key", "feature"), _CONTROL_FEATURES)
async def test_default_is_never_a_disabled_option(
    mass_minimal: MusicAssistant, conf_key: str, feature: PlayerFeature
) -> None:
    """The default must always resolve to a selectable option, even for required controls."""
    entry = _entries_by_key(mass_minimal, _ALL_FEATURES - {feature})[conf_key]
    default_option = next(option for option in entry.options if option.value == entry.default_value)
    assert default_option.disabled is False
    # with no native support the safe fallback is the always-present "none" control
    assert entry.default_value == PLAYER_CONTROL_NONE
