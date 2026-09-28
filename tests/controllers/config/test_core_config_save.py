"""Tests for what a core config save keeps in, and drops from, persistent storage."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest

from music_assistant.constants import CONF_CORE, CONF_LOG_LEVEL

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant

_DOMAIN = "cache"
_RAW_KEY = "runtime_state"
_RAW_VALUE = ["spotify--abc", "tidal--def"]


async def test_undeclared_raw_value_survives_a_core_config_save(mass: MusicAssistant) -> None:
    """A value stored without a matching config entry is carried over by the settings save."""
    mass.config.set_raw_core_config_value(_DOMAIN, _RAW_KEY, _RAW_VALUE)

    await mass.config.save_core_config(_DOMAIN, {CONF_LOG_LEVEL: "DEBUG"})

    assert mass.config.get_raw_core_config_value(_DOMAIN, _RAW_KEY) == _RAW_VALUE
    assert mass.config.get_raw_core_config_value(_DOMAIN, CONF_LOG_LEVEL) == "DEBUG"


async def test_declared_value_at_its_default_is_dropped_from_storage(
    mass: MusicAssistant,
) -> None:
    """Storage stays minimal: a declared entry set back to its default leaves no value behind."""
    mass.config.set_raw_core_config_value(_DOMAIN, _RAW_KEY, _RAW_VALUE)
    await mass.config.save_core_config(_DOMAIN, {CONF_LOG_LEVEL: "DEBUG"})

    await mass.config.save_core_config(_DOMAIN, {CONF_LOG_LEVEL: "GLOBAL"})

    stored_values = mass.config.get(f"{CONF_CORE}/{_DOMAIN}/values")
    assert CONF_LOG_LEVEL not in stored_values
    assert stored_values[_RAW_KEY] == _RAW_VALUE


async def test_a_failed_save_reverts_without_dropping_undeclared_values(
    mass: MusicAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The revert after a failed controller update restores the raw values along with the rest."""
    mass.config.set_raw_core_config_value(_DOMAIN, _RAW_KEY, _RAW_VALUE)
    monkeypatch.setattr(mass.cache, "update_config", AsyncMock(side_effect=RuntimeError("boom")))

    with pytest.raises(RuntimeError, match="boom"):
        await mass.config.save_core_config(_DOMAIN, {CONF_LOG_LEVEL: "DEBUG"})

    stored_values = mass.config.get(f"{CONF_CORE}/{_DOMAIN}/values")
    assert CONF_LOG_LEVEL not in stored_values
    assert stored_values[_RAW_KEY] == _RAW_VALUE
