"""Tests for handing a player's stored config over to the player that replaces it."""

import logging
from typing import TYPE_CHECKING, cast
from unittest.mock import MagicMock

from music_assistant_models.enums import PlayerFeature, PlayerType, ProviderType

from music_assistant.constants import (
    CONF_LINKED_PROTOCOL_IDS,
    CONF_PLAYER_DSP,
    CONF_PLAYERS,
    CONF_PROTOCOL_PARENT_ID,
)
from music_assistant.mass import MusicAssistant
from music_assistant.models.player import DeviceInfo, Player

if TYPE_CHECKING:
    from music_assistant.providers.universal_player import UniversalPlayerProvider

OLD_ID = "20:F8:3B:09:42:05"
NEW_ID = "new-client-id"


class _TestProvider:
    """Minimal player provider stand-in backed by the real MusicAssistant."""

    def __init__(self, mass: MusicAssistant, domain: str) -> None:
        """Initialize the test provider."""
        self.mass = mass
        self.domain = domain
        # keeps the real provider of this domain registered, so it still unloads
        self.instance_id = f"{domain}--test"
        self.translation_owner = f"provider.{domain}"
        self.name = domain
        self.available = True
        self.unloading = False
        self.logger = logging.getLogger(f"test.{domain}")
        self.manifest = MagicMock()
        self.manifest.domain = domain
        self.manifest.type = ProviderType.PLAYER
        self.type = ProviderType.PLAYER
        self.players: list[Player] = []

    async def unload(self, is_removed: bool = False) -> None:
        """Unload the provider (nothing to clean up)."""


class _TestPlayer(Player):
    """Minimal concrete player that registers on the real player controller."""

    def __init__(self, provider: _TestProvider, player_id: str, player_type: PlayerType) -> None:
        """Initialize the test player."""
        super().__init__(provider, player_id)  # type: ignore[arg-type]
        self._attr_name = player_id
        self._attr_type = player_type
        self._attr_available = True
        self._attr_powered = True
        self._attr_supported_features = {PlayerFeature.VOLUME_SET, PlayerFeature.PLAY_MEDIA}
        self._attr_device_info = DeviceInfo(model="Test Model", manufacturer="Test Manufacturer")
        self._cache.clear()

    async def stop(self) -> None:
        """Stop playback, required abstract method."""


def _store_old_player(mass: MusicAssistant) -> None:
    mass.config.set(
        f"{CONF_PLAYERS}/{OLD_ID}",
        {
            "player_id": OLD_ID,
            "provider": "sendspin",
            "player_type": "protocol",
            "enabled": True,
            "values": {
                CONF_PROTOCOL_PARENT_ID: "up_original",
                "output_channels": "left",
                "static_delay": 50,
            },
        },
    )
    mass.config.set(f"{CONF_PLAYER_DSP}/{OLD_ID}", {"enabled": True})


async def test_replacement_without_config_gets_a_copy(mass: MusicAssistant) -> None:
    """A replacement that was never seen before starts out as the replaced player."""
    _store_old_player(mass)

    mass.players.carry_over_player_config(OLD_ID, NEW_ID)

    new_conf = mass.config.get(f"{CONF_PLAYERS}/{NEW_ID}")
    assert new_conf["player_id"] == NEW_ID
    assert new_conf["values"] == {
        CONF_PROTOCOL_PARENT_ID: "up_original",
        "output_channels": "left",
        "static_delay": 50,
    }
    assert mass.config.get(f"{CONF_PLAYER_DSP}/{NEW_ID}") == {"enabled": True}


