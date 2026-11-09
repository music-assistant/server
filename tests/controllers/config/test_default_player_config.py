"""
Tests for create_default_player_config.

An existing stored player_type is never rewritten (support#5745), the default name
of an existing config follows the player while a name the user set stays, and a new
config stores the name as its default name only.
"""

from music_assistant_models.enums import PlayerType

from music_assistant.constants import CONF_PLAYERS
from music_assistant.mass import MusicAssistant

PLAYER_ID = "e4:5f:01:70:ef:67"


async def test_existing_player_type_not_rewritten(mass_minimal: MusicAssistant) -> None:
    """A repeated call with a transient type must not touch the persisted player_type."""
    mass_minimal.config.create_default_player_config(
        PLAYER_ID, "squeezelite", PlayerType.PROTOCOL, "solarium-bath-sl"
    )
    assert mass_minimal.config.get(f"{CONF_PLAYERS}/{PLAYER_ID}/player_type") == "protocol"

    # a later call with another type
    mass_minimal.config.create_default_player_config(PLAYER_ID, "squeezelite", PlayerType.PLAYER)

    assert mass_minimal.config.get(f"{CONF_PLAYERS}/{PLAYER_ID}/player_type") == "protocol"


async def test_existing_default_name_still_updated(mass_minimal: MusicAssistant) -> None:
    """The default_name update for existing configs keeps working."""
    mass_minimal.config.create_default_player_config(
        PLAYER_ID, "squeezelite", PlayerType.PROTOCOL, "old-name"
    )
    mass_minimal.config.create_default_player_config(
        PLAYER_ID, "squeezelite", PlayerType.PROTOCOL, "new-name"
    )
    assert mass_minimal.config.get(f"{CONF_PLAYERS}/{PLAYER_ID}/default_name") == "new-name"


async def test_creation_name_is_stored_as_default_name_only(mass_minimal: MusicAssistant) -> None:
    """
    The name a player is created with is stored as its default name only.

    Regression test for music-assistant/support#5888: a stored name used to be
    written for fresh configs as well, which is indistinguishable from a user
    rename. It kept shadowing every later default name and was carried over to
    any replacement player, producing duplicated friendly names.
    """
    mass_minimal.config.create_default_player_config(
        PLAYER_ID, "squeezelite", PlayerType.PROTOCOL, "old-name"
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
    mass_minimal.config.create_default_player_config(
        PLAYER_ID, "squeezelite", PlayerType.PROTOCOL, "old-name"
    )
    mass_minimal.config.set(f"{CONF_PLAYERS}/{PLAYER_ID}/name", "My Custom Name")

    mass_minimal.config.set_player_default_name(PLAYER_ID, "new-name")

    assert mass_minimal.config.get(f"{CONF_PLAYERS}/{PLAYER_ID}/default_name") == "new-name"
    assert mass_minimal.config.get(f"{CONF_PLAYERS}/{PLAYER_ID}/name") == "My Custom Name"
