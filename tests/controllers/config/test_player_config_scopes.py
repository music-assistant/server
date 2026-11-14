"""Tests for the scoped access to the stored config of a player and of a protocol."""

from __future__ import annotations

from copy import deepcopy
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock

import pytest
from music_assistant_models.config_entries import ConfigEntry, ConfigValueType
from music_assistant_models.constants import PLAYER_CONTROL_FAKE
from music_assistant_models.enums import (
    ConfigEntryType,
    IdentifierType,
    PlayerFeature,
    PlayerType,
    ProtocolRole,
)
from music_assistant_models.errors import InvalidDataError

from music_assistant.constants import (
    CONF_CACHED_ARP_MAC,
    CONF_DEVICE_INFO,
    CONF_ENABLED,
    CONF_HIDE_IN_UI,
    CONF_LAST_SEEN,
    CONF_MAX_VOLUME,
    CONF_MIN_VOLUME,
    CONF_OUTPUT_CHANNELS,
    CONF_PLAYER_PROTOCOLS,
    CONF_PLAYERS,
    CONF_POWER_CONTROL,
    CONF_PREFERRED_OUTPUT_PROTOCOL,
    CONF_PROTOCOL_DEFAULTS,
    CONF_PROTOCOL_KEY_SPLITTER,
)
from music_assistant.controllers.config.constants import PLAYER_VALUE_KEYS
from tests.common import MockProtocol, use_player_registry

# the real server with fake providers, for the config of a player that is offline
from tests.scenarios.conftest import scenario  # noqa: F401

from .helpers import attach_on_config

if TYPE_CHECKING:
    from music_assistant.controllers.config import ConfigController
    from music_assistant.mass import MusicAssistant
    from music_assistant.models.player import Player
    from tests.scenarios.harness import Scenario

# a player that was first seen after the split: its id is not that of its protocol
PLAYER_ID = "pl0123456789ab"
PROTOCOL_ID = "protocol_1"
PROVIDER = "test_instance"
# an entry the protocol object of a player declares
PROTOCOL_SETTING = "protocol_setting"
KITCHEN = {IdentifierType.MAC_ADDRESS: "54:78:C9:E6:0D:A0"}


@pytest.fixture
def config(mass_minimal: MusicAssistant) -> ConfigController:
    """Return the config controller with a stored config for a player and for its protocol."""
    use_player_registry(mass_minimal)
    _store(mass_minimal.config, PLAYER_ID, PROTOCOL_ID)
    return mass_minimal.config


def _store(
    config: ConfigController,
    player_id: str,
    protocol_id: str,
    role: ProtocolRole = ProtocolRole.NATIVE,
    player_type: PlayerType = PlayerType.PLAYER,
) -> None:
    """Store the config of a player and of one protocol of it."""
    if config.get(f"{CONF_PLAYERS}/{player_id}") is None:
        config.create_player_record(
            player_id, provider=PROVIDER, player_type=player_type, default_name="Player"
        )
    config.create_protocol_record(
        protocol_id, provider=PROVIDER, player_id=player_id, role=role, default_name="Player"
    )


def test_a_value_is_stored_with_the_id_it_was_set_for(config: ConfigController) -> None:
    """A player value and a protocol value are read back under the id they were stored for."""
    config.set_player_value(PLAYER_ID, CONF_HIDE_IN_UI, True)
    config.set_protocol_value(PROTOCOL_ID, CONF_OUTPUT_CHANNELS, "left")

    assert config.get_player_value(PLAYER_ID, CONF_HIDE_IN_UI) is True
    assert config.get_protocol_value(PROTOCOL_ID, CONF_OUTPUT_CHANNELS) == "left"
    assert config.get_player_value(PLAYER_ID, CONF_OUTPUT_CHANNELS) is None
    assert config.get_protocol_value(PROTOCOL_ID, CONF_HIDE_IN_UI) is None
    # neither id means anything in the other scope
    assert config.get_player_value(PROTOCOL_ID, CONF_HIDE_IN_UI) is None
    assert config.get_protocol_value(PLAYER_ID, CONF_OUTPUT_CHANNELS) is None


