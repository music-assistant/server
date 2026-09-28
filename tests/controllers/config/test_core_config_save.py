"""
Tests for what a core config save keeps in, and drops from, persistent storage.

State a core controller keeps next to its settings belongs in a declared (hidden) config
entry; keeping undeclared raw values across a save is the safety net, not the contract.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest

from music_assistant.constants import CONF_CORE, CONF_LOG_LEVEL

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant

_DOMAIN = "faketestcore"
_RAW_KEY = "runtime_state"
_RAW_VALUE = ["spotify--abc", "tidal--def"]


@pytest.fixture
def stub_mass(mass_minimal: MusicAssistant) -> MusicAssistant:
    """Return a minimal instance with a stub core controller that declares no entries of its own."""
    controller = SimpleNamespace(
        get_config_entries=AsyncMock(return_value=()), update_config=AsyncMock()
    )
    setattr(mass_minimal, _DOMAIN, controller)
    return mass_minimal


async def test_undeclared_raw_value_survives_a_core_config_save(stub_mass: MusicAssistant) -> None:
    """A value stored without a matching config entry is carried over by the settings save."""
    stub_mass.config.set_raw_core_config_value(_DOMAIN, _RAW_KEY, _RAW_VALUE)

    await stub_mass.config.save_core_config(_DOMAIN, {CONF_LOG_LEVEL: "DEBUG"})

    assert stub_mass.config.get_raw_core_config_value(_DOMAIN, _RAW_KEY) == _RAW_VALUE
    assert stub_mass.config.get_raw_core_config_value(_DOMAIN, CONF_LOG_LEVEL) == "DEBUG"


async def test_declared_value_at_its_default_is_dropped_from_storage(
    stub_mass: MusicAssistant,
) -> None:
    """Storage stays minimal: a declared entry set back to its default leaves no value behind."""
    stub_mass.config.set_raw_core_config_value(_DOMAIN, _RAW_KEY, _RAW_VALUE)
    await stub_mass.config.save_core_config(_DOMAIN, {CONF_LOG_LEVEL: "DEBUG"})

    await stub_mass.config.save_core_config(_DOMAIN, {CONF_LOG_LEVEL: "GLOBAL"})

    stored_values = stub_mass.config.get(f"{CONF_CORE}/{_DOMAIN}/values")
    assert CONF_LOG_LEVEL not in stored_values
    assert stored_values[_RAW_KEY] == _RAW_VALUE


async def test_a_failed_save_reverts_without_dropping_undeclared_values(
    stub_mass: MusicAssistant,
) -> None:
    """The revert after a failed controller update restores the raw values along with the rest."""
    stub_mass.config.set_raw_core_config_value(_DOMAIN, _RAW_KEY, _RAW_VALUE)
    getattr(stub_mass, _DOMAIN).update_config.side_effect = RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        await stub_mass.config.save_core_config(_DOMAIN, {CONF_LOG_LEVEL: "DEBUG"})

    stored_values = stub_mass.config.get(f"{CONF_CORE}/{_DOMAIN}/values")
    assert CONF_LOG_LEVEL not in stored_values
    assert stored_values[_RAW_KEY] == _RAW_VALUE
