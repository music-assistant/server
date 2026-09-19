"""
Regression tests for get_player_config not crashing on a provider-less entry.

Reproduces music-assistant/support#6430: a partial player config dict left on
disk by an older version (it lost its mandatory ``provider`` key) crashed the
config read with a mashumaro ``Field "provider" of type str is missing`` error
whenever the player was not currently registered. The single-player getter now
drops such an unreconstructable ghost and reports it as gone, while a merely
offline player with an intact config is still returned untouched.
"""

from unittest.mock import MagicMock

import pytest
from music_assistant_models.enums import PlayerType

from music_assistant.constants import CONF_PLAYERS
from music_assistant.mass import MusicAssistant

PLAYER_ID = "ap265463f0314b"


async def test_provider_less_entry_of_unregistered_player_is_dropped(
    mass_minimal: MusicAssistant,
) -> None:
    """A stored entry missing its provider is removed instead of crashing the read."""
    # the player is not registered, so its provider cannot be recovered from a live object
    mass_minimal.players = MagicMock()
    mass_minimal.players.get_player.return_value = None
    # a partial dict as resurrected by the config-set footgun: no base keys, only values
    mass_minimal.config.set(f"{CONF_PLAYERS}/{PLAYER_ID}", {"values": {"volume": 50}})

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
