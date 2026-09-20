"""
Regression tests for player config reads not crashing on a provider-less entry.

Reproduces music-assistant/support#6430: a partial player config dict left on
disk by an older version (it lost its mandatory ``provider``/``player_id`` keys)
crashed the config read with a mashumaro ``Field "provider" of type str is
missing`` error. The getters now recover the base keys when they can (from the
live player, or from the caller's authoritative provider during construction),
persist the repair, and drop an unreconstructable ghost otherwise, instead of
crashing. A merely offline player with an intact config is returned untouched.
"""

from typing import Any
from unittest.mock import MagicMock

import pytest
from music_assistant_models.enums import PlayerType

from music_assistant.constants import CONF_PLAYERS
from music_assistant.mass import MusicAssistant

PLAYER_ID = "ap265463f0314b"


def _ghost_entry() -> dict[str, Any]:
    """Return a partial dict as resurrected by the config-set footgun (only values)."""
    # a fresh dict per call: config.set stores it by reference and the heal mutates it
    return {"values": {"volume": 50}}


def _registered_player() -> MagicMock:
    """Return a minimal stand-in for a registered/live player."""
    player = MagicMock()
    player.state.name = "Bedroom"
    player.state.available = True
    player.state.type = PlayerType.PLAYER
    player.provider.instance_id = "airplay"
    return player


async def test_provider_less_entry_of_unregistered_player_is_dropped(
    mass_minimal: MusicAssistant,
) -> None:
    """A stored entry missing its provider is removed instead of crashing the read."""
    # the player is not registered, so its provider cannot be recovered from a live object
    mass_minimal.players = MagicMock()
    mass_minimal.players.get_player.return_value = None
    mass_minimal.config.set(f"{CONF_PLAYERS}/{PLAYER_ID}", _ghost_entry())

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


