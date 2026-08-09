"""
Regression tests for removing a player config with linked protocol players.

Reproduces the case where a player is first disabled and then removed: disabling
cascades to the linked protocol players, so none of them is registered anymore when
the removal comes in. The leftover (disabled) protocol config is not shown anywhere
and keeps the device from ever registering again.
"""

from unittest.mock import MagicMock

from music_assistant.constants import CONF_PLAYER_DSP, CONF_PLAYERS, CONF_PROTOCOL_PARENT_ID
from music_assistant.mass import MusicAssistant

PARENT_ID = "up_esp32"
PROTOCOL_ID = "spb_esp32"


def _store_configs(mass: MusicAssistant, enabled: bool) -> None:
    """Store a universal player config with a single linked protocol player."""
    mass.config.set(
        f"{CONF_PLAYERS}/{PARENT_ID}",
        {
            "player_id": PARENT_ID,
            "provider": "universal_player",
            "player_type": "player",
            "enabled": enabled,
            "values": {"linked_protocol_ids": [PROTOCOL_ID]},
        },
    )
    mass.config.set(
        f"{CONF_PLAYERS}/{PROTOCOL_ID}",
        {
            "player_id": PROTOCOL_ID,
            "provider": "sendspin",
            "player_type": "protocol",
            "enabled": enabled,
            "values": {CONF_PROTOCOL_PARENT_ID: PARENT_ID},
        },
    )
    mass.config.set(f"{CONF_PLAYER_DSP}/{PROTOCOL_ID}", {"enabled": True})


async def test_remove_wipes_unregistered_protocol_configs(mass: MusicAssistant) -> None:
    """Removing a disabled player also wipes the config of its linked protocol player."""
    _store_configs(mass, enabled=False)

    await mass.config.remove_player_config(PARENT_ID)

    assert mass.config.get(f"{CONF_PLAYERS}/{PARENT_ID}") is None
    assert mass.config.get(f"{CONF_PLAYERS}/{PROTOCOL_ID}") is None
    assert mass.config.get(f"{CONF_PLAYER_DSP}/{PROTOCOL_ID}") is None


async def test_remove_wipes_protocol_configs_with_a_half_broken_link(
    mass: MusicAssistant,
) -> None:
    """A protocol player is wiped by its own parent reference, not by the parent's list."""
    _store_configs(mass, enabled=False)
    mass.config.set(f"{CONF_PLAYERS}/{PARENT_ID}/values/linked_protocol_ids", [])

    await mass.config.remove_player_config(PARENT_ID)

    assert mass.config.get(f"{CONF_PLAYERS}/{PROTOCOL_ID}") is None


async def test_remove_keeps_reparented_protocol_configs(mass: MusicAssistant) -> None:
    """A protocol player that already moved to another parent keeps its config."""
    _store_configs(mass, enabled=True)
    mass.config.set(f"{CONF_PLAYERS}/{PROTOCOL_ID}/values/{CONF_PROTOCOL_PARENT_ID}", "cast_1")

    mass.players.delete_player_config(PARENT_ID)

    assert mass.config.get(f"{CONF_PLAYERS}/{PARENT_ID}") is None
    assert mass.config.get(f"{CONF_PLAYERS}/{PROTOCOL_ID}") is not None


async def test_remove_keeps_registered_protocol_configs(mass: MusicAssistant) -> None:
    """A protocol player that is still registered keeps its config to be re-parented."""
    _store_configs(mass, enabled=True)
    mass.players._players[PROTOCOL_ID] = MagicMock()

    mass.players.delete_player_config(PARENT_ID)

    assert mass.config.get(f"{CONF_PLAYERS}/{PARENT_ID}") is None
    assert mass.config.get(f"{CONF_PLAYERS}/{PROTOCOL_ID}") is not None
    assert mass.config.get(f"{CONF_PLAYER_DSP}/{PROTOCOL_ID}") is not None
