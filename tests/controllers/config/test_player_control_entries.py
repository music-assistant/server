"""Unit tests for the native player-control config options being shown disabled, not omitted."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from music_assistant_models.constants import (
    PLAYER_CONTROL_FAKE,
    PLAYER_CONTROL_NATIVE,
    PLAYER_CONTROL_NONE,
)
from music_assistant_models.enums import PlayerFeature, PlayerType, ProtocolRole

from music_assistant.constants import (
    CONF_IDLE_POWER_OFF,
    CONF_MUTE_CONTROL,
    CONF_POWER_CONTROL,
    CONF_VOLUME_CONTROL,
)
from music_assistant.mass import MusicAssistant

from .helpers import attach_on_config

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigEntry

# each control select and the player feature that enables its native option
_CONTROL_FEATURES = [
    (CONF_POWER_CONTROL, PlayerFeature.POWER),
    (CONF_VOLUME_CONTROL, PlayerFeature.VOLUME_SET),
    (CONF_MUTE_CONTROL, PlayerFeature.VOLUME_MUTE),
]
_ALL_FEATURES = {feature for _conf_key, feature in _CONTROL_FEATURES}


def _entries_by_key(
    mass: MusicAssistant,
    features: set[PlayerFeature],
    audio_features: set[PlayerFeature] | None = None,
    player_type: PlayerType = PlayerType.PLAYER,
) -> dict[str, ConfigEntry]:
    """
    Build the player-control config entries for a player.

    :param mass: A server with a real config controller.
    :param features: The features the own protocol of the player supports.
    :param audio_features: The features of an audio protocol of the player, if it has one.
    :param player_type: The type of the player.
    """
    is_group = player_type == PlayerType.GROUP
    player, _ = attach_on_config(
        mass,
        "test",
        "own_1",
        role=ProtocolRole.VIRTUAL if is_group else ProtocolRole.NATIVE,
        features=set(features),
        player_type=player_type,
    )
    if audio_features is not None:
        attach_on_config(
            mass,
            "squeezelite",
            "audio_1",
            role=ProtocolRole.AUDIO,
            player=player,
            features=audio_features,
        )
    entries = mass.config._create_player_control_config_entries(player)
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


async def test_fake_mute_offered_when_volume_provided_by_protocol_player(
    mass_minimal: MusicAssistant,
) -> None:
    """
    Fake mute is offered when the volume path comes from an audio protocol.

    Regression test: players whose volume is provided by an audio protocol
    (e.g. a device that is only known through squeezelite) don't report VOLUME_SET
    natively, yet fake mute works fine for them as it drives the configured
    volume control.
    """
    entry = _entries_by_key(mass_minimal, set(), {PlayerFeature.VOLUME_SET})[CONF_MUTE_CONTROL]
    fake = next(option for option in entry.options if option.value == PLAYER_CONTROL_FAKE)
    assert fake.disabled is False


async def test_fake_mute_not_offered_without_any_volume_path(
    mass_minimal: MusicAssistant,
) -> None:
    """Fake mute stays hidden when neither the player nor an audio protocol provides volume."""
    entry = _entries_by_key(mass_minimal, {PlayerFeature.VOLUME_MUTE})[CONF_MUTE_CONTROL]
    assert all(option.value != PLAYER_CONTROL_FAKE for option in entry.options)


async def test_group_player_only_gets_a_power_control(mass_minimal: MusicAssistant) -> None:
    """A group's volume and mute go to its members, so it is only offered the power control."""
    entries = _entries_by_key(mass_minimal, _ALL_FEATURES, player_type=PlayerType.GROUP)
    assert set(entries) == {CONF_POWER_CONTROL}


async def test_idle_power_off_follows_the_power_control(mass_minimal: MusicAssistant) -> None:
    """A player gets the idle power off time, which needs a power control to act through."""
    entry = _entries_by_key(mass_minimal, _ALL_FEATURES)[CONF_IDLE_POWER_OFF]
    assert entry.default_value == 0
    assert entry.depends_on == CONF_POWER_CONTROL
    assert entry.depends_on_value_not == PLAYER_CONTROL_NONE