async def test_registered_player_with_ghost_config_recovers(
    mass_minimal: MusicAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A registered player whose stored entry lost its base keys still reads without crashing."""
    mass_minimal.players = MagicMock()
    mass_minimal.players.get_player.return_value = _registered_player()
    # isolate the base-key handling from the provider-specific entry machinery
    monkeypatch.setattr(mass_minimal.config, "get_player_config_entries", _empty_entries)
    monkeypatch.setattr(
        mass_minimal.config, "_get_output_protocol_config_values", _empty_values_async
    )
    monkeypatch.setattr(mass_minimal.config, "_get_plugin_provider_config_values", lambda _e: {})
    mass_minimal.config.set(f"{CONF_PLAYERS}/{PLAYER_ID}", _ghost_entry())

    conf = await mass_minimal.config.get_player_config(PLAYER_ID)

    assert conf.player_id == PLAYER_ID
    assert conf.provider == "airplay"
    # the repair is persisted, so the entry survives a later prune once the player is offline
    assert mass_minimal.config.get(f"{CONF_PLAYERS}/{PLAYER_ID}/player_id") == PLAYER_ID
    assert mass_minimal.config.get(f"{CONF_PLAYERS}/{PLAYER_ID}/provider") == "airplay"


async def test_non_mapping_entry_is_pruned_from_list(
    mass_minimal: MusicAssistant,
) -> None:
    """A corrupt non-mapping entry is removed instead of crashing the list read."""
    mass_minimal.players = MagicMock()
    mass_minimal.players.get_player.return_value = None
    mass_minimal.config.create_default_player_config(
        "apvalid", "airplay", PlayerType.PLAYER, "Valid"
    )
    mass_minimal.config.set(f"{CONF_PLAYERS}/apbroken", "not-a-dict")

    configs = await mass_minimal.config.get_player_configs()

    ids = {conf.player_id for conf in configs}
    assert "apvalid" in ids
    assert mass_minimal.config.get(f"{CONF_PLAYERS}/apbroken") is None


async def test_create_default_overwrites_non_mapping_entry(
    mass_minimal: MusicAssistant,
) -> None:
    """Registration replaces a corrupt non-mapping entry with a valid default config."""
    mass_minimal.config.set(f"{CONF_PLAYERS}/{PLAYER_ID}", "not-a-dict")

    mass_minimal.config.create_default_player_config(
        PLAYER_ID, "airplay", PlayerType.PLAYER, "Bedroom"
    )

    assert mass_minimal.config.get(f"{CONF_PLAYERS}/{PLAYER_ID}/player_id") == PLAYER_ID
    assert mass_minimal.config.get(f"{CONF_PLAYERS}/{PLAYER_ID}/provider") == "airplay"


async def test_get_base_player_config_recovers_missing_base_keys(
    mass_minimal: MusicAssistant,
) -> None:
    """Player construction recovers a ghost entry from the caller's provider."""
    # get_base_player_config runs during Player.__init__, the path behind the reported
    # recurring crash on every mDNS announce for a device with a leftover ghost config
    mass_minimal.config.set(f"{CONF_PLAYERS}/{PLAYER_ID}", _ghost_entry())

    conf = mass_minimal.config.get_base_player_config(PLAYER_ID, "airplay")

    assert conf.provider == "airplay"
    assert conf.player_id == PLAYER_ID


async def test_create_default_player_config_heals_existing_ghost(
    mass_minimal: MusicAssistant,
) -> None:
    """Registration heals a leftover ghost on disk while preserving its stored values."""
    mass_minimal.config.set(f"{CONF_PLAYERS}/{PLAYER_ID}", _ghost_entry())

    mass_minimal.config.create_default_player_config(
        PLAYER_ID, "airplay", PlayerType.PLAYER, "Bedroom"
    )

    assert mass_minimal.config.get(f"{CONF_PLAYERS}/{PLAYER_ID}/player_id") == PLAYER_ID
    assert mass_minimal.config.get(f"{CONF_PLAYERS}/{PLAYER_ID}/provider") == "airplay"
    assert mass_minimal.config.get(f"{CONF_PLAYERS}/{PLAYER_ID}/values") == {"volume": 50}


async def test_get_player_configs_drops_provider_less_ghost(
    mass_minimal: MusicAssistant,
) -> None:
    """The player list prunes a malformed ghost instead of crashing, keeping valid ones."""
    mass_minimal.players = MagicMock()
    mass_minimal.players.get_player.return_value = None
    mass_minimal.config.create_default_player_config(
        "apvalid", "airplay", PlayerType.PLAYER, "Valid"
    )
    mass_minimal.config.set(f"{CONF_PLAYERS}/apghost", _ghost_entry())

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
    mass_minimal.config.set(f"{CONF_PLAYERS}/apghost", _ghost_entry())

    configs = await mass_minimal.config.get_player_configs(include_values=True)

    ids = {conf.player_id for conf in configs}
    assert "apvalid" in ids
    assert "apghost" not in ids


async def test_get_player_configs_persists_recovered_ghost_for_registered_player(
    mass_minimal: MusicAssistant,
) -> None:
    """A ghost of a live player is healed from the live provider and persisted, not pruned."""
    mass_minimal.players = MagicMock()
    mass_minimal.players.get_player.return_value = _registered_player()
    mass_minimal.config.set(f"{CONF_PLAYERS}/{PLAYER_ID}", _ghost_entry())

    configs = await mass_minimal.config.get_player_configs()

    assert PLAYER_ID in {conf.player_id for conf in configs}
    assert mass_minimal.config.get(f"{CONF_PLAYERS}/{PLAYER_ID}/provider") == "airplay"
    assert mass_minimal.config.get(f"{CONF_PLAYERS}/{PLAYER_ID}/player_id") == PLAYER_ID


async def _empty_entries(_player_id: str) -> list[Any]:
    return []


async def _empty_values_async(_entries: list[Any]) -> dict[str, Any]:
    return {}
