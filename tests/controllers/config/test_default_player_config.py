"""
Tests for the config a new player is stored with.

The name of its device is stored as its default name only, which follows the device
while a name the user set stays.
"""

from music_assistant_models.enums import PlayerType

from music_assistant.constants import CONF_PLAYERS
from music_assistant.mass import MusicAssistant

PLAYER_ID = "pl0123456789ab"


async def test_new_player_config_holds_the_given_base_values(mass_minimal: MusicAssistant) -> None:
    """A new player config stores its provider, type and values, and nothing of a setup."""
    mass_minimal.config.create_player_record(
        PLAYER_ID,
        provider="squeezelite",
        player_type=PlayerType.PLAYER,
        default_name="old-name",
        enabled=False,
        values={"created_at": 1},
    )

    assert mass_minimal.config.get(f"{CONF_PLAYERS}/{PLAYER_ID}") == {
        "player_id": PLAYER_ID,
        "provider": "squeezelite",
        "player_type": "player",
        "enabled": False,
        "name": None,
        "default_name": "old-name",
        "values": {"created_at": 1},
    }


async def test_creation_name_is_stored_as_default_name_only(mass_minimal: MusicAssistant) -> None:
    """
    The name a player is created with is stored as its default name only.

    Regression test for music-assistant/support#5888: a stored name used to be
    written for fresh configs as well, which is indistinguishable from a user
    rename. It kept shadowing every later default name and was carried over to
    any replacement player, producing duplicated friendly names.
    """
    mass_minimal.config.create_player_record(
        PLAYER_ID, provider="squeezelite", player_type=PlayerType.PLAYER, default_name="old-name"
    )
    assert mass_minimal.config.get(f"{CONF_PLAYERS}/{PLAYER_ID}/name") is None
    assert mass_minimal.config.get(f"{CONF_PLAYERS}/{PLAYER_ID}/default_name") == "old-name"

    mass_minimal.config.set_player_default_name(PLAYER_ID, "new-name")

    assert mass_minimal.config.get(f"{CONF_PLAYERS}/{PLAYER_ID}/default_name") == "new-name"
    assert mass_minimal.config.get(f"{CONF_PLAYERS}/{PLAYER_ID}/name") is None


async def test_user_renamed_name_preserved_on_default_name_update(
    mass_minimal: MusicAssistant,
) -> None:
    """A real user rename is never overwritten by a default name update."""
    mass_minimal.config.create_player_record(
        PLAYER_ID, provider="squeezelite", player_type=PlayerType.PLAYER, default_name="old-name"
    )
    mass_minimal.config.set(f"{CONF_PLAYERS}/{PLAYER_ID}/name", "My Custom Name")

    mass_minimal.config.set_player_default_name(PLAYER_ID, "new-name")

    assert mass_minimal.config.get(f"{CONF_PLAYERS}/{PLAYER_ID}/default_name") == "new-name"
    assert mass_minimal.config.get(f"{CONF_PLAYERS}/{PLAYER_ID}/name") == "My Custom Name"


async def test_default_name_of_a_player_without_config_is_not_stored(
    mass_minimal: MusicAssistant,
) -> None:
    """A default name update never creates a partial config for a player that has none."""
    mass_minimal.config.set_player_default_name(PLAYER_ID, "new-name")

    assert mass_minimal.config.get(f"{CONF_PLAYERS}/{PLAYER_ID}") is None
