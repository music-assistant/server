"""
Regression tests for player config reads not crashing on a provider-less entry.

Reproduces music-assistant/support#6430: a partial player config dict left on
disk by an older version (it lost its mandatory ``provider`` key) crashed the
config read with a mashumaro ``Field "provider" of type str is missing`` error.
The getters now recover the base keys when they can (from the live player, or
from the caller's authoritative provider during player construction) and drop
an unreconstructable ghost otherwise, instead of crashing. A merely offline
player with an intact config is returned untouched.
"""

from unittest.mock import MagicMock

import pytest
from music_assistant_models.enums import PlayerType

from music_assistant.constants import CONF_PLAYERS
from music_assistant.mass import MusicAssistant

PLAYER_ID = "ap265463f0314b"
# a partial dict as resurrected by the config-set footgun: no base keys, only values
GHOST_ENTRY = {"values": {"volume": 50}}


async def test_provider_less_entry_of_unregistered_player_is_dropped(
    mass_minimal: MusicAssistant,
) -> None:
    """A stored entry missing its provider is removed instead of crashing the read."""
    # the player is not registered, so its provider cannot be recovered from a live object
    mass_minimal.players = MagicMock()
    mass_minimal.players.get_player.return_value = None
    mass_minimal.config.set(f"{CONF_PLAYERS}/{PLAYER_ID}", GHOST_ENTRY)

    with pytest.raises(KeyError):
        await mass_minimal.config.get_player_config(PLAYER_ID)

    # the ghost is gone, so a later read can never trip over it again
    assert mass_minimal.config.get(f"{CONF_PLAYERS}/{PLAYER_ID}") is None


async def test_offline_player_with_valid_config_is_still_returned(
    mass_minimal: MusicAssistant,
) -> None:
    """An intact config of an unregistered (offline) player must not be dropped."""
    mass_minimal.players = MagicMock()
    mass_minimal.players.get_player.return_value = None
    mass_minimal.config.create_default_player_config(
        PLAYER_ID, "airplay", PlayerType.PLAYER, "Bedroom"
    )

    conf = await mass_minimal.config.get_player_config(PLAYER_ID)

    assert conf.provider == "airplay"
    assert conf.enabled is True
    assert mass_minimal.config.get(f"{CONF_PLAYERS}/{PLAYER_ID}") is not None


async def test_get_base_player_config_recovers_missing_base_keys(
    mass_minimal: MusicAssistant,
) -> None:
    """Player construction recovers a ghost entry from the caller's provider."""
    # get_base_player_config runs during Player.__init__, the path behind the reported
    # recurring crash on every mDNS announce for a device with a leftover ghost config
    mass_minimal.config.set(f"{CONF_PLAYERS}/{PLAYER_ID}", GHOST_ENTRY)

    conf = mass_minimal.config.get_base_player_config(PLAYER_ID, "airplay")

    assert conf.provider == "airplay"
    assert conf.player_id == PLAYER_ID


async def test_get_player_configs_drops_provider_less_ghost(
    mass_minimal: MusicAssistant,
) -> None:
    """The player list prunes a malformed ghost instead of crashing, keeping valid ones."""
    mass_minimal.players = MagicMock()
    mass_minimal.players.get_player.return_value = None
    mass_minimal.config.create_default_player_config(
        "apvalid", "airplay", PlayerType.PLAYER, "Valid"
    )
    mass_minimal.config.set(f"{CONF_PLAYERS}/apghost", GHOST_ENTRY)

    configs = await mass_minimal.config.get_player_configs()

    ids = {conf.player_id for conf in configs}
    assert "apvalid" in ids
    assert "apghost" not in ids
    assert mass_minimal.config.get(f"{CONF_PLAYERS}/apghost") is None


async def test_get_player_configs_with_values_survives_ghost(
    mass_minimal: MusicAssistant,
) -> None:
    """The include_values list path also tolerates and prunes a malformed ghost."""
    mass_minimal.players = MagicMock()
    mass_minimal.players.get_player.return_value = None
    mass_minimal.config.create_default_player_config(
        "apvalid", "airplay", PlayerType.PLAYER, "Valid"
    )
    mass_minimal.config.set(f"{CONF_PLAYERS}/apghost", GHOST_ENTRY)

    configs = await mass_minimal.config.get_player_configs(include_values=True)

    ids = {conf.player_id for conf in configs}
    assert "apvalid" in ids
    assert "apghost" not in ids