def test_a_missing_value_reads_as_the_default(config: ConfigController) -> None:
    """Nothing stored, a stored None and an unknown id all read as the given default."""
    config.set_protocol_value(PROTOCOL_ID, CONF_OUTPUT_CHANNELS, None)

    assert config.get_protocol_value(PROTOCOL_ID, CONF_OUTPUT_CHANNELS, "stereo") == "stereo"
    assert config.get_player_value(PLAYER_ID, CONF_HIDE_IN_UI, False) is False
    assert config.get_player_value("unknown", CONF_HIDE_IN_UI, False) is False
    assert config.get_protocol_value("unknown", CONF_ENABLED, True) is True


def test_a_base_key_is_read_and_stored_next_to_the_values(config: ConfigController) -> None:
    """A base key such as enabled is no value: it is stored on the config itself."""
    assert config.get_protocol_value(PROTOCOL_ID, "provider") == PROVIDER
    assert config.get_protocol_value(PROTOCOL_ID, "player_id") == PLAYER_ID
    assert config.get_player_value(PLAYER_ID, CONF_ENABLED) is True

    config.set_protocol_value(PROTOCOL_ID, CONF_ENABLED, False)
    config.set_player_value(PLAYER_ID, "name", "Kitchen")

    assert config.get(f"{CONF_PLAYER_PROTOCOLS}/{PROTOCOL_ID}/enabled") is False
    assert config.get(f"{CONF_PLAYERS}/{PLAYER_ID}/name") == "Kitchen"
    assert config.get(f"{CONF_PLAYER_PROTOCOLS}/{PROTOCOL_ID}/values") == {}
    assert config.get_protocol_value(PROTOCOL_ID, CONF_ENABLED, True) is False
    # the output of a player is switched off, the player is not
    assert config.get_player_value(PLAYER_ID, CONF_ENABLED) is True


def test_a_value_is_only_stored_for_a_stored_config(config: ConfigController) -> None:
    """Storing a value for an id without a stored config raises and creates nothing."""
    with pytest.raises(KeyError):
        config.set_player_value("unknown", CONF_HIDE_IN_UI, True)
    with pytest.raises(KeyError):
        config.set_protocol_value("unknown", CONF_OUTPUT_CHANNELS, "left")

    assert config.get(f"{CONF_PLAYERS}/unknown") is None
    assert config.get(f"{CONF_PLAYER_PROTOCOLS}/unknown") is None


def test_a_player_and_its_protocol_with_one_id_each_have_their_own_config(
    config: ConfigController,
) -> None:
    """A player from before the split shares its id with its protocol, and nothing else."""
    _store(config, "frozen_1", "frozen_1")

    config.set_player_value("frozen_1", CONF_HIDE_IN_UI, True)
    config.set_protocol_value("frozen_1", CONF_OUTPUT_CHANNELS, "left")
    config.set_protocol_value("frozen_1", CONF_ENABLED, False)

    assert config.get(f"{CONF_PLAYERS}/frozen_1/values") == {CONF_HIDE_IN_UI: True}
    assert config.get(f"{CONF_PLAYER_PROTOCOLS}/frozen_1/values") == {CONF_OUTPUT_CHANNELS: "left"}
    assert config.get_player_value("frozen_1", CONF_OUTPUT_CHANNELS) is None
    assert config.get_protocol_value("frozen_1", CONF_HIDE_IN_UI) is None
    assert config.get_player_value("frozen_1", CONF_ENABLED) is True
    assert config.get_protocol_value("frozen_1", CONF_ENABLED, True) is False


