"""Unit tests for the native player-control config options being shown disabled, not omitted."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import pytest
from music_assistant_models.constants import (
    PLAYER_CONTROL_FAKE,
    PLAYER_CONTROL_NATIVE,
    PLAYER_CONTROL_NONE,
)
from music_assistant_models.enums import PlayerFeature, PlayerType, ProtocolRole
from music_assistant_models.player_control import PlayerControl

from music_assistant.constants import (
    CONF_IDLE_POWER_OFF,
    CONF_MUTE_CONTROL,
    CONF_PLAYERS,
    CONF_POWER_CONTROL,
    CONF_POWER_CONTROL_CUTS_POWER,
    CONF_POWER_ON_STARTUP_TIME,
    CONF_VOLUME_CONTROL,
    PLAYER_CONTROL_PROTOCOL,
)
from music_assistant.mass import MusicAssistant

from .helpers import attach_on_config

if TYPE_CHECKING:
    from unittest.mock import MagicMock

    from music_assistant_models.config_entries import ConfigEntry

# each control select and the player feature that enables its native option
_CONTROL_FEATURES = [
    (CONF_POWER_CONTROL, PlayerFeature.POWER),
    (CONF_VOLUME_CONTROL, PlayerFeature.VOLUME_SET),
    (CONF_MUTE_CONTROL, PlayerFeature.VOLUME_MUTE),
]
_ALL_FEATURES = {feature for _conf_key, feature in _CONTROL_FEATURES}
# the control selects an audio protocol can take as well
_AUDIO_CONTROL_FEATURES = [
    (CONF_VOLUME_CONTROL, PlayerFeature.VOLUME_SET),
    (CONF_MUTE_CONTROL, PlayerFeature.VOLUME_MUTE),
]


def _entries_by_key(
    mass: MusicAssistant,
    features: set[PlayerFeature],
    audio_features: set[PlayerFeature] | None = None,
    player_type: PlayerType = PlayerType.PLAYER,
    power_control: str | None = None,
) -> dict[str, ConfigEntry]:
    """
    Build the player-control config entries for a player.

    :param mass: A server with a real config controller.
    :param features: The features the own protocol of the player supports.
    :param audio_features: The features of an audio protocol of the player, if it has one.
    :param player_type: The type of the player.
    :param power_control: The power control stored for the player, if any.
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
    if power_control is not None:
        mass.config.set(
            f"{CONF_PLAYERS}/{player.player_id}/values", {CONF_POWER_CONTROL: power_control}
        )
        player.refresh_state(signal_event=False)
    entries = mass.config._create_player_control_config_entries(player)
    return {entry.key: entry for entry in entries}


def _add_smart_plug(mass: MusicAssistant) -> str:
    """Register an external power control on the server and return its id."""
    # a first player brings the stand-in for the players controller
    attach_on_config(mass, "test", "other_1", player_id="other")
    plug = PlayerControl(
        id="switch.plug", provider="hass", name="Plug", supports_power=True, power_state=True
    )
    players = cast("MagicMock", mass.players)
    players.player_controls.return_value = [plug]
    players.get_player_control.side_effect = lambda control_id: (
        plug if control_id == plug.id else None
    )
    return plug.id


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


@pytest.mark.parametrize(("conf_key", "feature"), _AUDIO_CONTROL_FEATURES)
async def test_native_is_the_default_next_to_auto(
    mass_minimal: MusicAssistant, conf_key: str, feature: PlayerFeature
) -> None:
    """With an audio protocol that can take the control, native stays the default, auto follows."""
    entry = _entries_by_key(mass_minimal, {feature}, {feature})[conf_key]

    assert entry.default_value == PLAYER_CONTROL_NATIVE
    assert [option.value for option in entry.options][:2] == [
        PLAYER_CONTROL_NATIVE,
        PLAYER_CONTROL_PROTOCOL,
    ]
    # auto is selectable, and differs from the default, so a save stores it
    auto = next(option for option in entry.options if option.value == PLAYER_CONTROL_PROTOCOL)
    assert auto.disabled is False


@pytest.mark.parametrize(("conf_key", "feature"), _AUDIO_CONTROL_FEATURES)
async def test_auto_is_the_default_without_a_native_control(
    mass_minimal: MusicAssistant, conf_key: str, feature: PlayerFeature
) -> None:
    """Without a native control, auto is the default when an audio protocol can take it."""
    entry = _entries_by_key(mass_minimal, set(), {feature})[conf_key]

    assert entry.default_value == PLAYER_CONTROL_PROTOCOL


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


async def test_an_external_power_control_brings_the_cuts_power_option(
    mass_minimal: MusicAssistant,
) -> None:
    """A player on an external power control can be told that the control cuts its power."""
    plug_id = _add_smart_plug(mass_minimal)

    entries = _entries_by_key(mass_minimal, _ALL_FEATURES, power_control=plug_id)

    option = entries[CONF_POWER_CONTROL_CUTS_POWER]
    assert (option.default_value, option.advanced, option.depends_on) == (False, False, None)
    startup_time = entries[CONF_POWER_ON_STARTUP_TIME]
    assert (startup_time.default_value, startup_time.range) == (30, (5, 300))
    assert (startup_time.advanced, startup_time.depends_on) == (True, CONF_POWER_CONTROL_CUTS_POWER)
    # right below the power control they belong to
    assert list(entries)[:3] == [
        CONF_POWER_CONTROL,
        CONF_POWER_CONTROL_CUTS_POWER,
        CONF_POWER_ON_STARTUP_TIME,
    ]


@pytest.mark.parametrize(
    "power_control", [PLAYER_CONTROL_NATIVE, PLAYER_CONTROL_FAKE, PLAYER_CONTROL_NONE, None]
)
async def test_no_cuts_power_option_without_an_external_power_control(
    mass_minimal: MusicAssistant, power_control: str | None
) -> None:
    """A native, fake or absent power control can not cut the power of the player itself."""
    _add_smart_plug(mass_minimal)

    entries = _entries_by_key(mass_minimal, _ALL_FEATURES, power_control=power_control)

    assert CONF_POWER_CONTROL_CUTS_POWER not in entries
    assert CONF_POWER_ON_STARTUP_TIME not in entries


async def test_a_group_player_gets_no_cuts_power_option(mass_minimal: MusicAssistant) -> None:
    """A group player has no device that starts up, also on an external power control."""
    plug_id = _add_smart_plug(mass_minimal)

    entries = _entries_by_key(
        mass_minimal, _ALL_FEATURES, player_type=PlayerType.GROUP, power_control=plug_id
    )

    assert set(entries) == {CONF_POWER_CONTROL}
