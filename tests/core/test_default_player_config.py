"""
Regression tests for create_default_player_config not corrupting existing configs.

Reproduces music-assistant/support#5745: Player.__init__ calls
create_default_player_config before provider setup has determined the real
player type, so the passed type can be a transient class default. For
squeezelite protocol players this rewrote the persisted player_type from
"protocol" to "player" on every client (re)connect. Normally healed right
after registration, but when registration was interrupted (e.g. shutdown
while clients reconnect) the corrupted row hit disk and the universal player
restore logic deleted the wrapping universal player config on next startup,
wiping all user customizations.
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

    # simulate reconnect: Player.__init__ runs with the class default type (player)
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