def test_protocols_are_listed_by_their_player_and_by_their_provider(
    config: ConfigController,
) -> None:
    """The stored protocols are found by the player they belong to and by their provider."""
    _store(config, PLAYER_ID, "protocol_2", ProtocolRole.AUDIO)
    config.set(f"{CONF_PLAYER_PROTOCOLS}/protocol_3", {"provider": "other", "player_id": None})

    assert config.get_protocol_ids_of_player(PLAYER_ID) == [PROTOCOL_ID, "protocol_2"]
    assert config.get_protocol_ids_of_player("protocol_3") == []
    assert config.get_protocol_ids_of_provider(PROVIDER) == [PROTOCOL_ID, "protocol_2"]
    assert config.get_protocol_ids_of_provider("other") == ["protocol_3"]


async def test_stored_value_of_the_own_protocol_is_read_without_the_entries(
    config: ConfigController, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A value of the own protocol of a player is found for the player, also while it is away."""
    config.set_protocol_value(PROTOCOL_ID, PROTOCOL_SETTING, "on")
    monkeypatch.setattr(config, "get_player_config", AsyncMock(side_effect=AssertionError))

    assert await config.get_player_config_value(PLAYER_ID, PROTOCOL_SETTING) == "on"


class _SettingProtocol(MockProtocol):
    """A protocol with audio output that declares one config entry of its own."""

    async def get_config_entries(self) -> list[ConfigEntry]:
        """Return the entry this protocol declares."""
        return [
            ConfigEntry(
                key=PROTOCOL_SETTING,
                type=ConfigEntryType.STRING,
                default_value="off",
                required=False,
            )
        ]


def _attach(
    mass: MusicAssistant,
    protocol_id: str = PROTOCOL_ID,
    role: ProtocolRole = ProtocolRole.NATIVE,
    player: Player | None = None,
    player_type: PlayerType = PlayerType.PLAYER,
) -> tuple[Player, MockProtocol]:
    """Attach a protocol with a stored config to the player with the stored config."""
    _store(mass.config, PLAYER_ID, protocol_id, role, player_type)
    features = {PlayerFeature.VOLUME_SET}
    if player_type != PlayerType.GROUP:
        features.add(PlayerFeature.PLAY_MEDIA)
    return attach_on_config(
        mass,
        PROVIDER,
        protocol_id,
        "Player",
        role=role,
        player=player,
        player_id=PLAYER_ID,
        features=features,
        player_type=player_type,
        protocol_class=_SettingProtocol,
    )


def _stored_values(mass: MusicAssistant, tree: str, config_id: str) -> dict[str, Any]:
    """Return the stored values of one config, without what a player stores about its device."""
    values: dict[str, Any] = mass.config.get(f"{tree}/{config_id}/values")
    return {
        key: value
        for key, value in values.items()
        if key not in (CONF_DEVICE_INFO, CONF_LAST_SEEN, CONF_PROTOCOL_DEFAULTS)
    }


SAVED_VALUES: dict[str, ConfigValueType] = {
    CONF_HIDE_IN_UI: True,
    PROTOCOL_SETTING: "on",
    CONF_OUTPUT_CHANNELS: "left",
}


@pytest.mark.parametrize("role", [ProtocolRole.NATIVE, ProtocolRole.CONTROL])
async def test_saving_stores_each_value_with_who_declared_its_entry(
    mass_minimal: MusicAssistant, role: ProtocolRole
) -> None:
    """A value is stored with the player or with its own protocol, whichever declared it."""
    mass = mass_minimal
    _attach(mass, role=role)
    # what the provider stores for its protocol is no entry, and stays where it is
    mass.config.set_protocol_value(PROTOCOL_ID, CONF_CACHED_ARP_MAC, "AA:BB:CC:DD:EE:FF")

    saved = await mass.config.save_player_config(PLAYER_ID, dict(SAVED_VALUES))

    assert _stored_values(mass, CONF_PLAYERS, PLAYER_ID) == {CONF_HIDE_IN_UI: True}
    assert _stored_values(mass, CONF_PLAYER_PROTOCOLS, PROTOCOL_ID) == {
        PROTOCOL_SETTING: "on",
        CONF_OUTPUT_CHANNELS: "left",
        CONF_CACHED_ARP_MAC: "AA:BB:CC:DD:EE:FF",
    }
    # the player's config still holds all of them, as one config
    assert {key: saved.get_value(key) for key in SAVED_VALUES} == SAVED_VALUES
    assert mass.config.get_player_value(PLAYER_ID, PROTOCOL_SETTING) is None
    assert mass.config.get_protocol_value(PROTOCOL_ID, PROTOCOL_SETTING) == "on"
    assert await mass.config.get_player_config_value(PLAYER_ID, PROTOCOL_SETTING) == "on"

    # a value set back to its default leaves the config it was stored on
    await mass.config.save_player_config(PLAYER_ID, {PROTOCOL_SETTING: "off"})

    assert _stored_values(mass, CONF_PLAYERS, PLAYER_ID) == {CONF_HIDE_IN_UI: True}
    assert _stored_values(mass, CONF_PLAYER_PROTOCOLS, PROTOCOL_ID) == {
        CONF_OUTPUT_CHANNELS: "left",
        CONF_CACHED_ARP_MAC: "AA:BB:CC:DD:EE:FF",
    }


async def test_saving_a_group_keeps_its_power_control_with_the_player(
    mass_minimal: MusicAssistant,
) -> None:
    """The power control of a group is a value of the player, its own settings are not."""
    mass = mass_minimal
    player, _ = _attach(mass, role=ProtocolRole.VIRTUAL, player_type=PlayerType.GROUP)

    await mass.config.save_player_config(
        PLAYER_ID, {CONF_POWER_CONTROL: PLAYER_CONTROL_FAKE, PROTOCOL_SETTING: "on"}
    )

    assert _stored_values(mass, CONF_PLAYERS, PLAYER_ID) == {
        CONF_POWER_CONTROL: PLAYER_CONTROL_FAKE
    }
    assert _stored_values(mass, CONF_PLAYER_PROTOCOLS, PROTOCOL_ID) == {PROTOCOL_SETTING: "on"}
    # read back with the id of each: the group is powered through its fake control
    assert mass.config.get_player_value(PLAYER_ID, CONF_POWER_CONTROL) == PLAYER_CONTROL_FAKE
    assert mass.config.get_protocol_value(PROTOCOL_ID, CONF_POWER_CONTROL) is None
    player._cache.clear()
    assert player._power_control == PLAYER_CONTROL_FAKE


async def test_saving_a_player_without_own_protocol_keeps_the_unprefixed_values_itself(
    mass_minimal: MusicAssistant,
) -> None:
    """The settings of an audio protocol are prefixed, and stored with that protocol."""
    mass = mass_minimal
    _attach(mass, role=ProtocolRole.AUDIO)
    prefix = f"{PROTOCOL_ID}{CONF_PROTOCOL_KEY_SPLITTER}"

    saved = await mass.config.save_player_config(
        PLAYER_ID,
        {
            CONF_HIDE_IN_UI: True,
            f"{prefix}{PROTOCOL_SETTING}": "on",
            f"{prefix}{CONF_OUTPUT_CHANNELS}": "left",
        },
    )

    assert _stored_values(mass, CONF_PLAYERS, PLAYER_ID) == {CONF_HIDE_IN_UI: True}
    assert _stored_values(mass, CONF_PLAYER_PROTOCOLS, PROTOCOL_ID) == {
        PROTOCOL_SETTING: "on",
        CONF_OUTPUT_CHANNELS: "left",
    }
    assert saved.get_value(f"{prefix}{PROTOCOL_SETTING}") == "on"


async def test_a_rejected_save_restores_the_config_of_the_player_and_of_its_protocol(
    mass_minimal: MusicAssistant,
) -> None:
    """A save the players controller rejects leaves both stored configs as they were."""
    mass = mass_minimal
    _attach(mass)
    await mass.config.save_player_config(PLAYER_ID, dict(SAVED_VALUES))
    mass.players.on_player_config_change = AsyncMock(  # type: ignore[method-assign]
        side_effect=RuntimeError("rejected")
    )

    with pytest.raises(RuntimeError, match="rejected"):
        await mass.config.save_player_config(
            PLAYER_ID, {CONF_HIDE_IN_UI: False, PROTOCOL_SETTING: "other"}
        )

    assert _stored_values(mass, CONF_PLAYERS, PLAYER_ID) == {CONF_HIDE_IN_UI: True}
    assert _stored_values(mass, CONF_PLAYER_PROTOCOLS, PROTOCOL_ID) == {
        PROTOCOL_SETTING: "on",
        CONF_OUTPUT_CHANNELS: "left",
    }


async def test_protocol_without_a_stored_config_keeps_its_values_in_memory(
    mass_minimal: MusicAssistant,
) -> None:
    """A protocol that only exists while connected takes values without storing any."""
    mass = mass_minimal
    _, protocol = _attach(mass)
    mass.config.remove(f"{CONF_PLAYER_PROTOCOLS}/{PROTOCOL_ID}")
    protocol.set_config(await mass.config.get_protocol_config(protocol))

    mass.config.set_protocol_value(PROTOCOL_ID, PROTOCOL_SETTING, "on")
    mass.config.set_protocol_setup_value(PROTOCOL_ID, "token", "secret")

    assert mass.config.get(f"{CONF_PLAYER_PROTOCOLS}/{PROTOCOL_ID}") is None
    assert mass.config.get_protocol_value(PROTOCOL_ID, PROTOCOL_SETTING) == "on"
    assert mass.config.get_protocol_value(PROTOCOL_ID, "provider") == PROVIDER
    assert protocol.get_setup_value("token") == "secret"
    assert (await mass.config.get_protocol_config(protocol)).get_value(PROTOCOL_SETTING) == "on"


@pytest.mark.parametrize(
    ("role", "player_type"),
    [(ProtocolRole.NATIVE, PlayerType.PLAYER), (ProtocolRole.VIRTUAL, PlayerType.GROUP)],
)
async def test_the_entries_every_player_gets_are_values_of_the_player_itself(
    mass_minimal: MusicAssistant, role: ProtocolRole, player_type: PlayerType
) -> None:
    """The entries the players controller adds to every player are stored with the player."""
    player, protocol = _attach(mass_minimal, role=role, player_type=player_type)
    config = mass_minimal.config

    own_keys = {entry.key for entry in config._get_default_player_config_entries(player)}
    protocol_keys = {entry.key for entry in await config._get_protocol_config_entries(protocol)}

    assert own_keys
    assert own_keys | {CONF_PREFERRED_OUTPUT_PROTOCOL} <= PLAYER_VALUE_KEYS
    assert PROTOCOL_SETTING in protocol_keys
    assert not protocol_keys & PLAYER_VALUE_KEYS


async def test_saving_a_player_without_an_attached_protocol_keeps_its_default_name(
    scenario: Scenario,  # noqa: F811
) -> None:
    """The name its device reported stays stored while the player is renamed offline."""
    sonos = await scenario.add_provider("sonos")
    speaker = sonos.protocol(
        "speaker_1", "Speaker", identifiers={IdentifierType.MAC_ADDRESS: "54:78:C9:E6:0D:A1"}
    )
    await scenario.register(speaker)
    player_id = scenario.single_player(speaker).player_id
    await scenario.unregister(speaker)

    await scenario.configure_player(player_id, name="Kitchen")
    await scenario.configure_player(player_id, name="Study")

    stored = scenario.mass.config.get(f"{CONF_PLAYERS}/{player_id}")
    assert (stored["name"], stored["default_name"]) == ("Study", "Speaker")


async def test_saving_a_player_kept_without_protocols_keeps_its_default_name(
    scenario: Scenario,  # noqa: F811
) -> None:
    """A former wrapper the migration kept for its returning device keeps its device name."""
    await scenario.seed_pre_split_config(
        {
            "up0001": {
                "player_id": "up0001",
                "provider": "universal_player",
                "player_type": "player",
                "enabled": True,
                "name": "Den",
                "default_name": "Den TV",
                "values": {"icon": "mdi-television"},
            }
        }
    )

    await scenario.configure_player("up0001", name="Study")

    stored = scenario.mass.config.get(f"{CONF_PLAYERS}/up0001")
    assert (stored["name"], stored["default_name"]) == ("Study", "Den TV")


async def _session_of_a_device_client(scenario: Scenario) -> str:  # noqa: F811
    """
    Connect a client of a device as a player of its own, while its config names the device.

    Returns the id of the player of the device.
    """
    sonos = await scenario.add_provider("sonos")
    sendspin = await scenario.add_provider("sendspin")
    native = sonos.protocol("RINCON_KITCHEN", "Kitchen", identifiers=KITCHEN)
    await scenario.register(native)
    player_id = scenario.single_player(native).player_id
    client = sendspin.protocol("sendspin_kitchen", "Kitchen", role="audio", identifiers=KITCHEN)
    await scenario.register(client)
    assert scenario.player_of(client) == player_id
    await scenario.unregister(client)
    session = sendspin.protocol("sendspin_kitchen", "Kitchen", role="native", session_only=True)
    await scenario.register(session)
    assert scenario.player_of(session) == "sendspin_kitchen"
    return player_id


def _listed_protocols(scenario: Scenario, player_id: str) -> list[str]:  # noqa: F811
    """Return the ids of the protocols a player lists."""
    state = scenario.player(player_id)
    assert state is not None
    return [info.protocol_id for info in state.protocols]


async def test_customised_session_only_player_takes_its_protocol_config_from_the_device(
    scenario: Scenario,  # noqa: F811
) -> None:
    """
    The first save of a web player moves the config of its protocol from the device to it.

    The player of the device stops listing the protocol at once. When the client connects as
    an output of the device again, it is found by the identifiers of the device.
    """
    player_id = await _session_of_a_device_client(scenario)
    assert _listed_protocols(scenario, player_id) == ["RINCON_KITCHEN", "sendspin_kitchen"]

    await scenario.configure_player("sendspin_kitchen", name="Kitchen browser")

    config = scenario.mass.config
    assert config.get(f"{CONF_PLAYERS}/sendspin_kitchen/name") == "Kitchen browser"
    assert (config.get_protocol_record("sendspin_kitchen") or {}).get("player_id") == (
        "sendspin_kitchen"
    )
    assert _listed_protocols(scenario, player_id) == ["RINCON_KITCHEN"]

    session = scenario.protocol("sendspin_kitchen")
    await scenario.unregister(session)
    client = scenario.provider("sendspin").protocol(
        "sendspin_kitchen", "Kitchen", role="audio", identifiers=KITCHEN
    )
    await scenario.register(client)
    await scenario.idle()

    assert scenario.player_of(client) == player_id
    assert _listed_protocols(scenario, player_id) == ["RINCON_KITCHEN", "sendspin_kitchen"]


async def test_rejected_first_save_of_a_session_only_player_leaves_the_config_of_the_device(
    scenario: Scenario,  # noqa: F811
) -> None:
    """A web player whose first save is refused leaves the config its protocol had untouched."""
    player_id = await _session_of_a_device_client(scenario)
    config = scenario.mass.config
    stored = deepcopy(config.get(CONF_PLAYER_PROTOCOLS))
    stored_players = deepcopy(config.get(CONF_PLAYERS))

    with pytest.raises(InvalidDataError):
        await config.save_player_config(
            "sendspin_kitchen", {CONF_MIN_VOLUME: 80, CONF_MAX_VOLUME: 20}
        )
    await scenario.settle()

    assert config.get(CONF_PLAYER_PROTOCOLS) == stored
    assert config.get(CONF_PLAYERS) == stored_players
    assert _listed_protocols(scenario, player_id) == ["RINCON_KITCHEN", "sendspin_kitchen"]

    await scenario.unregister(scenario.protocol("sendspin_kitchen"))
    client = scenario.provider("sendspin").protocol(
        "sendspin_kitchen", "Kitchen", role="audio", identifiers=KITCHEN
    )
    await scenario.register(client)
    await scenario.idle()

    assert scenario.player_of(client) == player_id
