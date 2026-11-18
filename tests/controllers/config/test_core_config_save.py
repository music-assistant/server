"""
Tests for what a core config save keeps in, and drops from, persistent storage.

A stored value is a choice of the user and stays stored, also when it equals its default;
None unsets it. State a core controller keeps next to its settings belongs in a declared
(hidden) config entry; keeping undeclared raw values across a save is the safety net, not
the contract. The same safety net covers keys stored next to the values, which is where
the tasks controller keeps its scheduler state.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest
from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import ConfigEntryType

from music_assistant.constants import CONF_CORE, CONF_LOG_LEVEL
from music_assistant.controllers.cache.constants import CACHE_DATABASE_CLEANUP_TASK_ID

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant

_DOMAIN = "faketestcore"
_RAW_KEY = "runtime_state"
_RAW_VALUE = ["spotify--abc", "tidal--def"]
_TOP_LEVEL_KEY = "scheduler_state"
_TOP_LEVEL_VALUE = {"some_task": {"status": "idle", "schedule": {"enabled": False}}}
_OTHER_KEY = "other_setting"
_OTHER_ENTRY = ConfigEntry(
    key=_OTHER_KEY, type=ConfigEntryType.INTEGER, default_value=0, required=False
)


@pytest.fixture
def stub_mass(mass_minimal: MusicAssistant) -> MusicAssistant:
    """Return a minimal instance with a stub core controller that declares no entries of its own."""
    controller = SimpleNamespace(
        get_config_entries=AsyncMock(return_value=()), update_config=AsyncMock()
    )
    setattr(mass_minimal, _DOMAIN, controller)
    return mass_minimal


def _store_top_level_key(mass: MusicAssistant) -> None:
    """Store a key next to the values of the stub core config, as the tasks controller does."""
    mass.config.ensure_core_config_base(_DOMAIN)
    mass.config.set(f"{CONF_CORE}/{_DOMAIN}/{_TOP_LEVEL_KEY}", _TOP_LEVEL_VALUE)


async def test_undeclared_raw_value_survives_a_core_config_save(stub_mass: MusicAssistant) -> None:
    """A value stored without a matching config entry is carried over by the settings save."""
    stub_mass.config.set_raw_core_config_value(_DOMAIN, _RAW_KEY, _RAW_VALUE)

    await stub_mass.config.save_core_config(_DOMAIN, {CONF_LOG_LEVEL: "DEBUG"})

    assert stub_mass.config.get_raw_core_config_value(_DOMAIN, _RAW_KEY) == _RAW_VALUE
    assert stub_mass.config.get_raw_core_config_value(_DOMAIN, CONF_LOG_LEVEL) == "DEBUG"


async def test_declared_value_set_back_to_its_default_stays_stored(
    stub_mass: MusicAssistant,
) -> None:
    """A declared value set back to its default stays stored, also through a save of another."""
    getattr(stub_mass, _DOMAIN).get_config_entries.return_value = (_OTHER_ENTRY,)
    stub_mass.config.set_raw_core_config_value(_DOMAIN, _RAW_KEY, _RAW_VALUE)
    await stub_mass.config.save_core_config(_DOMAIN, {CONF_LOG_LEVEL: "DEBUG"})
    await stub_mass.config.save_core_config(_DOMAIN, {CONF_LOG_LEVEL: "GLOBAL"})

    await stub_mass.config.save_core_config(_DOMAIN, {CONF_LOG_LEVEL: "GLOBAL", _OTHER_KEY: 5})

    stored_values = stub_mass.config.get(f"{CONF_CORE}/{_DOMAIN}/values")
    assert stored_values[CONF_LOG_LEVEL] == "GLOBAL"
    assert stored_values[_OTHER_KEY] == 5
    assert stored_values[_RAW_KEY] == _RAW_VALUE


async def test_declared_value_at_its_default_is_not_stored_when_new(
    stub_mass: MusicAssistant,
) -> None:
    """Storage stays minimal: a value that was not stored is only stored when it is no default."""
    await stub_mass.config.save_core_config(_DOMAIN, {CONF_LOG_LEVEL: "GLOBAL"})

    assert CONF_LOG_LEVEL not in stub_mass.config.get(f"{CONF_CORE}/{_DOMAIN}/values", {})


async def test_declared_value_submitted_as_none_is_unset(stub_mass: MusicAssistant) -> None:
    """A value submitted as None leaves storage and the controller applies its default."""
    controller = getattr(stub_mass, _DOMAIN)
    stub_mass.config.set_raw_core_config_value(_DOMAIN, _RAW_KEY, _RAW_VALUE)
    await stub_mass.config.save_core_config(_DOMAIN, {CONF_LOG_LEVEL: "DEBUG"})
    controller.update_config.reset_mock()

    config = await stub_mass.config.save_core_config(_DOMAIN, {CONF_LOG_LEVEL: None})

    stored_values = stub_mass.config.get(f"{CONF_CORE}/{_DOMAIN}/values")
    assert CONF_LOG_LEVEL not in stored_values
    assert stored_values[_RAW_KEY] == _RAW_VALUE
    assert config.get_value(CONF_LOG_LEVEL) == "GLOBAL"
    controller.update_config.assert_awaited_once()


async def test_unsetting_a_value_at_its_default_is_stored_without_applying_it(
    stub_mass: MusicAssistant,
) -> None:
    """Unsetting a stored value that equals its default changes storage, not the controller."""
    controller = getattr(stub_mass, _DOMAIN)
    await stub_mass.config.save_core_config(_DOMAIN, {CONF_LOG_LEVEL: "DEBUG"})
    await stub_mass.config.save_core_config(_DOMAIN, {CONF_LOG_LEVEL: "GLOBAL"})
    controller.update_config.reset_mock()

    await stub_mass.config.save_core_config(_DOMAIN, {CONF_LOG_LEVEL: None})

    assert CONF_LOG_LEVEL not in stub_mass.config.get(f"{CONF_CORE}/{_DOMAIN}/values")
    controller.update_config.assert_not_awaited()


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


async def test_a_failed_save_reverts_to_a_value_stored_at_its_default(
    stub_mass: MusicAssistant,
) -> None:
    """The revert after a failed controller update restores a value stored at its default."""
    await stub_mass.config.save_core_config(_DOMAIN, {CONF_LOG_LEVEL: "DEBUG"})
    await stub_mass.config.save_core_config(_DOMAIN, {CONF_LOG_LEVEL: "GLOBAL"})
    getattr(stub_mass, _DOMAIN).update_config.side_effect = RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        await stub_mass.config.save_core_config(_DOMAIN, {CONF_LOG_LEVEL: "DEBUG"})

    assert stub_mass.config.get_raw_core_config_value(_DOMAIN, CONF_LOG_LEVEL) == "GLOBAL"


async def test_key_stored_next_to_the_values_survives_a_core_config_save(
    stub_mass: MusicAssistant,
) -> None:
    """A key a controller keeps next to the values is carried over by the settings save."""
    _store_top_level_key(stub_mass)

    await stub_mass.config.save_core_config(_DOMAIN, {CONF_LOG_LEVEL: "DEBUG"})

    assert stub_mass.config.get(f"{CONF_CORE}/{_DOMAIN}/{_TOP_LEVEL_KEY}") == _TOP_LEVEL_VALUE
    assert stub_mass.config.get_raw_core_config_value(_DOMAIN, CONF_LOG_LEVEL) == "DEBUG"


async def test_a_failed_save_reverts_without_dropping_keys_next_to_the_values(
    stub_mass: MusicAssistant,
) -> None:
    """The revert after a failed controller update restores the keys next to the values too."""
    _store_top_level_key(stub_mass)
    getattr(stub_mass, _DOMAIN).update_config.side_effect = RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        await stub_mass.config.save_core_config(_DOMAIN, {CONF_LOG_LEVEL: "DEBUG"})

    assert stub_mass.config.get(f"{CONF_CORE}/{_DOMAIN}/{_TOP_LEVEL_KEY}") == _TOP_LEVEL_VALUE
    assert CONF_LOG_LEVEL not in stub_mass.config.get(f"{CONF_CORE}/{_DOMAIN}/values")


async def test_saving_the_tasks_settings_keeps_the_scheduled_task_states(
    mass: MusicAssistant,
) -> None:
    """A schedule edit survives a save of the Tasks settings along with the other task states."""
    mass.tasks.set_task_enabled(CACHE_DATABASE_CLEANUP_TASK_ID, False)
    task_ids = set(mass.config.get(f"{CONF_CORE}/tasks/scheduled_task_states"))
    assert CACHE_DATABASE_CLEANUP_TASK_ID in task_ids

    await mass.config.save_core_config("tasks", {CONF_LOG_LEVEL: "DEBUG"})

    states = mass.config.get(f"{CONF_CORE}/tasks/scheduled_task_states")
    assert set(states) == task_ids
    assert states[CACHE_DATABASE_CLEANUP_TASK_ID]["schedule"]["enabled"] is False


async def test_a_successful_save_leaves_no_last_error(stub_mass: MusicAssistant) -> None:
    """A successful save neither stores nor returns a last_error."""
    config = await stub_mass.config.save_core_config(_DOMAIN, {CONF_LOG_LEVEL: "DEBUG"})

    assert config.last_error is None
    assert stub_mass.config.get(f"{CONF_CORE}/{_DOMAIN}/last_error") is None
