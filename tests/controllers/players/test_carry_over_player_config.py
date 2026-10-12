"""Tests for handing a player's stored config over to the player that replaces it."""

from music_assistant.constants import (
    CONF_LINKED_PROTOCOL_IDS,
    CONF_PLAYER_DSP,
    CONF_PLAYERS,
    CONF_PROTOCOL_PARENT_ID,
)
from music_assistant.mass import MusicAssistant

OLD_ID = "20:F8:3B:09:42:05"
NEW_ID = "new-client-id"


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
