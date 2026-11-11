"""Tests for the state the music controller keeps hidden in its core config."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from music_assistant.constants import CONF_LOG_LEVEL
from music_assistant.controllers.music.constants import (
    CONF_DELETED_PROVIDERS,
    CONF_TRACK_RECONCILIATION_CURSOR,
    CONF_TRACK_RECONCILIATION_RESCAN_DUE,
)

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigValueType

    from music_assistant.mass import MusicAssistant


# the settings page submits every entry with its current value, hidden ones included; an API
# caller may submit just the changed key
_SUBMIT_MODES = pytest.mark.parametrize(
    "full_form", [True, False], ids=["full_form", "changed_key"]
)


@_SUBMIT_MODES
@pytest.mark.parametrize(
    ("key", "stored"),
    [
        (CONF_DELETED_PROVIDERS, ["spotify--abc"]),
        (CONF_TRACK_RECONCILIATION_CURSOR, [12, 34]),
        (CONF_TRACK_RECONCILIATION_CURSOR, []),
        (CONF_TRACK_RECONCILIATION_RESCAN_DUE, True),
    ],
)
async def test_hidden_state_survives_a_core_config_save(
    music_mass_module: MusicAssistant, key: str, stored: ConfigValueType, full_form: bool
) -> None:
    """The values are declared, hidden settings, so saving the music settings carries them over."""
    mass = music_mass_module
    mass.config.set_raw_core_config_value("music", key, stored)

    await _save_music_settings(mass, full_form)

    assert mass.config.get_raw_core_config_value("music", key) == stored
    config = await mass.config.get_core_config("music")
    assert config.values[key].hidden


@_SUBMIT_MODES
@pytest.mark.parametrize(("cursor", "rescan_due"), [((12, 34), True), (None, False)])
async def test_the_walk_picks_up_where_it_was_after_a_core_config_save(
    music_mass_module: MusicAssistant,
    cursor: tuple[int, int] | None,
    rescan_due: bool,
    full_form: bool,
) -> None:
    """Saving the music settings rewinds neither a half-way nor a finished duplicate track walk."""
    mass = music_mass_module
    mass.music._set_track_reconciliation_state(cursor, rescan_due)

    await _save_music_settings(mass, full_form)
    mass.music._restore_track_reconciliation_state()

    assert mass.music._track_reconciliation_cursor == cursor
    assert mass.music._track_reconciliation_rescan_due is rescan_due


async def _save_music_settings(mass: MusicAssistant, full_form: bool) -> None:
    """
    Save the music core config with a changed log level.

    :param full_form: Submit every entry with its current value, as the settings page does,
        instead of only the changed key.
    """
    current = mass.config.get_raw_core_config_value("music", CONF_LOG_LEVEL)
    values: dict[str, ConfigValueType] = {CONF_LOG_LEVEL: "DEBUG" if current != "DEBUG" else "INFO"}
    if full_form:
        config = await mass.config.get_core_config("music")
        values = {key: entry.value for key, entry in config.values.items()} | values
    await mass.config.save_core_config("music", values)
