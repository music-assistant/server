"""
Tests for keeping the values a user chose explicitly when a config is saved.

A value that is stored stays stored, also when it equals the default of its entry, so a
choice survives a default that changes at runtime. A value that is not stored yet is only
stored when it differs from its default, and a value submitted as None is unset: it leaves
storage and follows its default again.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

from music_assistant_models.config_entries import (
    ConfigEntry,
    ConfigValueType,
    PlayerQueueConfig,
    ProviderConfig,
)
from music_assistant_models.enums import (
    ConfigEntryType,
    PlayerFeature,
    PlayerType,
    ProtocolRole,
    ProviderType,
)

from music_assistant.constants import (
    CONF_ENTRY_LOG_LEVEL,
    CONF_HIDE_IN_UI,
    CONF_OUTPUT_CHANNELS,
    CONF_PLAYER_PROTOCOLS,
    CONF_PLAYER_QUEUES,
    CONF_PROVIDERS,
)
from music_assistant.controllers.config.helpers import _raw_config_to_store
from music_assistant.models.provider import Provider
from tests.common import MockProtocol

from .helpers import attach_on_config

if TYPE_CHECKING:
    from contextlib import AbstractContextManager

    from music_assistant.mass import MusicAssistant
    from music_assistant.models.player import Player

PLAYER_ID = "pl0123456789ab"
PROTOCOL_ID = "protocol_1"
PROVIDER = "test_instance"
QUEUE_ID = "queue_1"
SETTING = "some_setting"
OTHER_SETTING = "other_setting"
RUNTIME_STATE = "runtime_state"


def _entries() -> list[ConfigEntry]:
    """Return a setting that defaults to off and another one."""
    return [
        ConfigEntry(key=SETTING, type=ConfigEntryType.STRING, default_value="off", required=False),
        ConfigEntry(
            key=OTHER_SETTING, type=ConfigEntryType.INTEGER, default_value=0, required=False
        ),
    ]


def _queue_config(values: dict[str, ConfigValueType]) -> PlayerQueueConfig:
    """Return a queue config with the stored values."""
    return cast(
        "PlayerQueueConfig",
        PlayerQueueConfig.parse(_entries(), {"queue_id": QUEUE_ID, "values": values}),
    )


def test_a_new_value_at_its_default_is_not_stored() -> None:
    """A value that was not stored is only stored when it differs from its default."""
    config = _queue_config({})
    submitted: dict[str, ConfigValueType] = {SETTING: "off", OTHER_SETTING: 5}
    config.update(submitted)

    assert _raw_config_to_store(config, {}, submitted)["values"] == {OTHER_SETTING: 5}


def test_a_stored_value_at_its_default_stays_stored() -> None:
    """A stored value stays, also at its default and when it is not submitted."""
    stored: dict[str, ConfigValueType] = {SETTING: "off", OTHER_SETTING: 0, RUNTIME_STATE: [1]}
    config = _queue_config(stored)
    submitted: dict[str, ConfigValueType] = {OTHER_SETTING: 0}
    config.update(submitted)

    assert _raw_config_to_store(config, stored, submitted)["values"] == stored


def test_a_value_submitted_as_none_is_unset() -> None:
    """A value submitted as None leaves storage; a value without an entry is kept."""
    stored: dict[str, ConfigValueType] = {SETTING: "on", RUNTIME_STATE: [1]}
    config = _queue_config(stored)
    submitted: dict[str, ConfigValueType] = {SETTING: None}
    config.update(submitted)

    assert _raw_config_to_store(config, stored, submitted)["values"] == {RUNTIME_STATE: [1]}


async def test_provider_save_keeps_a_choice_at_its_default(mass_minimal: MusicAssistant) -> None:
    """A provider keeps a stored value at its default through a save of another value."""
    provider = _store_provider(mass_minimal, {SETTING: "off"})

    with _provider_config_from_storage(mass_minimal):
        await mass_minimal.config.save_provider_config(
            "test", {SETTING: "off", OTHER_SETTING: 5}, instance_id=PROVIDER
        )
        assert _stored_provider_values(mass_minimal) == {SETTING: "off", OTHER_SETTING: 5}
        cast("AsyncMock", provider.update_config).assert_awaited_once()

        await mass_minimal.config.save_provider_config(
            "test", {SETTING: None}, instance_id=PROVIDER
        )

    # unsetting the value at its default changes storage only
    assert _stored_provider_values(mass_minimal) == {OTHER_SETTING: 5}
    cast("AsyncMock", provider.update_config).assert_awaited_once()


async def test_player_save_keeps_a_choice_at_its_default(mass_minimal: MusicAssistant) -> None:
    """A setting of the own protocol of a player keeps a choice through a default change."""
    _, protocol = _attach(mass_minimal)
    # chosen while the default was on
    await mass_minimal.config.save_player_config(PLAYER_ID, {SETTING: "off"})
    protocol.setting_default = "off"

    # a save of the whole form sends the choice, now at its default, with another change
    await mass_minimal.config.save_player_config(PLAYER_ID, {SETTING: "off", CONF_HIDE_IN_UI: True})
    protocol.setting_default = "on"

    assert mass_minimal.config.get_protocol_value(PROTOCOL_ID, SETTING) == "off"
    config = await mass_minimal.config.get_player_config(PLAYER_ID)
    assert config.get_value(SETTING) == "off"

    await mass_minimal.config.save_player_config(PLAYER_ID, {SETTING: None})

    assert mass_minimal.config.get_protocol_value(PROTOCOL_ID, SETTING) is None
    config = await mass_minimal.config.get_player_config(PLAYER_ID)
    assert config.get_value(SETTING) == "on"


async def test_player_save_unsetting_a_choice_at_its_default_is_not_passed_on(
    mass_minimal: MusicAssistant,
) -> None:
    """Unsetting a value of a player that equals its default changes storage only."""
    _attach(mass_minimal)
    mass_minimal.config.set_protocol_value(PROTOCOL_ID, SETTING, "on")

    await mass_minimal.config.save_player_config(PLAYER_ID, {SETTING: None})

    assert mass_minimal.config.get_protocol_value(PROTOCOL_ID, SETTING) is None
    cast("AsyncMock", mass_minimal.players.on_player_config_change).assert_not_awaited()


async def test_protocol_save_keeps_a_choice_at_its_default(mass_minimal: MusicAssistant) -> None:
    """A protocol keeps a choice through a default change, and a reset follows the default."""
    _, protocol = _attach(mass_minimal)
    on_change = cast("AsyncMock", mass_minimal.players.on_protocol_config_change)
    # chosen while the default was on
    await mass_minimal.config.save_protocol_config(PROTOCOL_ID, {SETTING: "off"})
    protocol.setting_default = "off"

    await mass_minimal.config.save_protocol_config(
        PROTOCOL_ID, {SETTING: "off", CONF_OUTPUT_CHANNELS: "left"}
    )

    assert mass_minimal.config.get_protocol_value(PROTOCOL_ID, SETTING) == "off"
    assert on_change.await_count == 2

    await mass_minimal.config.save_protocol_config(PROTOCOL_ID, {SETTING: None})

    # unsetting the value at its default changes storage only
    assert mass_minimal.config.get_protocol_value(PROTOCOL_ID, SETTING) is None
    assert mass_minimal.config.get_protocol_value(PROTOCOL_ID, CONF_OUTPUT_CHANNELS) == "left"
    assert on_change.await_count == 2


async def test_protocol_save_unsets_a_value_of_a_protocol_that_is_not_attached(
    mass_minimal: MusicAssistant,
) -> None:
    """A value submitted as None leaves the config of a protocol that is not attached."""
    _attach(mass_minimal)
    mass_minimal.config.create_protocol_record(
        "protocol_2",
        provider=PROVIDER,
        player_id=PLAYER_ID,
        role=ProtocolRole.AUDIO,
        default_name=None,
    )
    mass_minimal.config.set_protocol_value("protocol_2", SETTING, "on")

    await mass_minimal.config.save_protocol_config("protocol_2", {SETTING: None})

    assert SETTING not in mass_minimal.config.get(f"{CONF_PLAYER_PROTOCOLS}/protocol_2/values")


async def test_queue_save_keeps_a_choice_at_its_default(mass_minimal: MusicAssistant) -> None:
    """A queue keeps a stored value at its default through a save of another value."""
    mass_minimal.player_queues = MagicMock()
    mass_minimal.player_queues.get.return_value = None
    mass_minimal.player_queues.get_queue_config_entries.side_effect = _entries
    mass_minimal.config.set(
        f"{CONF_PLAYER_QUEUES}/{QUEUE_ID}", {"queue_id": QUEUE_ID, "values": {SETTING: "off"}}
    )

    await mass_minimal.config.save_player_queue_config(QUEUE_ID, {SETTING: "off", OTHER_SETTING: 5})

    assert mass_minimal.config.get_raw_player_queue_config_value(QUEUE_ID, SETTING) == "off"

    await mass_minimal.config.save_player_queue_config(QUEUE_ID, {SETTING: None})

    assert mass_minimal.config.get_raw_player_queue_config_value(QUEUE_ID, SETTING) is None
    assert mass_minimal.config.get_raw_player_queue_config_value(QUEUE_ID, OTHER_SETTING) == 5


class _RuntimeDefaultProtocol(MockProtocol):
    """A protocol with audio output whose setting has a default that changes at runtime."""

    setting_default = "on"

    async def get_config_entries(self) -> list[ConfigEntry]:
        """Return the setting, with its current default."""
        return [
            ConfigEntry(
                key=SETTING,
                type=ConfigEntryType.STRING,
                default_value=self.setting_default,
                required=False,
            )
        ]


def _attach(mass: MusicAssistant) -> tuple[Player, _RuntimeDefaultProtocol]:
    """Attach a protocol with a stored config to a player with a stored config."""
    mass.config.create_player_record(
        PLAYER_ID, provider=PROVIDER, player_type=PlayerType.PLAYER, default_name="Player"
    )
    mass.config.create_protocol_record(
        PROTOCOL_ID,
        provider=PROVIDER,
        player_id=PLAYER_ID,
        role=ProtocolRole.NATIVE,
        default_name="Player",
    )
    player, protocol = attach_on_config(
        mass,
        PROVIDER,
        PROTOCOL_ID,
        "Player",
        player_id=PLAYER_ID,
        features={PlayerFeature.VOLUME_SET, PlayerFeature.PLAY_MEDIA},
        protocol_class=_RuntimeDefaultProtocol,
    )
    return player, cast("_RuntimeDefaultProtocol", protocol)


def _store_provider(mass: MusicAssistant, values: dict[str, ConfigValueType]) -> Provider:
    """Store the config of a loaded, available provider and return the provider."""
    raw_conf = {
        "type": ProviderType.MUSIC.value,
        "domain": "test",
        "instance_id": PROVIDER,
        "enabled": True,
        "values": values,
    }
    mass.config.set(f"{CONF_PROVIDERS}/{PROVIDER}", raw_conf)
    provider = Provider(
        mass,
        MagicMock(domain="test", type=ProviderType.MUSIC),
        cast("ProviderConfig", ProviderConfig.parse(_provider_entries(), raw_conf)),
    )
    provider.available = True
    provider.update_config = AsyncMock()  # type: ignore[method-assign]
    mass.get_provider = MagicMock(return_value=provider)  # type: ignore[method-assign]
    return provider


def _provider_entries() -> list[ConfigEntry]:
    """Return the entries of the test provider."""
    return [CONF_ENTRY_LOG_LEVEL, *_entries()]


def _provider_config_from_storage(mass: MusicAssistant) -> AbstractContextManager[Any]:
    """Serve the config of the test provider parsed from storage, as a context manager."""

    async def _get_provider_config(_instance_id: str) -> ProviderConfig:
        raw_conf = mass.config.get(f"{CONF_PROVIDERS}/{PROVIDER}")
        return cast("ProviderConfig", ProviderConfig.parse(_provider_entries(), raw_conf))

    return patch.object(mass.config, "get_provider_config", side_effect=_get_provider_config)


def _stored_provider_values(mass: MusicAssistant) -> dict[str, ConfigValueType]:
    """Return the stored values of the test provider."""
    return cast(
        "dict[str, ConfigValueType]", mass.config.get(f"{CONF_PROVIDERS}/{PROVIDER}/values")
    )