async def test_existing_replacement_keeps_its_values_but_takes_the_parent(
    mass: MusicAssistant,
) -> None:
    """A replacement that already got its own parent moves back to the original one."""
    _store_old_player(mass)
    mass.config.set(
        f"{CONF_PLAYERS}/{NEW_ID}",
        {
            "player_id": NEW_ID,
            "provider": "sendspin",
            "player_type": "protocol",
            "enabled": True,
            "values": {CONF_PROTOCOL_PARENT_ID: "up_duplicate", "output_channels": "stereo"},
        },
    )

    mass.players.carry_over_player_config(OLD_ID, NEW_ID)

    assert mass.config.get(f"{CONF_PLAYERS}/{NEW_ID}/values") == {
        CONF_PROTOCOL_PARENT_ID: "up_original",
        "output_channels": "stereo",
        "static_delay": 50,
    }


async def test_parent_lists_the_replacement(mass: MusicAssistant) -> None:
    """A parent that is enabled again finds the replacement among its protocol players."""
    _store_old_player(mass)
    mass.config.set(
        f"{CONF_PLAYERS}/up_original",
        {
            "player_id": "up_original",
            "provider": "universal_player",
            "player_type": "player",
            "enabled": False,
            "values": {CONF_LINKED_PROTOCOL_IDS: [OLD_ID]},
        },
    )

    mass.players.carry_over_player_config(OLD_ID, NEW_ID)

    assert mass.config.get(f"{CONF_PLAYERS}/up_original/values/{CONF_LINKED_PROTOCOL_IDS}") == [
        OLD_ID,
        NEW_ID,
    ]


async def test_previous_parent_no_longer_lists_the_replacement(mass: MusicAssistant) -> None:
    """A parent the replacement leaves can not disable it later through its stored list."""
    _store_old_player(mass)
    mass.config.set(
        f"{CONF_PLAYERS}/{NEW_ID}",
        {
            "player_id": NEW_ID,
            "provider": "sendspin",
            "player_type": "protocol",
            "enabled": True,
            "values": {CONF_PROTOCOL_PARENT_ID: "up_duplicate"},
        },
    )
    mass.config.set(
        f"{CONF_PLAYERS}/up_duplicate",
        {
            "player_id": "up_duplicate",
            "provider": "universal_player",
            "player_type": "player",
            "enabled": False,
            "values": {CONF_LINKED_PROTOCOL_IDS: [NEW_ID]},
        },
    )

    mass.players.carry_over_player_config(OLD_ID, NEW_ID)

    assert mass.config.get(f"{CONF_PLAYERS}/up_duplicate/values/{CONF_LINKED_PROTOCOL_IDS}") == []


async def test_replacement_takes_the_place_of_a_still_registered_player(
    mass: MusicAssistant,
) -> None:
    """The replacement joins the wrapper right away, also when the old player was registered."""
    sendspin_provider = _TestProvider(mass, "sendspin")
    mass._providers[sendspin_provider.instance_id] = sendspin_provider  # type: ignore[assignment]
    universal_provider = cast("UniversalPlayerProvider", mass.get_provider("universal_player"))
    wrapper = await universal_provider.create_universal_player(
        player_id="up_original",
        name="Kitchen",
        device_info=DeviceInfo(),
        protocol_player_ids=[OLD_ID],
    )
    old_player = _TestPlayer(sendspin_provider, OLD_ID, PlayerType.PROTOCOL)
    await mass.players.register(old_player)
    assert old_player.protocol_parent_id == "up_original"

    await mass.players.unregister(OLD_ID)
    mass.players.carry_over_player_config(OLD_ID, NEW_ID)
    new_player = _TestPlayer(sendspin_provider, NEW_ID, PlayerType.PROTOCOL)
    await mass.players.register(new_player)
    mass.players.delete_player_config(OLD_ID, replacement_player_id=NEW_ID)

    assert new_player.protocol_parent_id == "up_original"
    assert [link.output_protocol_id for link in wrapper.linked_output_protocols] == [NEW_ID]
    assert mass.config.get(f"{CONF_PLAYERS}/up_original/values/{CONF_LINKED_PROTOCOL_IDS}") == [
        NEW_ID
    ]
